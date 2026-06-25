// Spectator viewer. Polls a match's GET .../scene endpoint and renders the full
// `scene` payload (the world projected to now; there is no server push). Rendering
// dispatches on the match's game_id; add a renderer to RENDERERS for a new game.
//
// The camera is a standard OrbitControls rig: drag to rotate, scroll to zoom,
// right-drag to pan. Per-poll values (position, heading, scale, bar height) are
// stored as targets and eased every animation frame, so motion reads continuously
// between polls.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

// API base: explicit ?api= wins; otherwise localhost for local dev, and the
// production API subdomain when served from anywhere else.
const API = new URLSearchParams(location.search).get("api")
  || (location.hostname === "localhost" || location.hostname === "127.0.0.1"
        ? "http://localhost:8080"
        : "https://api.ludicrous-arena.com");
const POLL_HZ = 8;

const PALETTE = [0x7cc4ff, 0xff7c7c, 0x9cff7c, 0xffd27c, 0xc77cff, 0x7cffe1, 0xff7cd2, 0xe1ff7c];
const statusEl = document.getElementById("status");
const scoreboard = document.getElementById("scoreboard");
const scoresEl = document.getElementById("scores");
const healthWrap = document.getElementById("healthwrap");
const healthEl = document.getElementById("health");
const banner = document.getElementById("banner");
const bannerText = document.getElementById("bannerText");

// ---- three.js bootstrapping ----------------------------------------------
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.1;
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
document.getElementById("scene").appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0e16);
scene.fog = new THREE.Fog(0x0a0e16, 40, 130);

const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 500);
camera.position.set(10, 14, 18);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.autoRotate = true;        // gentle until the viewer grabs it
controls.autoRotateSpeed = 0.6;
controls.maxPolarAngle = Math.PI * 0.49; // do not drop below the floor
controls.addEventListener("start", () => { controls.autoRotate = false; });

const root = new THREE.Group();
scene.add(root);

scene.add(new THREE.HemisphereLight(0xbcd0ff, 0x20242e, 0.85));
const key = new THREE.DirectionalLight(0xffffff, 1.6);
key.position.set(18, 40, 14);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
Object.assign(key.shadow.camera, { left: -40, right: 40, top: 40, bottom: -40, near: 1, far: 140 });
key.shadow.bias = -0.0004;
scene.add(key);
scene.add(new THREE.AmbientLight(0x3a4658, 0.55));

function resize() {
  const w = innerWidth, h = innerHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
addEventListener("resize", resize);
resize();

let camSize = 20, framed = false;
function frameOnce() {
  if (framed) return;
  framed = true;
  controls.target.set(camSize / 2, 0.5, camSize / 2);
  camera.position.set(camSize / 2, camSize * 1.0, camSize * 1.35);
  controls.update();
}

const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 0.1);   // clamp so a tab regaining focus does not jump effects
  RENDERERS[activeGame]?.lerp?.(dt);   // only the active game's meshes need easing
  controls.update();
  renderer.render(scene, camera);
}

// Free the GPU resources of a subtree before detaching it. three.js does not do
// this on remove()/clear(), so without it every match switch and every removed
// mesh/label texture leaks video memory over a long spectator session.
function disposeTree(obj) {
  obj.traverse((n) => {
    n.geometry?.dispose();
    const m = n.material;
    if (m) (Array.isArray(m) ? m : [m]).forEach((mat) => { mat.map?.dispose(); mat.dispose(); });
  });
}

function clearRoot() {
  disposeTree(root);
  root.clear();
  framed = false;
  for (const r of Object.values(RENDERERS)) r.reset?.();
}

function colorFor(i) { return PALETTE[i % PALETTE.length]; }
const lerpN = (a, b, t) => a + (b - a) * t;
const hex = (c) => "#" + (c).toString(16).padStart(6, "0");

// A floating text label (agent name) as a sprite with a canvas texture.
function makeLabel(text, color) {
  const cv = document.createElement("canvas");
  cv.width = 256; cv.height = 64;
  const ctx = cv.getContext("2d");
  ctx.font = "bold 34px ui-monospace, monospace";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "rgba(8,10,14,0.72)";
  const w = ctx.measureText(text).width + 28;
  ctx.fillRect(128 - w / 2, 8, w, 48);
  ctx.fillStyle = hex(color);
  ctx.fillText(text, 128, 33);
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  const spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false }));
  spr.scale.set(2.4, 0.6, 1);
  return spr;
}

// ---- per-game renderers ---------------------------------------------------
const RENDERERS = {
  skirmish: makeSkirmishRenderer(),
  deathmatch: makeDeathmatchRenderer(),
  lockdown: makeLockdownRenderer(),
  trading_desk: makeFinanceRenderer(),
};

function makeSkirmishRenderer() {
  const fighters = new Map();   // id -> { group, label, target, heading, dead, hp, hitFlash }
  const shots = new Map();      // index -> { mesh, target }
  let prevBullets = [];         // last poll's bullet positions, to spot newly-fired shots
  let tAcc = 0;                 // accumulated time, for beacon bob/pulse
  let floor = null, wallGroup = null, sig = "";

  function buildArena(d) {
    const s = `${d.grid}|${d.walls.length}`;
    if (sig === s) return;
    sig = s;
    if (floor) root.remove(floor);
    if (wallGroup) root.remove(wallGroup);
    const g = d.grid;
    floor = new THREE.Group();
    // Lighter floor + brighter grid lines, easier to read than the old dark board.
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(g, g),
      new THREE.MeshStandardMaterial({ color: 0x3b4660, roughness: 0.92 }));
    plane.rotation.x = -Math.PI / 2;
    plane.position.set(g / 2 - 0.5, 0, g / 2 - 0.5);
    plane.receiveShadow = true;
    floor.add(plane);
    const grid = new THREE.GridHelper(g, g, 0x8fa6cc, 0x5a6a8c);
    grid.position.set(g / 2 - 0.5, 0.01, g / 2 - 0.5);
    floor.add(grid);
    root.add(floor);

    wallGroup = new THREE.Group();
    const wallMat = new THREE.MeshStandardMaterial({ color: 0x9fb0cc, roughness: 0.75, metalness: 0.05 });
    const geo = new THREE.BoxGeometry(0.98, 1.25, 0.98);
    for (const [x, y] of d.walls) {
      const m = new THREE.Mesh(geo, wallMat);
      m.position.set(x, 0.625, y);
      m.castShadow = true; m.receiveShadow = true;
      wallGroup.add(m);
    }
    root.add(wallGroup);
  }

  function makeFighter(color) {
    const grp = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.5, metalness: 0.1 });
    const legs = new THREE.Mesh(new THREE.BoxGeometry(0.34, 0.42, 0.26), mat);
    legs.position.y = 0.21; legs.castShadow = true; grp.add(legs);
    const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.2, 0.34, 4, 8), mat);
    torso.position.y = 0.66; torso.castShadow = true; grp.add(torso);
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.17, 16, 16),
      new THREE.MeshStandardMaterial({ color: 0xf0e6d2, roughness: 0.6 }));
    head.position.y = 1.02; head.castShadow = true; grp.add(head);
    // Gun points +X (the model's forward); rotation.y aims it along facing.
    const gun = new THREE.Mesh(new THREE.BoxGeometry(0.55, 0.09, 0.12),
      new THREE.MeshStandardMaterial({ color: 0x20242c, roughness: 0.4, metalness: 0.5 }));
    gun.position.set(0.34, 0.66, 0.12); gun.castShadow = true; grp.add(gun);
    grp.userData.bodyMat = mat;   // referenced to pulse red on a hit
    // Beacon: a marker that hovers over an "exposed" fighter (idle too long, now
    // visible to every enemy). Hidden until the scene flags the fighter exposed.
    const beacon = new THREE.Mesh(new THREE.ConeGeometry(0.22, 0.4, 4),
      new THREE.MeshBasicMaterial({ color: 0xffa23c, transparent: true }));
    beacon.position.y = 1.95; beacon.rotation.x = Math.PI; beacon.visible = false;
    grp.add(beacon); grp.userData.beacon = beacon;
    return grp;
  }

  // Transient effect rings (muzzle blast, hit burst), pooled so repeated shots do
  // not allocate. Each grows and fades over `dur` seconds, then is hidden and reused.
  const fx = [];
  function spawnRing(x, y, color, dur, r0, r1, h) {
    let e = fx.find(o => !o.mesh.visible);
    if (!e) {
      const mat = new THREE.MeshBasicMaterial({ color, transparent: true,
        side: THREE.DoubleSide, blending: THREE.AdditiveBlending, depthWrite: false });
      const mesh = new THREE.Mesh(new THREE.RingGeometry(0.5, 0.85, 28), mat);
      mesh.rotation.x = -Math.PI / 2;   // lie flat, grow outward in the XZ plane
      root.add(mesh);
      e = { mesh };
      fx.push(e);
    }
    e.mesh.material.color.setHex(color);
    e.mesh.position.set(x, h, y);
    e.mesh.visible = true;
    e.t = 0; e.dur = dur; e.r0 = r0; e.r1 = r1;
  }

  return {
    reset() { fighters.clear(); shots.clear(); fx.length = 0; prevBullets = []; tAcc = 0; floor = null; wallGroup = null; sig = ""; },
    update(d) {
      buildArena(d);
      camSize = d.grid; frameOnce();

      const seen = new Set();
      d.players.forEach((p, i) => {
        seen.add(p.id);
        let rec = fighters.get(p.id);
        if (!rec) {
          const group = makeFighter(colorFor(i));
          group.position.set(p.x, 0, p.y);
          const label = makeLabel(p.name, colorFor(i));
          label.position.set(p.x, 1.7, p.y);
          root.add(group); root.add(label);
          rec = { group, label, target: new THREE.Vector3(p.x, 0, p.y),
                  heading: Math.atan2(-p.dy, p.dx), dead: !p.alive, hp: p.hearts, hitFlash: 0 };
          fighters.set(p.id, rec);
        }
        if (p.hearts < rec.hp) {            // lost a heart since the last poll: flash red
          rec.hitFlash = 1;
          const lethal = !p.alive;
          spawnRing(p.x, p.y, 0xff2b2b, lethal ? 0.6 : 0.42, 0.3, lethal ? 2.1 : 1.4, 0.09);
        }
        rec.hp = p.hearts;
        rec.target.set(p.x, 0, p.y);
        if (p.alive) rec.heading = Math.atan2(-p.dy, p.dx);
        rec.dead = !p.alive;
        rec.label.position.set(p.x, 1.7, p.y);
        rec.label.material.opacity = p.alive ? 1 : 0.4;
        rec.group.userData.beacon.visible = !!p.exposed && p.alive;
      });
      for (const [id, rec] of fighters) if (!seen.has(id)) {
        disposeTree(rec.group); root.remove(rec.group);
        disposeTree(rec.label); root.remove(rec.label);  // frees the name-label texture
        fighters.delete(id);
      }

      // Bullets: pooled spheres, lerped so they streak rather than jump.
      const n = d.bullets.length;
      d.bullets.forEach((b, i) => {
        let rec = shots.get(i);
        if (!rec) {
          const m = new THREE.Mesh(new THREE.SphereGeometry(0.12, 10, 10),
            new THREE.MeshBasicMaterial({ color: 0xffe9a0 }));
          m.position.set(b.x, 0.6, b.y);
          root.add(m);
          rec = { mesh: m, target: new THREE.Vector3(b.x, 0.6, b.y) };
          shots.set(i, rec);
        }
        rec.mesh.visible = true;
        rec.target.set(b.x, 0.6, b.y);
      });
      for (const [i, rec] of shots) if (i >= n) rec.mesh.visible = false;

      // A bullet with no near match in last poll's set was just fired: muzzle blast
      // at the gun. (Between polls a live bullet moves < 1 cell, so it self-matches.)
      d.bullets.forEach(b => {
        if (!prevBullets.some(pb => Math.hypot(pb.x - b.x, pb.y - b.y) < 1.6))
          spawnRing(b.x, b.y, 0xffcf6a, 0.34, 0.25, 1.25, 0.6);
      });
      prevBullets = d.bullets.map(b => ({ x: b.x, y: b.y }));

      const ranked = d.players.slice().sort((a, b) => b.score - a.score);
      renderScores(ranked.map(p => ({ label: p.name, score: p.score, color: colorFor(d.players.indexOf(p)) })));
      renderHealth(d.players.map((p, i) => ({ name: p.name, hearts: p.hearts,
        max: d.hearts_max, alive: p.alive, color: colorFor(i) })));
      statusExtra = `round ${d.round} · first to ${d.score_to_win}`;
    },
    lerp(dt) {
      const d = dt || 0.016;
      tAcc += d;
      for (const rec of fighters.values()) {
        rec.group.position.lerp(rec.target, 0.22);
        // Ease heading along the shortest arc.
        let dh = rec.heading - rec.group.rotation.y;
        dh = Math.atan2(Math.sin(dh), Math.cos(dh));
        rec.group.rotation.y += dh * 0.25;
        // Fall over when eliminated; stand otherwise.
        const targZ = rec.dead ? -Math.PI / 2 : 0;
        rec.group.rotation.z = lerpN(rec.group.rotation.z, targZ, 0.18);
        rec.group.position.y = lerpN(rec.group.position.y, rec.dead ? 0.18 : 0, 0.18);
        // Hit flash: pulse the body emissive red, decaying over ~0.4s.
        const bm = rec.group.userData.bodyMat;
        if (bm) {
          if (rec.hitFlash > 0) rec.hitFlash = Math.max(0, rec.hitFlash - d / 0.4);
          bm.emissive.setRGB(rec.hitFlash * 0.95, 0, 0);
        }
        const beacon = rec.group.userData.beacon;
        if (beacon && beacon.visible) {       // bob and spin so an exposed camper stands out
          beacon.rotation.y += d * 4;
          beacon.position.y = 1.95 + Math.sin(tAcc * 5) * 0.1;
          beacon.material.opacity = 0.7 + 0.3 * Math.sin(tAcc * 5);
        }
      }
      for (const rec of shots.values()) if (rec.mesh.visible) rec.mesh.position.lerp(rec.target, 0.5);
      // Expand and fade the effect rings.
      for (const e of fx) {
        if (!e.mesh.visible) continue;
        e.t += d;
        const k = e.t / e.dur;
        if (k >= 1) { e.mesh.visible = false; continue; }
        const r = e.r0 + (e.r1 - e.r0) * k;
        e.mesh.scale.set(r, r, 1);
        e.mesh.material.opacity = 1 - k;
      }
    },
  };
}

function makeDeathmatchRenderer() {
  const players = new Map();
  const shots = new Map();
  let ground = null, size = 20;
  function ensureGround(s) {
    if (ground && size === s) return;
    size = s;
    if (ground) root.remove(ground);
    ground = new THREE.Group();
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(s, s),
      new THREE.MeshStandardMaterial({ color: 0x2a3344, roughness: 0.95 }));
    plane.rotation.x = -Math.PI / 2; plane.position.set(s / 2, 0, s / 2); plane.receiveShadow = true;
    ground.add(plane);
    ground.add(new THREE.GridHelper(s, s, 0x6a7ea0, 0x3a4760).translateX(s / 2).translateZ(s / 2));
    root.add(ground);
  }
  return {
    reset() { players.clear(); shots.clear(); ground = null; },
    update(d) {
      ensureGround(d.arena_size); camSize = d.arena_size; frameOnce();
      const seen = new Set();
      d.players.forEach((p, i) => {
        seen.add(p.id);
        let rec = players.get(p.id);
        if (!rec) {
          const mesh = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.9, 0.9),
            new THREE.MeshStandardMaterial({ color: colorFor(i), roughness: 0.4, metalness: 0.2, transparent: true }));
          mesh.castShadow = true; mesh.position.set(p.x, 0.45, p.y);
          root.add(mesh);
          rec = { mesh, target: new THREE.Vector3(p.x, 0.45, p.y), heading: -p.heading, scale: 1, opacity: 1 };
          players.set(p.id, rec);
        }
        rec.target.set(p.x, 0.45, p.y); rec.heading = -p.heading;
        rec.opacity = p.alive ? 1 : 0.2; rec.scale = 0.6 + 0.4 * (p.hp / 100);
      });
      for (const [id, rec] of players) if (!seen.has(id)) { root.remove(rec.mesh); players.delete(id); }
      const nn = d.projectiles.length;
      d.projectiles.forEach((pr, i) => {
        let rec = shots.get(i);
        if (!rec) {
          const m = new THREE.Mesh(new THREE.SphereGeometry(0.13, 10, 10), new THREE.MeshBasicMaterial({ color: 0xffe28a }));
          m.position.set(pr.x, 0.45, pr.y); root.add(m);
          rec = { mesh: m, target: new THREE.Vector3(pr.x, 0.45, pr.y) }; shots.set(i, rec);
        }
        rec.mesh.visible = true; rec.target.set(pr.x, 0.45, pr.y);
      });
      for (const [i, rec] of shots) if (i >= nn) rec.mesh.visible = false;
      renderScores(d.players.map((p, i) => ({ label: p.name, score: p.score, color: colorFor(i) })));
      renderHealth(null);
    },
    lerp() {
      for (const rec of players.values()) {
        rec.mesh.position.lerp(rec.target, 0.25);
        rec.mesh.rotation.y = lerpN(rec.mesh.rotation.y, rec.heading, 0.3);
        const s = lerpN(rec.mesh.scale.x, rec.scale, 0.3); rec.mesh.scale.setScalar(s);
        rec.mesh.material.opacity = lerpN(rec.mesh.material.opacity, rec.opacity, 0.3);
      }
      for (const rec of shots.values()) if (rec.mesh.visible) rec.mesh.position.lerp(rec.target, 0.5);
    },
  };
}

function makeLockdownRenderer() {
  const pawns = new Map();
  let frags = [], floor = null, exitMesh = null, grid = 8;
  function ensureFloor(g) {
    if (floor && grid === g) return;
    grid = g; if (floor) root.remove(floor);
    floor = new THREE.Group();
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(g, g),
      new THREE.MeshStandardMaterial({ color: 0x222b3a, roughness: 1 }));
    plane.rotation.x = -Math.PI / 2; plane.position.set((g - 1) / 2, 0, (g - 1) / 2); plane.receiveShadow = true;
    floor.add(plane);
    floor.add(new THREE.GridHelper(g, g, 0x6a7ea0, 0x3a4760).translateX((g - 1) / 2).translateZ((g - 1) / 2));
    root.add(floor);
  }
  return {
    reset() { pawns.clear(); frags = []; floor = null; exitMesh = null; },
    update(d) {
      ensureFloor(d.grid); camSize = d.grid; frameOnce();
      if (!exitMesh) {
        exitMesh = new THREE.Mesh(new THREE.BoxGeometry(0.8, 0.05, 0.8),
          new THREE.MeshStandardMaterial({ color: 0x39d98a, emissive: 0x0c5a36 }));
        root.add(exitMesh);
      }
      exitMesh.position.set(d.exit.x, 0.03, d.exit.y);
      // Pool fragment meshes by index (reuse across polls); do not rebuild each frame.
      d.fragments.forEach((f, i) => {
        let m = frags[i];
        if (!m) {
          m = new THREE.Mesh(new THREE.IcosahedronGeometry(0.18),
            new THREE.MeshStandardMaterial({ color: 0xffd27c, emissive: 0x5a4012 }));
          m.castShadow = true; root.add(m); frags[i] = m;
        }
        m.visible = true; m.position.set(f.x, 0.4, f.y);
      });
      for (let i = d.fragments.length; i < frags.length; i++) frags[i].visible = false;
      const seen = new Set();
      d.players.forEach((p, i) => {
        seen.add(p.id);
        let rec = pawns.get(p.id);
        if (!rec) {
          const mesh = new THREE.Mesh(new THREE.CapsuleGeometry(0.25, 0.4, 4, 8),
            new THREE.MeshStandardMaterial({ color: colorFor(i), roughness: 0.5 }));
          mesh.castShadow = true; mesh.position.set(p.x, 0.45, p.y); root.add(mesh);
          rec = { mesh, target: new THREE.Vector3(p.x, 0.45, p.y) }; pawns.set(p.id, rec);
        }
        rec.target.set(p.x, 0.45, p.y);
      });
      for (const [id, rec] of pawns) if (!seen.has(id)) { root.remove(rec.mesh); pawns.delete(id); }
      renderScores([{ label: "Extracted", score: `${d.delivered}/${d.total}`, color: 0x39d98a },
                    { label: "Ticks left", score: d.time_left, color: 0x7cc4ff }]);
      renderHealth(null);
    },
    lerp() { for (const { mesh, target } of pawns.values()) mesh.position.lerp(target, 0.3); },
  };
}

function makeFinanceRenderer() {
  const bars = new Map();
  let floor = null, line = null, span = 12, startCash = 10000;
  function ensureFloor(w) {
    if (floor && span === w) return;
    span = w; if (floor) root.remove(floor);
    floor = new THREE.Mesh(new THREE.PlaneGeometry(w, w),
      new THREE.MeshStandardMaterial({ color: 0x222b3a, roughness: 1 }));
    floor.rotation.x = -Math.PI / 2; floor.position.set(w / 2, 0, w / 2); floor.receiveShadow = true; root.add(floor);
  }
  return {
    reset() { bars.clear(); floor = null; if (line) { root.remove(line); line = null; } },
    update(d) {
      startCash = d.start_cash || 10000;
      const w = Math.max(d.players.length * 1.6, 12);
      ensureFloor(w); camSize = w; frameOnce();
      const seen = new Set();
      d.players.forEach((p, i) => {
        seen.add(p.id);
        let rec = bars.get(p.id);
        if (!rec) {
          const mesh = new THREE.Mesh(new THREE.BoxGeometry(0.9, 1, 0.9),
            new THREE.MeshStandardMaterial({ color: colorFor(i), roughness: 0.45, metalness: 0.15 }));
          mesh.castShadow = true; root.add(mesh); rec = { mesh, height: 1 }; bars.set(p.id, rec);
        }
        rec.height = Math.max(0.15, (p.equity / startCash) * 5);
        rec.mesh.position.set(1.2 + i * 1.6, 0, w / 2);
      });
      for (const [id, rec] of bars) if (!seen.has(id)) { root.remove(rec.mesh); bars.delete(id); }
      if (d.prices && d.prices.length > 1) {
        const p0 = d.prices[0] || 1;
        const denom = Math.max(1, d.prices.length - 1);  // span the revealed width
        const pts = d.prices.map((pr, j) => new THREE.Vector3((j / denom) * w, 3 + (pr / p0 - 1) * 14, w + 1));
        if (!line) {
          line = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0x7cc4ff }));
          root.add(line);
        }
        line.geometry.setFromPoints(pts);  // reuse the line; do not allocate a new one each poll
      }
      renderScores(d.players.slice().sort((a, b) => b.equity - a.equity)
        .map(p => ({ label: `${p.name} (pos ${p.position}${p.done ? " ✓" : ""})`, score: Math.round(p.equity), color: colorFor(d.players.indexOf(p)) })));
      renderHealth(null);
    },
    lerp() {
      for (const rec of bars.values()) {
        const h = lerpN(rec.mesh.scale.y, rec.height, 0.2);
        rec.mesh.scale.y = h; rec.mesh.position.y = h / 2;
      }
    },
  };
}

// ---- HUD ------------------------------------------------------------------
function esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function renderScores(rows) {
  scoreboard.hidden = false;
  scoresEl.innerHTML = rows.map(r =>
    `<div class="row"><span><span class="dot" style="background:${hex(r.color)}"></span>${esc(r.label)}</span><b>${esc(r.score)}</b></div>`).join("");
}
function renderHealth(rows) {
  if (!rows) { healthWrap.hidden = true; return; }
  healthWrap.hidden = false;
  healthEl.innerHTML = rows.map(r => {
    const hearts = "♥".repeat(Math.max(0, r.hearts)) + "♡".repeat(Math.max(0, r.max - r.hearts));
    return `<div class="row"><span class="name${r.alive ? "" : " dead"}"><span class="dot" style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:${hex(r.color)}"></span>${esc(r.name)}</span><span class="hearts" style="color:${r.alive ? "#ff6b7a" : "#56607a"}">${r.alive ? hearts : "out"}</span></div>`;
  }).join("");
}

// ---- match selection + polling -------------------------------------------
let pollTimer = null, activeGame = null, statusExtra = "";
let currentMatch = null, currentGame = null;   // what the viewer is connected to

async function loadMatches() {
  const sel = document.getElementById("matchSelect");
  const keep = sel.value;
  try {
    const matches = await (await fetch(`${API}/v1/matches`)).json();
    sel.innerHTML = `<option value="">select a match…</option>` + matches.map(m =>
      `<option value="${m.match_id}" data-game="${m.game_id}">${esc(m.game_id)} · ${m.match_id.slice(0, 6)} · ${m.phase}</option>`).join("");
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
    statusEl.textContent = `${matches.length} match(es)`;
  } catch (e) { statusEl.textContent = `cannot reach API at ${API}`; }
}

function connect(matchId, gameId) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  clearRoot();
  activeGame = gameId; statusExtra = "";
  currentMatch = matchId || null; currentGame = gameId || null;
  banner.style.display = "none";
  if (!matchId) return;
  const r = RENDERERS[gameId];
  if (!r) { statusEl.textContent = `no renderer for game '${gameId}'`; return; }
  let ended = false;
  async function poll() {
    try {
      const resp = await fetch(`${API}/v1/matches/${matchId}/scene`);
      if (!resp.ok) {
        statusEl.textContent = resp.status === 404 ? "match no longer available" : `error ${resp.status}`;
        if (resp.status === 404 && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        return;
      }
      const frame = await resp.json();
      if (frame.scene) r.update(frame.scene);
      else { statusEl.textContent = `${gameId} · ${frame.phase} · waiting for players…`; return; }
      statusEl.textContent = `${gameId} · tick ${frame.tick} · ${frame.phase}${statusExtra ? " · " + statusExtra : ""}`;
      if (frame.phase === "finished" && !ended) {
        ended = true;
        const res = frame.result || {};
        // winners are match-local player_ids; map them to display names.
        const players = frame.scene?.players || [];
        const nameOf = (id) => players.find(p => p.id === id)?.name || id;
        const w = (res.winners || []).map(nameOf);
        bannerText.textContent = w.length ? `Winner: ${w.join(", ")} (${res.reason})` : `Match over (${res.reason})`;
        banner.style.display = "grid";
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      }
    } catch (e) { statusEl.textContent = "reconnecting…"; }
  }
  poll();
  pollTimer = setInterval(poll, 1000 / POLL_HZ);
}

document.getElementById("matchSelect").addEventListener("change", (e) => {
  const opt = e.target.selectedOptions[0];
  connect(e.target.value, opt?.dataset.game);
});
document.getElementById("refresh").addEventListener("click", loadMatches);
setInterval(() => { if (!pollTimer) loadMatches(); }, 4000);

// Admin reset: the button shows only when an admin token is stored in this browser.
// Provide it once via the URL fragment (#admin=<token>); it is moved to localStorage
// and stripped from the address bar. The server still enforces the token, so the
// button being hidden is convenience, not the security boundary.
(function setupAdmin() {
  const ADMIN_KEY = "arena_admin_token";
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("admin");
  if (fromHash) {
    localStorage.setItem(ADMIN_KEY, fromHash);
    history.replaceState(null, "", location.pathname + location.search);
  }
  const token = localStorage.getItem(ADMIN_KEY);
  const btn = document.getElementById("reset");
  if (!token || !btn) return;
  btn.hidden = false;
  btn.addEventListener("click", async () => {
    if (!currentMatch) { statusEl.textContent = "select a match first"; return; }
    btn.disabled = true;
    try {
      const resp = await fetch(`${API}/v1/matches/${currentMatch}/reset`,
        { method: "POST", headers: { Authorization: "Bearer " + token } });
      if (resp.ok) {
        connect(currentMatch, currentGame);   // resume polling from the fresh world
        statusEl.textContent = "match reset";
      } else if (resp.status === 401 || resp.status === 403) {
        statusEl.textContent = "admin token rejected";
        localStorage.removeItem(ADMIN_KEY); btn.hidden = true;
      } else {
        statusEl.textContent = `reset failed (${resp.status})`;
      }
    } catch (e) { statusEl.textContent = "reset failed"; }
    btn.disabled = false;
  });
})();

const wantMatch = new URLSearchParams(location.search).get("match");
(async function init() {
  await loadMatches();
  if (wantMatch) {
    const sel = document.getElementById("matchSelect");
    const opt = [...sel.options].find(o => o.value === wantMatch);
    if (opt) { sel.value = wantMatch; connect(wantMatch, opt.dataset.game); }
  }
})();

animate(); // RENDERERS initialized above; safe to start the render loop

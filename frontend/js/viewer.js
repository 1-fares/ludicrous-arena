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

// Deterministic PRNG (mulberry32) so a stage's procedural crack texture is stable
// across polls instead of shimmering as it is regenerated.
function mulberry32(seed) {
  return function () {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// A cracked-floor texture for one collapse stage. Higher stages darken the surface
// toward charred and add more, brighter fault lines. The same canvas is used as the
// emissive map, so the bright fault pixels glow while the dulled surface does not.
function crackCanvasTexture(stage, stages) {
  const frac = stages > 1 ? stage / (stages - 1) : 1;
  const cv = document.createElement("canvas");
  cv.width = cv.height = 128;
  const ctx = cv.getContext("2d");
  // Base floor color (0x3b4660) darkening toward charred (0x241a12) with the stage.
  const r = Math.round(0x3b + (0x24 - 0x3b) * frac);
  const g = Math.round(0x46 + (0x1a - 0x46) * frac);
  const b = Math.round(0x60 + (0x12 - 0x60) * frac);
  ctx.fillStyle = `rgb(${r},${g},${b})`;
  ctx.fillRect(0, 0, 128, 128);
  const rnd = mulberry32(stage * 0x9e3779b1 + 17);
  for (let i = 0; i < 80; i++) {              // charred speckle, denser at high stages
    ctx.fillStyle = `rgba(10,8,6,${(0.05 + 0.25 * frac * rnd()).toFixed(3)})`;
    const s = 2 + rnd() * 5;
    ctx.fillRect(rnd() * 128, rnd() * 128, s, s);
  }
  const lines = 2 + stage * 2;               // glowing fault lines from edge toward centre
  for (let i = 0; i < lines; i++) {
    let x = rnd() * 128, y = rnd() < 0.5 ? 0 : 128;
    if (rnd() < 0.5) { y = rnd() * 128; x = rnd() < 0.5 ? 0 : 128; }
    const heat = 0.4 + 0.6 * frac;
    ctx.strokeStyle = `rgb(${Math.round(180 + 75 * heat)},${Math.round(70 + 60 * heat)},${Math.round(20 * heat)})`;
    ctx.lineWidth = 1 + 2 * frac;
    ctx.beginPath();
    ctx.moveTo(x, y);
    let cx = x, cy = y;
    const steps = 4 + Math.floor(rnd() * 3);
    for (let s2 = 0; s2 < steps; s2++) {     // jagged walk to read as a fracture
      cx += (64 - cx) * (0.25 + rnd() * 0.3) + (rnd() - 0.5) * 26;
      cy += (64 - cy) * (0.25 + rnd() * 0.3) + (rnd() - 0.5) * 26;
      ctx.lineTo(cx, cy);
    }
    ctx.stroke();
  }
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// ---- per-game renderers ---------------------------------------------------
const RENDERERS = {
  skirmish: makeSkirmishRenderer(),
  deathmatch: makeDeathmatchRenderer(),
  lockdown: makeLockdownRenderer(),
  trading_desk: makeFinanceRenderer(),
};

function makeSkirmishRenderer() {
  const fighters = new Map();   // id -> { group, label, target, heading, dead, out, falling, fallen, vy, hp, hitFlash }
  const shots = new Map();      // index -> { mesh, target }
  let prevBullets = [];         // last poll's bullet positions, to spot newly-fired shots
  let tAcc = 0;                 // accumulated time, for beacon bob/pulse and crack pulse
  let floor = null, wallGroup = null, builtGrid = -1;
  // Collapsing floor: the board is built from per-cell tiles so individual cells can
  // crack, sink and fall away (a single plane cannot be punched to show a hole).
  const tiles = new Map();      // "x,y" -> { mesh, state, decay, fallsIn, stage, sink, vy, crackMat }
  // Walls collapse the same way: each wall cell is its own box so it can crack, sink,
  // glow and tumble into the abyss, then be restored solid on a new round.
  const walls = new Map();      // "x,y" -> { mesh, x, y, state, decay, fallsIn, stage, sink, vy, crackMat }
  const crackTex = new Map();   // stage -> CanvasTexture, shared by every cracking tile/wall
  let tileGeo = null, solidMat = null, wallGeo = null, wallSolidMat = null, curStages = 1;
  const TILE_H = 0.3, VOID_DEPTH = 7, WALL_H = 1.25, WALL_REST_Y = WALL_H / 2;

  function getCrackTex(stage, stages) {
    let t = crackTex.get(stage);
    if (!t) { t = crackCanvasTexture(stage, stages); crackTex.set(stage, t); }
    return t;
  }

  // Per-tile crack materials and the stage textures live outside the scene graph once
  // a tile reverts to solid, so disposeTree() (which only walks attached meshes) cannot
  // reach them. Free them explicitly on rebuild and teardown.
  function disposeFloorExtras() {
    for (const t of tiles.values()) if (t.crackMat) { t.crackMat.dispose(); t.crackMat = null; }
    for (const w of walls.values()) if (w.crackMat) { w.crackMat.dispose(); w.crackMat = null; }
    for (const tex of crackTex.values()) tex.dispose();
    crackTex.clear();
  }

  function buildArena(d) {
    const g = d.grid;
    // Rebuild only when the maze identity changes: a new grid size, or a wall cell we
    // have never built (lobby placeholder -> real board, or a different match). Within a
    // round the standing-wall set only ever shrinks as walls crack and fall, so this never
    // rebuilds mid-collapse and the per-cell fall animations survive.
    const incoming = [];
    for (const [x, y] of (d.walls || [])) incoming.push(`${x},${y}`);
    for (const c of (d.walls_cracking || [])) incoming.push(`${c.x},${c.y}`);
    if (floor && g === builtGrid && incoming.every(k => walls.has(k))) return;
    builtGrid = g;
    if (floor) { disposeTree(floor); root.remove(floor); }
    if (wallGroup) { disposeTree(wallGroup); root.remove(wallGroup); }
    disposeFloorExtras();
    tiles.clear();
    walls.clear();
    floor = new THREE.Group();
    // Abyss seen through holes once the floor falls away: a dark unlit plane far below.
    const abyss = new THREE.Mesh(new THREE.PlaneGeometry(g * 3, g * 3),
      new THREE.MeshBasicMaterial({ color: 0x05070c }));
    abyss.rotation.x = -Math.PI / 2;
    abyss.position.set(g / 2 - 0.5, -VOID_DEPTH, g / 2 - 0.5);
    floor.add(abyss);
    // One thin tile per cell, top flush with y=0. The 0.96 width leaves a dark gap that
    // reads as the old grid, and the box thickness gives holes a visible lip when a
    // neighbour falls away. Geometry and the solid material are shared across tiles.
    tileGeo = new THREE.BoxGeometry(0.96, TILE_H, 0.96);
    solidMat = new THREE.MeshStandardMaterial({ color: 0x3b4660, roughness: 0.92 });
    for (let x = 0; x < g; x++) for (let y = 0; y < g; y++) {
      const m = new THREE.Mesh(tileGeo, solidMat);
      m.position.set(x, -TILE_H / 2, y);
      m.receiveShadow = true;
      floor.add(m);
      tiles.set(`${x},${y}`, { mesh: m, state: "solid", decay: 0, fallsIn: 99,
        stage: -1, sink: 0, vy: 0, crackMat: null });
    }
    root.add(floor);

    // Wall boxes share one geometry and one solid material; individual wall meshes are
    // created lazily in updateWalls so a cell first seen mid-crack still gets a box.
    wallGroup = new THREE.Group();
    wallGeo = new THREE.BoxGeometry(0.98, WALL_H, 0.98);
    wallSolidMat = new THREE.MeshStandardMaterial({ color: 0x9fb0cc, roughness: 0.75, metalness: 0.05 });
    root.add(wallGroup);
  }

  function ensureWall(key, x, y) {
    let w = walls.get(key);
    if (!w) {
      const m = new THREE.Mesh(wallGeo, wallSolidMat);
      m.position.set(x, WALL_REST_Y, y);
      m.castShadow = true; m.receiveShadow = true;
      wallGroup.add(m);
      w = { mesh: m, x, y, state: "solid", decay: 0, fallsIn: 99, stage: -1, sink: 0, vy: 0, crackMat: null };
      walls.set(key, w);
    }
    return w;
  }

  // Reconcile the wall field with this poll's data, mirroring updateFloor. Cracking walls
  // point at the matching stage texture and start to sink/glow; a wall that has moved into
  // floor.void begins its tumble into the abyss; everything else returns to a solid box (a
  // new round repairs the maze). Animation happens in lerp().
  function updateWalls(d) {
    const crackMap = new Map();
    (d.walls_cracking || []).forEach(c => crackMap.set(`${c.x},${c.y}`, c));
    const voidSet = new Set((d.floor?.void || []).map(([x, y]) => `${x},${y}`));
    for (const [x, y] of (d.walls || [])) ensureWall(`${x},${y}`, x, y);
    for (const c of (d.walls_cracking || [])) ensureWall(`${c.x},${c.y}`, c.x, c.y);
    for (const [key, w] of walls) {
      if (voidSet.has(key)) {
        if (w.state !== "void") { w.state = "void"; w.vy = 0; }   // begin the fall
      } else if (crackMap.has(key)) {
        const c = crackMap.get(key);
        w.state = "crack"; w.decay = c.decay || 0; w.fallsIn = c.falls_in ?? 99;
        const stage = Math.max(0, Math.min(curStages - 1, w.decay));
        if (w.stage !== stage) {                  // (re)point the wall at this stage's look
          w.stage = stage;
          if (!w.crackMat) w.crackMat = new THREE.MeshStandardMaterial({
            color: 0xffffff, roughness: 0.8, emissive: 0xff7a1e, emissiveIntensity: 0 });
          const tex = getCrackTex(stage, curStages);
          w.crackMat.map = tex; w.crackMat.emissiveMap = tex; w.crackMat.needsUpdate = true;
          w.mesh.material = w.crackMat;
        }
      } else if (w.state !== "solid") {            // standing again (new round): restore the box
        w.state = "solid"; w.stage = -1; w.decay = 0; w.fallsIn = 99; w.vy = 0; w.sink = 0;
        w.mesh.material = wallSolidMat; w.mesh.visible = true;
        w.mesh.rotation.set(0, 0, 0); w.mesh.scale.setScalar(1);
        w.mesh.position.set(w.x, WALL_REST_Y, w.y);
      }
    }
  }

  // Reconcile the tile field with this poll's floor data. Void cells start dropping;
  // cracking cells point their material at the matching stage texture; everything else
  // returns to solid (a new round repairs the board). Animation happens in lerp().
  function updateFloor(d) {
    const crackMap = new Map();
    (d.floor?.cracking || []).forEach(c => crackMap.set(`${c.x},${c.y}`, c));
    const voidSet = new Set((d.floor?.void || []).map(([x, y]) => `${x},${y}`));
    for (const [key, t] of tiles) {
      if (voidSet.has(key)) {
        if (t.state !== "void") { t.state = "void"; t.vy = 0; }   // begin the fall
      } else if (crackMap.has(key)) {
        const c = crackMap.get(key);
        t.state = "crack"; t.decay = c.decay || 0; t.fallsIn = c.falls_in ?? 99;
        const stage = Math.max(0, Math.min(curStages - 1, t.decay));
        if (t.stage !== stage) {                  // (re)point the tile at this stage's look
          t.stage = stage;
          if (!t.crackMat) t.crackMat = new THREE.MeshStandardMaterial({
            color: 0xffffff, roughness: 0.8, emissive: 0xff7a1e, emissiveIntensity: 0 });
          const tex = getCrackTex(stage, curStages);
          t.crackMat.map = tex; t.crackMat.emissiveMap = tex; t.crackMat.needsUpdate = true;
          t.mesh.material = t.crackMat;
        }
      } else if (t.state !== "solid") {            // repaired (new round): restore the tile
        t.state = "solid"; t.stage = -1; t.decay = 0; t.fallsIn = 99; t.vy = 0; t.sink = 0;
        t.mesh.material = solidMat; t.mesh.visible = true; t.mesh.rotation.set(0, 0, 0);
        t.mesh.position.y = -TILE_H / 2;           // snap back up rather than drift from the abyss
      }
    }
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
    reset() {
      fighters.clear(); shots.clear(); fx.length = 0; prevBullets = []; tAcc = 0;
      disposeFloorExtras(); tiles.clear(); walls.clear();
      floor = null; wallGroup = null; builtGrid = -1;
      tileGeo = null; solidMat = null; wallGeo = null; wallSolidMat = null; curStages = 1;
    },
    // True while any eliminated fighter is still dropping through the hole; poll() uses
    // this to hold the winner banner until the fall-off animations finish.
    fallsPending() {
      for (const rec of fighters.values()) if (rec.falling) return true;
      return false;
    },
    update(d) {
      buildArena(d);
      curStages = d.collapse?.stages || 1;
      updateFloor(d);
      updateWalls(d);
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
          // A fighter first seen already out fell before we tuned in: place it gone, do
          // not replay the drop (the out -> respawn cycle below animates real transitions).
          const startedOut = !!p.out;
          rec = { group, label, target: new THREE.Vector3(p.x, 0, p.y),
                  heading: Math.atan2(-p.dy, p.dx), dead: !p.alive,
                  out: startedOut, falling: false, fallen: startedOut, vy: 0, hp: p.hearts, hitFlash: 0 };
          if (startedOut) { group.visible = false; label.visible = false; }
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
        const wasOut = rec.out;
        rec.out = !!p.out;
        rec.dead = !p.alive && !p.out;        // out (eliminated) drives its own fall, not the downed pose
        if (rec.out && !wasOut && !rec.falling && !rec.fallen) {   // out just went false -> true
          // Always play the burst + drop, even if the match finishes this same poll; the
          // winner banner is held (see poll()) until in-flight drops complete.
          rec.falling = true; rec.vy = 0;
          spawnRing(p.x, p.y, 0x7a3ce0, 0.7, 0.3, 2.0, 0.1);   // dark burst marks the elimination
        }
        if (!rec.out && (rec.falling || rec.fallen)) {  // respawned / new round: lift back in
          rec.falling = false; rec.fallen = false; rec.vy = 0;
          rec.group.visible = true; rec.label.visible = true;
          rec.group.scale.setScalar(1);
          rec.group.rotation.set(0, rec.group.rotation.y, 0);
          rec.group.position.set(p.x, 0, p.y);
        }
        rec.label.position.set(p.x, 1.7, p.y);
        rec.label.material.opacity = p.out ? 0 : (p.alive ? 1 : 0.4);
        rec.group.userData.beacon.visible = !!p.exposed && p.alive && !p.out;
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

      const round = d.match?.round ?? d.round ?? 0;
      const roundsToWin = d.match?.rounds_to_win ?? d.score_to_win ?? 0;
      const roundWins = d.match?.round_wins || {};
      const winsOf = (p) => (typeof p.round_wins === "number" ? p.round_wins : (roundWins[p.name] || 0));
      const ranked = d.players.slice().sort((a, b) => (winsOf(b) - winsOf(a)) || (b.score - a.score));
      renderScores(ranked.map(p => ({ label: p.name, score: p.score, frags: p.frags,
        exposed: p.exposed, out: p.out, wins: winsOf(p), color: colorFor(d.players.indexOf(p)) })),
        "score = frags + new ground", { round, roundsToWin });
      renderHealth(d.players.map((p, i) => ({ name: p.name, hearts: p.hearts,
        max: d.hearts_max, alive: p.alive, out: p.out, color: colorFor(i) })));
      statusExtra = `round ${round} - first to ${roundsToWin}`;
    },
    lerp(dt) {
      const d = dt || 0.016;
      tAcc += d;
      // Floor: drop void tiles under gravity, ease crack sink, pulse the danger glow.
      for (const t of tiles.values()) {
        if (t.state === "void") {
          t.vy += d * 9;
          t.mesh.position.y -= t.vy * d;
          t.mesh.rotation.x += d * 1.5;            // tumble as it falls into the abyss
          if (t.mesh.position.y < -VOID_DEPTH) t.mesh.visible = false;
          continue;
        }
        let targetSink = 0;
        if (t.state === "crack" && t.crackMat) {
          const frac = curStages > 1 ? t.stage / (curStages - 1) : 1;
          targetSink = 0.04 + 0.14 * frac;
          // The nearer the fall, the brighter and more red the fault lines pulse.
          const imminent = t.fallsIn <= 2 ? (3 - Math.max(0, t.fallsIn)) / 3 : 0;
          const pulse = 0.5 + 0.5 * Math.sin(tAcc * 6);
          t.crackMat.emissiveIntensity = 0.25 + 0.5 * frac + imminent * (0.6 + 0.8 * pulse);
          t.crackMat.emissive.setHex(t.fallsIn <= 1 ? 0xff3010 : 0xff7a1e);
          targetSink += imminent * 0.05 * pulse;
        }
        t.sink = lerpN(t.sink, targetSink, 0.15);
        t.mesh.position.y = lerpN(t.mesh.position.y, -TILE_H / 2 - t.sink, 0.2);
      }
      // Walls: identical treatment to floor tiles, scaled for the taller box. Void walls
      // tumble into the abyss leaving a hole; cracking walls sink and glow toward collapse.
      for (const w of walls.values()) {
        if (w.state === "void") {
          w.vy += d * 9;
          w.mesh.position.y -= w.vy * d;
          w.mesh.rotation.x += d * 1.5;            // tumble as it falls into the abyss
          w.mesh.rotation.z += d * 0.8;
          if (w.mesh.position.y < -VOID_DEPTH) w.mesh.visible = false;
          continue;
        }
        let targetSink = 0;
        if (w.state === "crack" && w.crackMat) {
          const frac = curStages > 1 ? w.stage / (curStages - 1) : 1;
          targetSink = 0.05 + 0.18 * frac;
          // The nearer the fall, the brighter and more red the fault lines pulse.
          const imminent = w.fallsIn <= 2 ? (3 - Math.max(0, w.fallsIn)) / 3 : 0;
          const pulse = 0.5 + 0.5 * Math.sin(tAcc * 6);
          w.crackMat.emissiveIntensity = 0.25 + 0.5 * frac + imminent * (0.6 + 0.8 * pulse);
          w.crackMat.emissive.setHex(w.fallsIn <= 1 ? 0xff3010 : 0xff7a1e);
          targetSink += imminent * 0.06 * pulse;
        }
        w.sink = lerpN(w.sink, targetSink, 0.15);
        w.mesh.position.y = lerpN(w.mesh.position.y, WALL_REST_Y - w.sink, 0.2);
      }
      for (const rec of fighters.values()) {
        if (rec.falling) {                         // eliminated: fall through the hole, tumbling
          rec.vy += d * 11;
          rec.group.position.y -= rec.vy * d;
          rec.group.rotation.x += d * 7;
          rec.group.rotation.z += d * 4;
          rec.group.scale.multiplyScalar(1 - d * 0.6);
          rec.label.material.opacity = Math.max(0, rec.label.material.opacity - d * 3);
          if (rec.group.position.y < -VOID_DEPTH) {
            rec.falling = false; rec.fallen = true;
            rec.group.visible = false; rec.label.visible = false;   // gone from the board (spectating)
          }
          continue;
        }
        if (rec.fallen) continue;                  // stays hidden until a respawn / new round
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
// Filled/empty pips for a fighter's round-wins, sized to the rounds-to-win target so the
// scoreboard reads "how close to taking the match". Capped so a long target cannot overflow.
function winPips(wins, target) {
  const span = Math.min(Math.max(wins, target || 0), 7);
  if (!span) return "";
  let s = "";
  for (let i = 0; i < span; i++) s += i < wins ? "●" : "○";
  return ` <span class="wins" title="${wins} round win${wins === 1 ? "" : "s"}">${s}</span>`;
}
function renderScores(rows, legend, progress) {
  scoreboard.hidden = false;
  const prog = progress && progress.roundsToWin
    ? `<div class="progress">Round ${esc(progress.round)} / first to ${esc(progress.roundsToWin)}</div>` : "";
  const head = legend ? `<div class="legend">${esc(legend)}</div>` : "";
  scoresEl.innerHTML = prog + head + rows.map(r => {
    const pips = r.wins != null ? winPips(r.wins, progress?.roundsToWin) : "";
    const detail = r.frags != null
      ? ` <span class="sub">${r.frags} frag${r.frags === 1 ? "" : "s"}${r.exposed ? " · <b class='exp'>EXPOSED</b>" : ""}${r.out ? " · <b class='out'>OUT</b>" : ""}</span>`
      : "";
    return `<div class="row"><span><span class="dot" style="background:${hex(r.color)}"></span>${esc(r.label)}${pips}${detail}</span><b>${esc(r.score)}</b></div>`;
  }).join("");
}
function renderHealth(rows) {
  if (!rows) { healthWrap.hidden = true; return; }
  healthWrap.hidden = false;
  healthEl.innerHTML = rows.map(r => {
    const hearts = "♥".repeat(Math.max(0, r.hearts)) + "♡".repeat(Math.max(0, r.max - r.hearts));
    // Three states: alive shows hearts; eliminated (fell into the void) is a distinct
    // purple "out"; merely downed shows "down" (it will respawn this round).
    const right = r.out
      ? `<span class="hearts" style="color:#b07cff">out</span>`
      : `<span class="hearts" style="color:${r.alive ? "#ff6b7a" : "#56607a"}">${r.alive ? hearts : "down"}</span>`;
    return `<div class="row"><span class="name${(r.alive && !r.out) ? "" : " dead"}"><span class="dot" style="display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:${hex(r.color)}"></span>${esc(r.name)}</span>${right}</div>`;
  }).join("");
}

// ---- toasts (players joining / dropping) ---------------------------------
const toastsEl = document.getElementById("toasts");
function toast(msg, leave) {
  const t = document.createElement("div");
  t.className = "toast" + (leave ? " leave" : "");
  t.textContent = msg;
  toastsEl.appendChild(t);
  requestAnimationFrame(() => t.classList.add("show"));
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 300); }, 3500);
}

// ---- match selection, auto-follow director, polling ----------------------
let pollTimer = null, activeGame = null, statusExtra = "";
let currentMatch = null, currentGame = null, autoFollow = true, adminToken = null;
const matchSelect = document.getElementById("matchSelect");
const gameMeta = {};   // game_id -> {min, title}

function titleFor(g) { return gameMeta[g]?.title || (g ? g[0].toUpperCase() + g.slice(1) : "-"); }

async function loadGameMeta() {
  try {
    for (const g of await (await fetch(`${API}/v1/games`)).json())
      gameMeta[g.id] = { min: g.min_players, title: g.title };
  } catch (e) { /* defaults are fine */ }
}

function setBanner(html) {
  if (html) { bannerText.innerHTML = html; banner.style.display = "grid"; }
  else { banner.style.display = "none"; }
}

// A placeholder skirmish board for the lobby, so the arena is up and circling and
// players visibly gather in it as they join (real positions appear once it starts).
function lobbyScene(info) {
  const g = 13, walls = [];
  for (let i = 0; i < g; i++) walls.push([i, 0], [i, g - 1], [0, i], [g - 1, i]);
  const n = Math.max(1, info.players.length);
  const players = info.players.map((p, i) => {
    const a = (i / n) * Math.PI * 2;
    return {
      id: p.player_id, name: p.display_name,
      x: Math.round(g / 2 + Math.cos(a) * 3), y: Math.round(g / 2 + Math.sin(a) * 3),
      dx: 0, dy: 1, hearts: 3, alive: true, exposed: false, score: 0, frags: 0,
    };
  });
  return { grid: g, walls, round: 0, phase: "lobby", score_to_win: 0, hearts_max: 3, players, bullets: [] };
}

function connect(matchId, gameId) {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  clearRoot();
  activeGame = gameId; statusExtra = "";
  currentMatch = matchId || null; currentGame = gameId || null;
  setBanner(null);
  if (!matchId) return;
  const r = RENDERERS[gameId];
  if (!r) { statusEl.textContent = `no renderer for game '${gameId}'`; return; }
  let rosterMap = null;   // player_id -> name, to toast joins/drops (null = first poll, seed quietly)
  let finishedAt = 0;     // when this match first read as finished, to time the held-banner safety window

  async function poll() {
    try {
      const [sceneR, infoR] = await Promise.all([
        fetch(`${API}/v1/matches/${matchId}/scene`),
        fetch(`${API}/v1/matches/${matchId}`),
      ]);
      if (sceneR.status === 404 || infoR.status === 404) {
        currentMatch = null;                       // gone (deleted/GC'd): the director repicks
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        return;
      }
      const frame = await sceneR.json();
      const info = await infoR.json();
      updateAdminControls(info);
      // Toast players as they connect / drop, so you see the lobby fill one by one.
      const cur = new Map(info.players.map(p => [p.player_id, p.display_name]));
      if (rosterMap === null) { rosterMap = cur; }   // first poll: seed without toasting
      else {
        const verb = info.phase === "running" ? "playing" : "waiting";
        for (const [id, nm] of cur) if (!rosterMap.has(id)) toast(`${nm} joined (${verb})`);
        for (const [id, nm] of rosterMap) if (!cur.has(id)) toast(`${nm} dropped`, true);
        rosterMap = cur;
      }
      // Lobby, or not yet started: show the arena spinning with players gathering
      // (the real board + characters once the server has it), the count in the
      // status. Never a blank board.
      if (info.phase === "lobby" || !frame.scene) {
        const min = gameMeta[gameId]?.min ?? 2;
        const need = info.players.length >= min ? "ready to start" : `waiting for ${min - info.players.length} more`;
        if (frame.scene) { r.update(frame.scene); setBanner(null); }              // real maze + players
        else if (gameId === "skirmish" && info.players.length) { r.update(lobbyScene(info)); setBanner(null); }
        else { setBanner(`<b>${esc(titleFor(gameId))}</b><div class="bsub">${info.players.length}/${info.max_players} joined · ${need}</div>`); scoreboard.hidden = true; }
        statusEl.textContent = `${titleFor(gameId)} · Lobby · ${info.players.length}/${info.max_players} joined · ${need}`;
        return;
      }
      if (frame.scene) r.update(frame.scene);
      if (info.phase === "finished") {
        if (!finishedAt) finishedAt = Date.now();
        const res = frame.result || {};
        const players = frame.scene?.players || [];
        const nameOf = (id) => players.find(p => p.id === id)?.name || id;
        const w = (res.winners || []).map(nameOf);
        if (info.break_until && info.break_until * 1000 > Date.now()) {
          const secs = Math.max(0, Math.round(info.break_until - Date.now() / 1000));
          const t = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
          setBanner(`<b>Intermission</b><div class="bsub">Next round in ${t} · agents are improving</div>${info.break_note ? `<div class="bsub">${esc(info.break_note)}</div>` : ""}`);
          statusEl.textContent = `${gameId} · break · next round ${t}`;
        } else if (r.fallsPending?.() && Date.now() - finishedAt < 4000) {
          // Hold the winner banner while a fall-off is still in flight (4s safety cap so it
          // can never hang); keep the board on screen so the drop is fully visible.
          setBanner(null);
          statusEl.textContent = `${titleFor(gameId)} · finishing…`;
        } else {
          setBanner(w.length ? `<b>Winner: ${esc(w.join(", "))}</b><div class="bsub">${esc(res.reason || "")}</div>` : `<b>Match over</b>`);
          statusEl.textContent = `${gameId} · finished · ${w.join(", ") || "-"}`;
        }
      } else {
        finishedAt = 0;
        setBanner(null);
        statusEl.textContent = `${titleFor(gameId)} · ${statusExtra || "running"} · LIVE`;
      }
    } catch (e) { statusEl.textContent = "reconnecting…"; }
  }
  poll();
  pollTimer = setInterval(poll, 1000 / POLL_HZ);
}

const followBtn = document.getElementById("follow");
matchSelect.addEventListener("change", (e) => {
  autoFollow = false; followBtn.classList.remove("on");      // a manual pick pins the match
  const opt = e.target.selectedOptions[0];
  connect(e.target.value, opt?.dataset.game);
});
followBtn.addEventListener("click", () => {
  autoFollow = true; followBtn.classList.add("on"); director();   // hand control back to the director
});

function bestMatch(ms) {
  return ms.find(m => m.phase === "running") || ms.find(m => m.phase === "finished") || ms[0] || null;
}

// The director keeps the dropdown fresh and, while auto-following, connects to the
// best live match, advancing to the next game on its own. It never yanks a spectator
// who has pinned a match (manual pick or ?match=).
async function director() {
  let matches;
  try { matches = await (await fetch(`${API}/v1/matches`)).json(); }
  catch (e) { statusEl.textContent = `cannot reach API at ${API}`; return; }
  const keep = matchSelect.value;
  matchSelect.innerHTML = `<option value="">select a match…</option>` + matches.map(m =>
    `<option value="${m.match_id}" data-game="${m.game_id}">${esc(m.game_id)} · ${m.match_id.slice(0, 6)} · ${m.phase} · ${m.players.length}p</option>`).join("");
  if ([...matchSelect.options].some(o => o.value === keep)) matchSelect.value = keep;
  if (!autoFollow) return;
  const best = bestMatch(matches);
  if (best) {
    if (best.match_id !== currentMatch) { matchSelect.value = best.match_id; connect(best.match_id, best.game_id); }
  } else if (!currentMatch) {
    setBanner(`<b>LUDICROUS ARENA</b><div class="bsub">No matches running. This page connects automatically when one begins.</div>`);
    scoreboard.hidden = true; statusEl.textContent = "waiting for a match";
  }
}
setInterval(director, 3000);

// ---- admin controls (revealed by an admin token) -------------------------
function updateAdminControls(info) {
  const startBtn = document.getElementById("start");   // start only makes sense in a lobby
  if (startBtn) startBtn.hidden = !(adminToken && info && info.phase === "lobby");
}

async function adminCall(method, path, body) {
  const resp = await fetch(`${API}${path}`, {
    method, headers: { Authorization: "Bearer " + adminToken, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (resp.status === 401 || resp.status === 403) {
    statusEl.textContent = "admin token rejected";
    localStorage.removeItem("arena_admin_token");
    document.getElementById("adminbar").hidden = true; adminToken = null;
  }
  return resp;
}

// Admin token is provided once via the URL fragment (#admin=<token>); it is moved to
// localStorage and stripped from the bar. The server enforces it, so the panel being
// hidden is only convenience, not the security boundary.
(function setupAdmin() {
  const ADMIN_KEY = "arena_admin_token";
  const fromHash = new URLSearchParams(location.hash.slice(1)).get("admin");
  if (fromHash) { localStorage.setItem(ADMIN_KEY, fromHash); history.replaceState(null, "", location.pathname + location.search); }
  adminToken = localStorage.getItem(ADMIN_KEY);
  const bar = document.getElementById("adminbar");
  if (!adminToken || !bar) return;
  bar.hidden = false;
  const on = (id, fn) => document.getElementById(id).addEventListener("click", fn);
  on("create", async () => {
    const r = await adminCall("POST", "/v1/matches", { game_id: "skirmish", autostart: false });
    if (r.ok) {
      const m = await r.json();
      autoFollow = false; followBtn.classList.remove("on");
      matchSelect.value = m.match_id; connect(m.match_id, m.game_id);
      statusEl.textContent = "match created; waiting for players";
    }
  });
  on("start", async () => {
    if (!currentMatch) return;
    const r = await adminCall("POST", `/v1/matches/${currentMatch}/start`);
    statusEl.textContent = r.ok ? "started" : `start failed (${r.status})`;
  });
  on("reset", async () => {
    if (currentMatch && confirm("Reset this match to round 1 for all players?")) {
      const r = await adminCall("POST", `/v1/matches/${currentMatch}/reset`);
      if (r.ok) connect(currentMatch, currentGame);
    }
  });
  on("brk", async () => {
    if (!currentMatch) return;
    const mins = parseFloat(prompt("Break length in minutes?", "5") || "0");
    if (mins > 0) {
      await adminCall("POST", `/v1/matches/${currentMatch}/break`,
        { minutes: mins, note: prompt("Note for agents (optional)?", "") || null });
      statusEl.textContent = `break: ${mins} min`;
    }
  });
  on("clear", async () => {
    const ms = await (await fetch(`${API}/v1/matches?phase=finished`)).json();
    for (const m of ms) await adminCall("DELETE", `/v1/matches/${m.match_id}`);
    statusEl.textContent = `cleared ${ms.length} finished`; director();
  });
})();

const wantMatch = new URLSearchParams(location.search).get("match");
(async function init() {
  await loadGameMeta();
  if (wantMatch) {
    autoFollow = false;                       // an explicit link pins the match
    let game = "skirmish";
    try {
      const m = (await (await fetch(`${API}/v1/matches`)).json()).find(x => x.match_id === wantMatch);
      if (m) game = m.game_id;
    } catch (e) { /* fall back to skirmish */ }
    connect(wantMatch, game);
  } else {
    followBtn.classList.add("on");
  }
  director();                                 // populate the dropdown and (if following) connect
})();

animate(); // RENDERERS initialized above; safe to start the render loop

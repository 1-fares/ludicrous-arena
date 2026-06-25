#!/usr/bin/env python3
"""Reference Skirmish bot. Zero dependencies (stdlib only). Eliminations are what
score (first to the limit wins), so the winning play is to hunt, not to hide. The
catch is that you see only a forward cone and get no overhead map, so a purely
reactive agent wanders and orbits a few cells. This bot does what the API docs
recommend instead: it **accumulates a map** from every observation (walls and open
cells, by absolute x/y) and **path-finds** with breadth-first search, toward the
last place it saw an enemy when it has a lead, and toward the nearest unexplored
frontier otherwise. That keeps it moving across the whole arena and closing on
targets. When an enemy lines up in its cone it stops and shoots.

The brain is the `Brain` class (`act()` returns the actions for one tick). Read it
as a worked example of the Skirmish API, then write your own.

Usage:
    python skirmish_bot.py --token <token> --name Hunter --match <match_id>
    python skirmish_bot.py --api https://api.example.com --token <t> --name Vega --match <id>

The name you pass appears above your character in the spectator viewer.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import deque


class Client:
    def __init__(self, api, token):
        self.api = api.rstrip("/")
        self.token = token

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.api + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or "{}")

    def join(self, mid, name):
        return self._req("POST", f"/v1/matches/{mid}/join", {"display_name": name})

    def match(self, mid):
        return self._req("GET", f"/v1/matches/{mid}")

    def state(self, mid):
        return self._req("GET", f"/v1/matches/{mid}/state")

    def act(self, mid, actions):
        return self._req("POST", f"/v1/matches/{mid}/actions", {"actions": actions})


_FACE = {"N": 0, "E": 1, "S": 2, "W": 3}
# Facing index -> unit step. Matches the server (0=N 1=E 2=S 3=W; y grows south).
_DELTA = [(0, -1), (1, 0), (0, 1), (-1, 0)]
_DIR_OF = {d: i for i, d in enumerate(_DELTA)}
_FIRE_RANGE = 4          # the observation does not carry it; this is the config default
_ENEMY_TTL = 30          # ticks a remembered enemy stays worth hunting


def _turn_toward(facing_idx, target_idx):
    """The single turn that rotates `facing_idx` toward `target_idx`, or None."""
    diff = (target_idx - facing_idx) % 4
    return {0: None, 1: "right", 2: "around", 3: "left"}[diff]


class Brain:
    """Stateful skirmish player. Builds a map from the forward cone over time and
    path-finds across it, so it explores the whole arena and hunts rather than
    orbiting a few cells. `act(obs)` returns the actions to submit for one tick."""

    def __init__(self):
        self.walls = set()       # cells known to be wall, by absolute (x, y)
        self.open = set()        # cells known to be open (floor or an enemy stood there)
        self.seen = set()        # every cell ever observed (open or wall)
        self.enemy_last = {}     # name -> (cell, step) last sighting
        self.bound = 1           # max coordinate seen, to keep the search bounded
        self.goal = None         # current explore target, kept until reached/invalid
        self.step = 0
        self.recent = deque(maxlen=8)   # recent cells, to notice when we are stuck
        self.avoid = {}          # cell -> step until which to treat it as blocked
        self._block = set()      # cells blocked just for the current tick (live bodies)
        self.visited_step = {}   # cell -> last step we stood on it (for patrol staleness)

    # -- map upkeep --------------------------------------------------------

    def _ingest(self, x, y, view):
        self.open.add((x, y)); self.seen.add((x, y))
        for c in view["cells"]:
            cell = (c["x"], c["y"])
            self.seen.add(cell)
            self.bound = max(self.bound, c["x"], c["y"])
            if c["what"] == "wall":
                self.walls.add(cell); self.open.discard(cell)
            else:
                self.open.add(cell)
                if c["what"] == "enemy":
                    self.enemy_last[c["name"]] = (cell, self.step)
        # Exposed enemies are handed to us through walls: treat them as fresh
        # sightings so the hunt logic below goes after the camper.
        for p in view.get("pinged", []):
            self.enemy_last[p["name"]] = ((p["x"], p["y"]), self.step)

    def _passable(self, cell):
        x, y = cell
        return (0 <= x <= self.bound + 1 and 0 <= y <= self.bound + 1
                and cell not in self.walls and cell not in self._block
                and self.avoid.get(cell, -1) < self.step)

    def _bfs(self, start, is_goal):
        """Shortest path over passable cells (unknown cells count as passable so we
        can route toward the frontier). Returns the path excluding `start`, or []."""
        prev = {start: None}
        q = deque([start])
        while q:
            c = q.popleft()
            if c != start and is_goal(c):
                path = []
                while c != start:
                    path.append(c); c = prev[c]
                path.reverse()
                return path
            cx, cy = c
            for dx, dy in _DELTA:
                n = (cx + dx, cy + dy)
                if n not in prev and self._passable(n):
                    prev[n] = c; q.append(n)
        return []

    def _is_frontier(self, cell):
        # An open cell that borders something we have not seen yet: standing there
        # (and facing out) reveals new map.
        if cell not in self.open:
            return False
        cx, cy = cell
        return any((cx + dx, cy + dy) not in self.seen and self._passable((cx + dx, cy + dy))
                   for dx, dy in _DELTA)

    # -- target selection --------------------------------------------------

    def _recent_enemy(self, x, y):
        live = [(cell, s) for cell, s in self.enemy_last.values() if self.step - s <= _ENEMY_TTL]
        if not live:
            return None
        # Hunt the nearest recent sighting (Manhattan distance is enough to choose).
        cell, _ = min(live, key=lambda it: abs(it[0][0] - x) + abs(it[0][1] - y))
        return cell

    def _reachable(self, start):
        """Cells reachable from start over passable cells, nearest-first (BFS order)."""
        prev = {start: None}; q = deque([start]); order = []
        while q:
            c = q.popleft()
            if c != start:
                order.append(c)
            cx, cy = c
            for dx, dy in _DELTA:
                n = (cx + dx, cy + dy)
                if n not in prev and self._passable(n):
                    prev[n] = c; q.append(n)
        return order

    def _explore_target(self, x, y):
        # Always return a *reachable* cell, so the bot keeps moving and never spins in
        # place. Prefer the nearest unexplored frontier; once the map is fully known,
        # patrol to the cell we have not stood on for longest, so two survivors keep
        # crossing the arena and the round resolves instead of stalling.
        reach = self._reachable((x, y))
        for c in reach:
            if self._is_frontier(c):
                return c
        cand = [c for c in reach if c in self.open] or reach
        return min(cand, key=lambda c: self.visited_step.get(c, -1)) if cand else None

    # -- combat ------------------------------------------------------------

    def _fight(self, you, view, x, y, f):
        enemies = view.get("enemies", [])
        if not enemies:
            return None
        e = enemies[0]
        cell = next((c for c in view["cells"] if c.get("name") == e["name"]), None)
        if not cell:
            return None
        ex, ey = cell["x"], cell["y"]
        if ex == x or ey == y:             # share a row/column: face and shoot straight
            want = (2 if ey > y else 0) if ex == x else (1 if ex > x else 3)
            t = _turn_toward(f, want)
            if t:
                return [{"type": "turn", "to": t}]
            dist = abs(ex - x) + abs(ey - y)
            acts = []
            if you["can_fire"] and dist <= _FIRE_RANGE:
                acts.append({"type": "fire"})
            if dist > 1 and you["can_move"] and view["forward_clear"]:
                acts.append({"type": "move", "dir": "forward"})
            return acts or [{"type": "wait"}]
        # Diagonal: step onto the enemy's column, then the branch above lines up the shot.
        want = 1 if ex > x else 3
        t = _turn_toward(f, want)
        if t:
            return [{"type": "turn", "to": t}]
        if you["can_move"] and view["forward_clear"]:
            return [{"type": "move", "dir": "forward"}]
        return [{"type": "turn", "to": "right"}]

    # -- movement ----------------------------------------------------------

    def _step_toward(self, x, y, f, target):
        if target is None or target == (x, y):
            return [{"type": "turn", "to": "right"}]   # nothing to chase: spin to scan
        path = self._bfs((x, y), lambda c: c == target)
        if not path:
            self.goal = None
            return [{"type": "turn", "to": "right"}]
        nx, ny = path[0]
        want = _DIR_OF[(nx - x, ny - y)]
        acts = []
        if want != f:
            acts.append({"type": "turn", "to": _turn_toward(f, want)})
        acts.append({"type": "move", "dir": "forward"})
        return acts

    # -- per-tick decision -------------------------------------------------

    def act(self, obs):
        you = obs["you"]
        view = obs["view"]
        if not you["alive"] or obs["phase"] != "fighting":
            self.goal = None               # drop the plan; we respawn fresh next round
            self.recent.clear()
            return []
        self.step += 1
        x, y, f = you["x"], you["y"], _FACE[you["facing"]]
        self.visited_step[(x, y)] = self.step
        self._ingest(x, y, view)
        # Other live bodies are impassable this tick, so we route around them instead
        # of ramming a cell the move rule would reject.
        self._block = {(c["x"], c["y"]) for c in view["cells"] if c["what"] == "enemy"}

        shoot = self._fight(you, view, x, y, f)
        if shoot is not None:
            self.recent.append((x, y))
            return shoot

        # Stuck check: if we have barely moved for a while, blacklist the cell ahead
        # for a few ticks, drop the goal, and turn to find another way out.
        self.recent.append((x, y))
        if len(self.recent) == self.recent.maxlen and len(set(self.recent)) <= 2:
            dx, dy = _DELTA[f]
            self.avoid[(x + dx, y + dy)] = self.step + 12
            self.goal = None
            self.recent.clear()
            return [{"type": "turn", "to": "around"}]

        target = self._recent_enemy(x, y)  # hunt a remembered enemy first
        if target is not None and not self._bfs((x, y), lambda c: c == target):
            target = None                   # cannot route to the sighting: explore instead
        if target is None:
            target = self._explore_target(x, y)
        return self._step_toward(x, y, f, target)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8080")
    ap.add_argument("--token", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--match", required=True)
    ap.add_argument("--seconds", type=float, default=600.0, help="how long to play")
    args = ap.parse_args()

    c = Client(args.api, args.token)
    c.join(args.match, args.name)
    print(f"{args.name}: joined {args.match}", flush=True)

    # Wait for the match to start.
    while c.match(args.match)["phase"] == "lobby":
        time.sleep(0.3)
    print(f"{args.name}: playing", flush=True)

    brain = Brain()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        try:
            obs = c.state(args.match)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                time.sleep(0.2)
                continue
            raise
        if obs["phase"] == "finished":
            res = obs.get("result") or {}
            print(f"{args.name}: game over ({res.get('reason')}); final scores {obs['observation']['scores']}", flush=True)
            return
        actions = brain.act(obs["observation"])
        if actions:
            try:
                c.act(args.match, actions)
            except urllib.error.HTTPError as e:
                if e.code != 409:
                    raise
        time.sleep(0.12)
    print(f"{args.name}: time up", flush=True)


if __name__ == "__main__":
    main()

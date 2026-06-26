"""Skirmish -- a grid-maze tactical shooter, the headline game.

Characters live on a grid of open cells and walls. Each one occupies a cell and
faces one of four directions. They move a cell at a time, turn in 90-degree steps,
and can only *see* forward through a narrow cone, so walls are cover and position
is everything. Shots travel a few cells then fade. Three hits and you are down,
lying on the floor and out of the round. Every elimination scores a point; first
to the score limit takes the game. Downed fighters respawn each new round, so the
incentive is to hunt: points come from kills, not from outliving a stalled round.

Pacing is real-time (`realtime=true`): the world advances on the wall clock, and
per-character cooldowns gate how often you can move or fire (one shot per second).
The engine fast-forwards the deterministic simulation on demand, so this game, like
every other, runs with no server loop.

Actions:
    {"type": "move", "dir": "forward" | "backward"}   step one cell (blocked by walls/players)
    {"type": "turn", "to": "left" | "right" | "around"}  rotate 90/90/180 degrees (free, no cooldown)
    {"type": "fire"}                                   shoot forward, once the gun has cooled down
    {"type": "wait"}                                   do nothing

Observation (forward vision only): you see an expanding cone ahead. At forward
distance d you see the row of 2d+1 cells (3 immediately ahead, then 5, then 7, ...)
out to `sight`, minus any cell hidden behind a wall. Coordinates are relative to
where you face: `forward` (1..sight) and `right` (negative is left).
    you:  {x, y, facing, hearts, alive, can_move, can_fire, score, frags, territory,
           exposed, camp_ticks, expose_at, last_hit}
    view: {cells: [{forward, right, x, y, what: wall|empty|enemy, name?}],
           enemies: [{name, forward, right, distance, bearing}],
           bullets: [{x, y, dx, dy}],       # in-flight shots within sight, even from behind
           pinged: [{name, x, y}],          # exposed campers, revealed through walls
           wall_ahead, forward_clear}
    round, phase, scores

`can_move` and `can_fire` are independent cooldowns: you may fire while the move
cooldown is active (shoot-and-scoot) and move while the gun is reloading. `camp_ticks`
counts ticks in the current cell; at `expose_at` you become `exposed`. `last_hit`
({tick, dir, from}) is the only cue for fire from outside the cone.

Two incentives push you to keep moving and hunting rather than camp. Standing in one
cell for `expose_ticks` makes you **exposed**: your position is broadcast to every
living enemy (in their `view.pinged`, ignoring walls and their cone) until you move,
so a camper becomes everyone's target. And the first time you enter each new cell
earns a sliver of score (`cell_bonus`, up to `territory_cap`), so a mover outranks a
camper on equal kills. Kills still dominate: `score = frags + min(territory, cap)`.

Config (all optional):
    grid:            int = 13      side length
    seed:            int = 1       deterministic maze + spawns
    wall_density:    float = 0.16  fraction of interior cells that are cover
    score_to_win:    int = 10      points (frags + capped territory) to win the game
    hearts:          int = 3       hits to eliminate
    fire_range:      int = 4       cells a shot travels before fading
    fire_cooldown:   int = 10      ticks between shots (10 tps -> 1s)
    move_cooldown:   int = 3       ticks between steps
    bullet_speed:    float = 6.0   cells per second
    sight:           int = 12      max forward distance seen (cone is 2*forward+1 wide)
    intermission:    int = 20      ticks between rounds (10 tps -> 2s)
    cell_bonus:      float = 0.05  score for each new cell entered (rewards exploring)
    territory_cap:   float = 4.0   most you can earn from new ground (kills still decide)
    expose_ticks:    int = 30      ticks idle in one cell before you are exposed
    drop_after:      int = 150     ticks with no action before a dropped client is downed (0=off)
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from arena.config import merge_defaults
from arena.models import GameMeta, GameMode, MatchResult, PlayerSlot
from arena.registry import register

# Facing: 0=N 1=E 2=S 3=W. Grid y grows "south" (down). Movement is along these.
DIRS = [(0, -1), (1, 0), (0, 1), (-1, 0)]
DIR_NAMES = ["N", "E", "S", "W"]
# Bearing of a direction relative to where you face (index = (abs_dir - facing) % 4).
REL_NAMES = ["ahead", "right", "behind", "left"]


def _dir_index(dx: float, dy: float) -> int:
    """Index into DIRS for an axis-aligned unit vector (bullets travel on an axis)."""
    key = (1 if dx > 0.5 else (-1 if dx < -0.5 else 0),
           1 if dy > 0.5 else (-1 if dy < -0.5 else 0))
    return DIRS.index(key) if key in DIRS else 0

META = GameMeta(
    id="skirmish",
    title="Skirmish",
    description="Grid-maze tactical shooter. Move a cell at a time, turn in 90-degree steps, see only forward through a narrow cone. Three hits and you are down. Every elimination scores a point and new ground scores a sliver, while camping in one cell exposes your position to all enemies; first to 10 wins the game.",
    mode=GameMode.deathmatch,
    min_players=2,
    max_players=8,
    teams=[],
    tick_rate=10.0,
    realtime=True,
    config_schema={
        "type": "object",
        "properties": {
            "grid": {"type": "integer", "default": 13, "minimum": 5, "maximum": 40},
            "seed": {"type": "integer", "default": 1},
            "wall_density": {"type": "number", "default": 0.16, "minimum": 0.0, "maximum": 0.5},
            "score_to_win": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100},
            "hearts": {"type": "integer", "default": 3, "minimum": 1, "maximum": 20},
            "fire_range": {"type": "integer", "default": 4, "minimum": 1, "maximum": 40},
            "fire_cooldown": {"type": "integer", "default": 10, "minimum": 0, "maximum": 600},
            "move_cooldown": {"type": "integer", "default": 3, "minimum": 0, "maximum": 600},
            "bullet_speed": {"type": "number", "default": 6.0, "minimum": 0.5, "maximum": 100.0},
            "sight": {"type": "integer", "default": 12, "minimum": 1, "maximum": 100,
                      "description": "Max forward distance you can see; the cone is 2*forward+1 cells wide at each step."},
            "intermission": {"type": "integer", "default": 20, "minimum": 0, "maximum": 6000},
            "round_limit": {"type": "integer", "default": 0, "minimum": 0, "maximum": 1_000_000,
                            "description": "Ticks before a stalled round is force-reset (respawns everyone, awards nothing). 0 disables it: a round ends only when one is left standing or all are dead."},
            "cell_bonus": {"type": "number", "default": 0.05, "minimum": 0.0, "maximum": 1.0,
                           "description": "Points for the first time you enter each new cell (capped by territory_cap). Rewards exploring; 0 disables it."},
            "territory_cap": {"type": "number", "default": 4.0, "minimum": 0.0, "maximum": 1000.0,
                              "description": "Most a player can earn from new ground over the match, so eliminations still decide the game."},
            "expose_ticks": {"type": "integer", "default": 30, "minimum": 0, "maximum": 6000,
                             "description": "Ticks of staying in one cell before you are exposed: your position is revealed to every living enemy (through walls) until you move. 0 disables it."},
            "drop_after": {"type": "integer", "default": 150, "minimum": 0, "maximum": 100000,
                           "description": "Ticks with no submitted action before a player is treated as dropped: they are downed for the round so a gone client cannot freeze the game or be farmed. 0 disables it."},
        },
    },
    action_schema={
        "oneOf": [
            {"type": "object", "required": ["type", "dir"],
             "properties": {"type": {"const": "move"}, "dir": {"enum": ["forward", "backward"]}}},
            {"type": "object", "required": ["type", "to"],
             "properties": {"type": {"const": "turn"}, "to": {"enum": ["left", "right", "around"]}}},
            {"type": "object", "required": ["type"], "properties": {"type": {"const": "fire"}}},
            {"type": "object", "required": ["type"], "properties": {"type": {"const": "wait"}}},
        ]
    },
    observation_schema={
        "type": "object",
        "properties": {
            "you": {
                "type": "object",
                "description": "Your own pose and status.",
                "properties": {
                    "x": {"type": "integer", "description": "Grid column."},
                    "y": {"type": "integer", "description": "Grid row."},
                    "facing": {"enum": ["N", "E", "S", "W"]},
                    "hearts": {"type": "integer", "description": "Hits remaining before elimination."},
                    "alive": {"type": "boolean"},
                    "can_move": {"type": "boolean", "description": "False while the move cooldown is active."},
                    "can_fire": {"type": "boolean", "description": "False while the gun is reloading. Independent of can_move: you may fire while the move cooldown is active (shoot-and-scoot), and move while the gun is reloading."},
                    "score": {"type": "number", "description": "Your standing: frags + capped territory."},
                    "frags": {"type": "integer", "description": "Eliminations you have scored."},
                    "territory": {"type": "number", "description": "Points earned from new ground (capped by territory_cap)."},
                    "exposed": {"type": "boolean", "description": "You have stayed put too long: your position is now broadcast to every enemy until you move."},
                    "camp_ticks": {"type": "integer", "description": "Ticks you have stayed in the current cell. Reaching expose_at flips exposed to true; moving resets it to 0."},
                    "expose_at": {"type": "integer", "description": "The camp_ticks value at which you become exposed (the expose_ticks config). 0 means exposure is disabled."},
                    "last_hit": {"type": ["object", "null"], "description": "The most recent hit you took this round, or null. Cleared on respawn.",
                                 "properties": {
                                     "tick": {"type": "integer", "description": "Tick the hit landed."},
                                     "dir": {"enum": ["N", "E", "S", "W"], "description": "Absolute direction the bullet was travelling."},
                                     "from": {"enum": ["ahead", "right", "behind", "left"], "description": "Bearing of the shooter relative to your facing."}}},
                },
            },
            "view": {
                "type": "object",
                "description": "Forward cone vision. At forward distance d you see 2d+1 cells, occluded by walls.",
                "properties": {
                    "cells": {
                        "type": "array",
                        "description": "Every visible cell, in your facing-relative frame.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "forward": {"type": "integer", "description": "Cells ahead (1..sight)."},
                                "right": {"type": "integer", "description": "Lateral offset; negative is to your left."},
                                "x": {"type": "integer", "description": "Absolute grid column."},
                                "y": {"type": "integer", "description": "Absolute grid row."},
                                "what": {"enum": ["wall", "empty", "enemy"]},
                                "name": {"type": "string", "description": "Present when what == enemy."},
                            },
                        },
                    },
                    "enemies": {
                        "type": "array",
                        "description": "Visible enemies (a subset of cells), nearest first. right == 0 with forward <= fire_range is a clean shot.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "forward": {"type": "integer"},
                                "right": {"type": "integer"},
                                "distance": {"type": "number", "description": "Cells away."},
                                "bearing": {"enum": ["ahead", "left", "right"]},
                            },
                        },
                    },
                    "wall_ahead": {"type": "integer", "description": "Clear cells straight ahead until a wall."},
                    "forward_clear": {"type": "boolean", "description": "True if the cell directly ahead is open."},
                    "bullets": {
                        "type": "array",
                        "description": "In-flight projectiles within sight, including from behind or beside you (not cone-gated and not occluded), so you can dodge. Empty while you are down.",
                        "items": {"type": "object", "properties": {
                            "x": {"type": "number", "description": "Absolute column (fractional; bullets move between cells)."},
                            "y": {"type": "number", "description": "Absolute row."},
                            "dx": {"type": "number", "description": "Unit velocity in x (the axis the bullet travels)."},
                            "dy": {"type": "number", "description": "Unit velocity in y."}}},
                    },
                    "pinged": {
                        "type": "array",
                        "description": "Exposed enemies (idle too long), revealed to everyone regardless of walls or your cone. Hunt them.",
                        "items": {"type": "object", "properties": {
                            "name": {"type": "string"},
                            "x": {"type": "integer"}, "y": {"type": "integer"}}},
                    },
                },
            },
            "round": {"type": "integer"},
            "phase": {"enum": ["fighting", "intermission"]},
            "scores": {"type": "object", "description": "Standing (frags + capped territory) per display name. Values are numbers: territory adds a fractional bonus.",
                       "additionalProperties": {"type": "number"}},
        },
    },
)


@dataclass
class Fighter:
    name: str
    x: int
    y: int
    facing: int
    hearts: int
    frags: int = 0               # eliminations scored (the headline points)
    terr: float = 0.0            # points earned from new ground, capped by territory_cap
    alive: bool = True
    move_cd: int = 0
    fire_cd: int = 0
    idle: int = 0                # ticks spent in the same cell; drives "exposed"
    exposed: bool = False        # idle too long: position broadcast to all enemies
    px: int = -1                 # cell at the previous tick, to detect movement
    py: int = -1
    visited: list = field(default_factory=list)   # [[x,y], ...] cells already scored for territory
    last_act: int = 0            # tick of the last submitted action; stale => client gone (dropped)
    last_hit: Optional[dict] = None  # most recent hit this round: {tick, dir, from}, or None


@dataclass
class Bullet:
    owner: str
    x: float
    y: float
    dx: float
    dy: float
    travelled: float = 0.0  # cells covered; despawns past fire_range


@dataclass
class State:
    cfg: dict[str, Any]
    grid: int
    walls: list[list[int]]       # [[x,y], ...] wall cells (incl. border)
    spawns: list[list[int]]      # spawn cells, one per slot index
    fighters: dict[str, Fighter]
    bullets: list[Bullet] = field(default_factory=list)
    round: int = 1
    phase: str = "fighting"      # "fighting" | "intermission"
    inter_cd: int = 0
    round_start: int = 0         # tick the current round began (for the round limit)
    tick: int = 0


# ---- maze generation ------------------------------------------------------

def _build_maze(cfg: dict[str, Any]) -> tuple[set[tuple[int, int]], list[tuple[int, int]]]:
    """Deterministic, relatively-open maze: a solid border plus scattered interior
    cover (single blocks and short segments). Returns (walls, spawn cells). Spawns
    are taken from the largest open region, spread apart."""
    g = cfg["grid"]
    rng = random.Random(cfg["seed"])
    walls: set[tuple[int, int]] = set()
    for i in range(g):
        walls.update({(i, 0), (i, g - 1), (0, i), (g - 1, i)})

    interior = [(x, y) for x in range(1, g - 1) for y in range(1, g - 1)]
    density = max(0.0, min(0.5, cfg["wall_density"]))   # clamp, defense in depth
    # Cap the wall count so at least max_players interior cells stay open: this
    # guarantees enough spread-out spawns and prevents an unfillable target.
    target = min(int(len(interior) * density), max(0, len(interior) - META.max_players))
    placed = 0
    attempts = 0
    while placed < target and attempts < target * 40 + 100:   # bounded: always terminates
        attempts += 1
        x, y = rng.choice(interior)
        seg = rng.choice([1, 1, 2, 3])              # mostly singles, some short walls
        horiz = rng.random() < 0.5
        for k in range(seg):
            if placed >= target:
                break
            cx = x + (k if horiz else 0)
            cy = y + (0 if horiz else k)
            if 1 <= cx < g - 1 and 1 <= cy < g - 1 and (cx, cy) not in walls:
                walls.add((cx, cy))
                placed += 1

    # Largest connected open region (4-connectivity), via flood fill.
    open_cells = {(x, y) for x in range(g) for y in range(g) if (x, y) not in walls}
    best: set[tuple[int, int]] = set()
    seen: set[tuple[int, int]] = set()
    for start in open_cells:
        if start in seen:
            continue
        stack, comp = [start], set()
        while stack:
            c = stack.pop()
            if c in comp:
                continue
            comp.add(c)
            cx, cy = c
            for dx, dy in DIRS:
                n = (cx + dx, cy + dy)
                if n in open_cells and n not in comp:
                    stack.append(n)
        seen |= comp
        if len(comp) > len(best):
            best = comp

    # Spawns: greedily pick cells far from those already chosen. Never empty (the
    # wall cap keeps open cells available); fall back defensively just in case.
    region = sorted(best) or sorted(open_cells) or [(1, 1)]
    spawns: list[tuple[int, int]] = [region[0]]
    while len(spawns) < META.max_players and len(spawns) < len(region):
        far = max(region, key=lambda c: min((c[0] - s[0]) ** 2 + (c[1] - s[1]) ** 2 for s in spawns))
        if far in spawns:
            break
        spawns.append(far)
    return walls, spawns


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _points(f: "Fighter", cfg: dict[str, Any]) -> float:
    """A player's standing: a point per elimination plus capped territory. Kills
    dominate (territory is capped below the score limit), so you cannot win by
    pacing alone, but a mover always outranks a camper on equal kills."""
    return f.frags + min(f.terr, cfg["territory_cap"])


class Skirmish:
    meta = META

    # -- setup -------------------------------------------------------------

    def init_state(self, config: dict[str, Any], players: list[PlayerSlot]) -> State:
        cfg = merge_defaults(self.meta, config)
        walls, spawns = _build_maze(cfg)
        fighters: dict[str, Fighter] = {}
        for i, p in enumerate(players):
            sx, sy = spawns[i % len(spawns)]
            fighters[p.player_id] = Fighter(name=p.display_name, x=sx, y=sy,
                                            facing=i % 4, hearts=cfg["hearts"])
        return State(
            cfg=cfg, grid=cfg["grid"],
            walls=[[x, y] for (x, y) in sorted(walls)],
            spawns=[[x, y] for (x, y) in spawns],
            fighters=fighters,
        )

    def add_player(self, state: State, slot: PlayerSlot) -> None:
        """Add a player to a running match at score 0 (frags/territory start fresh,
        so a late joiner is not handicapped beyond being behind on the board).
        Idempotent on player_id so a join retry cannot duplicate the fighter."""
        if slot.player_id in state.fighters:
            return
        i = len(state.fighters)
        sx, sy = state.spawns[i % len(state.spawns)]
        occupied = {(f.x, f.y) for f in state.fighters.values()}
        if (sx, sy) in occupied:                    # deterministic free cell if the spawn is taken
            walls = self._wallset(state)
            free = [(x, y) for x in range(state.grid) for y in range(state.grid)
                    if (x, y) not in walls and (x, y) not in occupied]
            if free:
                sx, sy = free[(i * 7) % len(free)]
        state.fighters[slot.player_id] = Fighter(name=slot.display_name, x=sx, y=sy,
                                                 facing=i % 4, hearts=state.cfg["hearts"],
                                                 last_act=state.tick)

    def active_players(self, state: State) -> set[str]:
        """player_ids still in the game. A dropped player (removed in tick) is gone,
        so the engine prunes them from the match roster."""
        return set(state.fighters.keys())

    # -- spatial helpers ---------------------------------------------------

    def _wallset(self, state: State) -> set[tuple[int, int]]:
        return {(x, y) for x, y in state.walls}

    def _occupied(self, state: State, exclude: str) -> set[tuple[int, int]]:
        return {(f.x, f.y) for pid, f in state.fighters.items()
                if f.alive and pid != exclude}

    def _blocked(self, state: State, cell: tuple[int, int], mover: str) -> bool:
        g = state.grid
        x, y = cell
        if not (0 <= x < g and 0 <= y < g):
            return True
        return cell in self._wallset(state) or cell in self._occupied(state, mover)

    def _los_clear(self, state: State, ax: int, ay: int, bx: int, by: int) -> bool:
        """Line of sight between two cell centers: no wall strictly between them."""
        walls = self._wallset(state)
        steps = max(abs(bx - ax), abs(by - ay))
        if steps == 0:
            return True
        for i in range(1, steps):
            t = i / steps
            cx, cy = round(ax + (bx - ax) * t), round(ay + (by - ay) * t)
            if (cx, cy) in walls:
                return False
        return True

    # -- rules -------------------------------------------------------------

    def validate(self, state: State, player_id: str, action: dict[str, Any]) -> Optional[str]:
        f = state.fighters[player_id]
        t = action.get("type")
        if not f.alive:
            return "you are eliminated this round"
        if state.phase != "fighting":
            return "round is between phases; wait"
        if t == "wait":
            return None
        if t == "turn":
            if action.get("to") not in ("left", "right", "around"):
                return "turn.to must be left, right, or around"
            return None
        if t == "move":
            d = action.get("dir")
            if d not in ("forward", "backward"):
                return "move.dir must be forward or backward"
            if f.move_cd > 0:
                return "still moving (on cooldown)"
            dx, dy = DIRS[f.facing]
            if d == "backward":
                dx, dy = -dx, -dy
            if self._blocked(state, (f.x + dx, f.y + dy), player_id):
                return "blocked by a wall or another character"
            return None
        if t == "fire":
            if f.fire_cd > 0:
                return "gun is reloading"
            return None
        return f"unknown action type: {t!r}"

    def apply(self, state: State, player_id: str, action: dict[str, Any]) -> None:
        f = state.fighters[player_id]
        f.last_act = state.tick      # any action (incl. wait) proves the client is alive
        t = action["type"]
        if t == "turn":
            f.facing = (f.facing + {"left": -1, "right": 1, "around": 2}[action["to"]]) % 4
        elif t == "move":
            dx, dy = DIRS[f.facing]
            if action["dir"] == "backward":
                dx, dy = -dx, -dy
            f.x += dx
            f.y += dy
            f.move_cd = state.cfg["move_cooldown"]
            # Territory: first time on this cell (and not yet capped) earns a sliver.
            # The cap also bounds how many cells we remember, keeping state small.
            if f.terr < state.cfg["territory_cap"] and [f.x, f.y] not in f.visited:
                f.visited.append([f.x, f.y])
                f.terr += state.cfg["cell_bonus"]
        elif t == "fire":
            dx, dy = DIRS[f.facing]
            state.bullets.append(Bullet(owner=player_id, x=float(f.x), y=float(f.y),
                                        dx=float(dx), dy=float(dy)))
            f.fire_cd = state.cfg["fire_cooldown"]

    def tick(self, state: State, dt: float) -> None:
        cfg = state.cfg
        expose = cfg["expose_ticks"]
        drop = cfg["drop_after"]
        dropped: list[str] = []
        for pid, f in state.fighters.items():
            if f.move_cd > 0:
                f.move_cd -= 1
            if f.fire_cd > 0:
                f.fire_cd -= 1
            # Drop a gone client: an ALIVE fighter that submits no action for
            # drop_after ticks during a fight is a disconnected client, remove it
            # from the game (no phantom body). A dead fighter waiting to respawn
            # legitimately sends nothing, so it is never dropped; it gets a fresh
            # grace window on respawn (_new_round resets last_act). If a dropped
            # player reconnects they re-join as a fresh player at score 0.
            if drop and f.alive and state.phase == "fighting" and state.tick - f.last_act >= drop:
                dropped.append(pid)
                continue
            # Idle tracking: staying in one cell raises idle; moving resets it. Past
            # expose_ticks the fighter is "exposed" and broadcast to all enemies.
            if (f.x, f.y) == (f.px, f.py):
                f.idle += 1
            else:
                f.idle = 0
                f.px, f.py = f.x, f.y
            f.exposed = bool(expose) and f.alive and f.idle >= expose
        for pid in dropped:
            del state.fighters[pid]

        walls = self._wallset(state)
        speed = cfg["bullet_speed"]
        alive_bullets: list[Bullet] = []
        for b in state.bullets:
            b.x += b.dx * speed * dt
            b.y += b.dy * speed * dt
            b.travelled += speed * dt
            cell = (round(b.x), round(b.y))
            if b.travelled > cfg["fire_range"] or cell in walls:
                continue
            hit = False
            for pid, f in state.fighters.items():
                if pid == b.owner or not f.alive:
                    continue
                if (f.x, f.y) == cell:
                    f.hearts -= 1
                    # Record where the shot came from so the victim can react: dir is
                    # the bullet's absolute travel direction; from is the source's
                    # bearing relative to the victim's facing (it came from behind the
                    # travel direction). This is the only cue for fire outside the cone.
                    tdir = _dir_index(b.dx, b.dy)
                    f.last_hit = {"tick": state.tick, "dir": DIR_NAMES[tdir],
                                  "from": REL_NAMES[((tdir + 2) - f.facing) % 4]}
                    if f.hearts <= 0:
                        f.alive = False
                        killer = state.fighters.get(b.owner)
                        if killer is not None:
                            killer.frags += 1   # the kill is the headline point
                    hit = True
                    break
            if not hit:
                alive_bullets.append(b)
        state.bullets = alive_bullets

        self._resolve_round(state)
        state.tick += 1

    def _resolve_round(self, state: State) -> None:
        cfg = state.cfg
        if state.phase == "fighting":
            alive = [f for f in state.fighters.values() if f.alive]
            # Eliminations are scored as they land (see tick). A round resets only
            # to respawn the downed and keep the hunt going; surviving a round, or
            # outlasting a stalled one, is worth nothing on its own. round_limit
            # (off by default) just force-resets a round that has stalled.
            timed_out = cfg["round_limit"] > 0 and state.tick - state.round_start >= cfg["round_limit"]
            if (len(alive) <= 1 and len(state.fighters) >= 2) or timed_out:
                state.phase = "intermission"
                state.inter_cd = cfg["intermission"]
                state.bullets = []
            return
        # intermission
        state.inter_cd -= 1
        if state.inter_cd <= 0:
            if max((_points(f, cfg) for f in state.fighters.values()), default=0) >= cfg["score_to_win"]:
                return  # leave finished; result() ends the match
            self._new_round(state)

    def _new_round(self, state: State) -> None:
        for i, (pid, f) in enumerate(state.fighters.items()):
            sx, sy = state.spawns[i % len(state.spawns)]
            f.x, f.y, f.facing = sx, sy, i % 4
            f.hearts = state.cfg["hearts"]
            f.alive = True
            f.move_cd = f.fire_cd = 0
            f.idle, f.exposed, f.px, f.py = 0, False, sx, sy   # frags/terr persist across rounds
            f.last_hit = None                                  # a new round clears stale damage cues
            f.last_act = state.tick                            # fresh drop grace each round
        state.bullets = []
        state.round += 1
        state.phase = "fighting"
        state.round_start = state.tick

    # -- views -------------------------------------------------------------

    def _sees(self, walls: set[tuple[int, int]], ax: int, ay: int, bx: int, by: int) -> bool:
        """Line of sight from cell (ax,ay) to (bx,by): no wall strictly between
        them. The target cell itself is not counted, so a wall is visible (you see
        its face) but everything behind it is occluded. Sampled finely so the cone
        edges shadow correctly."""
        dx, dy = bx - ax, by - ay
        n = max(abs(dx), abs(dy))
        if n == 0:
            return True
        steps = n * 4
        for i in range(1, steps):
            t = i / steps
            cx, cy = round(ax + dx * t), round(ay + dy * t)
            if (cx, cy) != (bx, by) and (cx, cy) in walls:
                return False
        return True

    def _vision(self, state: State, f: Fighter) -> dict[str, Any]:
        """The expanding forward cone the agent sees, in its own frame. At forward
        distance d the agent sees the row of 2d+1 cells (d=1 -> 3 cells, d=2 -> 5,
        d=3 -> 7, ...) centred on its facing, out to `sight`, minus any cell whose
        line of sight is blocked by a wall. Coordinates are facing-relative:
        `forward` (1..sight ahead) and `right` (negative is left)."""
        cfg = state.cfg
        g = state.grid
        walls = self._wallset(state)
        fwd = DIRS[f.facing]
        rt = DIRS[(f.facing + 1) % 4]     # the agent's right-hand direction
        enemy_at = {(e.x, e.y): e.name for pid, e in state.fighters.items()
                    if e is not f and e.alive}
        cells: list[dict[str, Any]] = []
        enemies: list[dict[str, Any]] = []
        for d in range(1, cfg["sight"] + 1):
            for r in range(-d, d + 1):     # width 2d+1, centred on the forward axis
                cx = f.x + fwd[0] * d + rt[0] * r
                cy = f.y + fwd[1] * d + rt[1] * r
                if not (0 <= cx < g and 0 <= cy < g):
                    continue
                if not self._sees(walls, f.x, f.y, cx, cy):
                    continue               # behind a wall: not visible
                if (cx, cy) in walls:
                    cells.append({"forward": d, "right": r, "x": cx, "y": cy, "what": "wall"})
                elif (cx, cy) in enemy_at:
                    name = enemy_at[(cx, cy)]
                    cells.append({"forward": d, "right": r, "x": cx, "y": cy, "what": "enemy", "name": name})
                    enemies.append({"name": name, "forward": d, "right": r,
                                    "distance": round(math.hypot(cx - f.x, cy - f.y), 2),
                                    "bearing": "ahead" if r == 0 else ("right" if r > 0 else "left")})
                else:
                    cells.append({"forward": d, "right": r, "x": cx, "y": cy, "what": "empty"})
        enemies.sort(key=lambda e: e["distance"])
        # Convenience scalars derived from the cone (the central column).
        wall_ahead, cx, cy = 0, f.x, f.y
        for _ in range(cfg["sight"]):
            cx, cy = cx + fwd[0], cy + fwd[1]
            if not (0 <= cx < g and 0 <= cy < g) or (cx, cy) in walls:
                break
            wall_ahead += 1
        return {"cells": cells, "enemies": enemies,
                "wall_ahead": wall_ahead, "forward_clear": wall_ahead >= 1}

    def observe(self, state: State, player_id: str) -> dict[str, Any]:
        cfg = state.cfg
        f = state.fighters[player_id]
        view = self._vision(state, f) if f.alive else {"cells": [], "enemies": [], "wall_ahead": 0, "forward_clear": False}
        # Exposed enemies are broadcast to everyone, ignoring walls and the cone, so a
        # camper cannot hide. Your own `exposed` flag warns you that you are lit up.
        view["pinged"] = [{"name": e.name, "x": e.x, "y": e.y}
                          for pid, e in state.fighters.items()
                          if pid != player_id and e.alive and e.exposed]
        # In-flight bullets within sight, including from behind or beside you (not
        # cone-gated, not occluded), so dodging is possible. Dead fighters see none.
        sight = cfg["sight"]
        view["bullets"] = ([{"x": round(b.x, 2), "y": round(b.y, 2),
                             "dx": round(b.dx, 2), "dy": round(b.dy, 2)}
                            for b in state.bullets
                            if math.hypot(b.x - f.x, b.y - f.y) <= sight] if f.alive else [])
        return {
            # The move and fire cooldowns are independent: you can fire while the move
            # cooldown is active (shoot-and-scoot) and vice versa, so can_fire tracks
            # only the gun's own cooldown.
            "you": {"x": f.x, "y": f.y, "facing": DIR_NAMES[f.facing], "hearts": f.hearts,
                    "alive": f.alive, "can_move": f.move_cd == 0, "can_fire": f.fire_cd == 0,
                    "score": round(_points(f, cfg), 2), "frags": f.frags,
                    "territory": round(f.terr, 2), "exposed": f.exposed,
                    "camp_ticks": f.idle, "expose_at": cfg["expose_ticks"],
                    "last_hit": f.last_hit},
            "view": view,
            "round": state.round,
            "phase": state.phase,
            "scores": {f.name: round(_points(f, cfg), 2) for f in state.fighters.values()},
        }

    def render(self, state: State) -> dict[str, Any]:
        return {
            "grid": state.grid,
            "walls": state.walls,
            "round": state.round,
            "phase": state.phase,
            "score_to_win": state.cfg["score_to_win"],
            "hearts_max": state.cfg["hearts"],
            "players": [
                {"id": pid, "name": f.name, "x": f.x, "y": f.y,
                 "dx": DIRS[f.facing][0], "dy": DIRS[f.facing][1],
                 "hearts": f.hearts, "alive": f.alive, "exposed": f.exposed,
                 "score": round(_points(f, state.cfg), 2), "frags": f.frags}
                for pid, f in state.fighters.items()
            ],
            "bullets": [{"x": round(b.x, 2), "y": round(b.y, 2)} for b in state.bullets],
        }

    def result(self, state: State) -> Optional[MatchResult]:
        scores = {f.name: round(_points(f, state.cfg), 2) for f in state.fighters.values()}
        by_pid = {pid: _points(f, state.cfg) for pid, f in state.fighters.items()}
        top = max(by_pid.values(), default=0.0)
        # Points (frags + capped territory) accumulate mid-round, so the game ends the
        # instant a fighter reaches the limit, regardless of phase. The climb is visible.
        if top >= state.cfg["score_to_win"]:
            return MatchResult(finished_tick=state.tick,
                               winners=[pid for pid, s in by_pid.items() if s == top],
                               scores=scores, reason="reached the score limit")
        # Safety cap so a stalled match cannot run forever.
        if state.tick >= state.cfg["score_to_win"] * 6000:
            return MatchResult(finished_tick=state.tick,
                               winners=[pid for pid, s in by_pid.items() if s == top],
                               scores=scores, reason="time cap reached")
        return None

    # -- persistence -------------------------------------------------------

    def encode_state(self, state: State) -> dict[str, Any]:
        return {
            "cfg": state.cfg, "grid": state.grid, "walls": state.walls, "spawns": state.spawns,
            "fighters": {pid: asdict(f) for pid, f in state.fighters.items()},
            "bullets": [asdict(b) for b in state.bullets],
            "round": state.round, "phase": state.phase, "inter_cd": state.inter_cd,
            "round_start": state.round_start, "tick": state.tick,
        }

    def decode_state(self, data: dict[str, Any]) -> State:
        return State(
            cfg=data["cfg"], grid=data["grid"], walls=data["walls"], spawns=data["spawns"],
            fighters={pid: Fighter(**f) for pid, f in data["fighters"].items()},
            bullets=[Bullet(**b) for b in data["bullets"]],
            round=data["round"], phase=data["phase"], inter_cd=data["inter_cd"],
            round_start=data.get("round_start", 0), tick=data["tick"],
        )


register(Skirmish())

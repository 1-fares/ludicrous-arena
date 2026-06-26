"""Skirmish -- a grid-maze tactical shooter, the headline game.

Characters live on a grid of open cells and walls. Each one occupies a cell and
faces one of four directions. They move a cell at a time, turn in 90-degree steps,
and can only *see* forward through a narrow cone, so walls are cover and position
is everything. Shots travel a few cells then fade. Three hits and you are down,
lying on the floor and out of the round. A round goes to the last fighter standing;
the match goes to the first to `rounds_to_win` round-wins. Eliminations score frags
(the scoreboard and the round-win tiebreak) but no longer decide the match on their
own. Downed and fallen fighters both respawn each new round.

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
    rounds_to_win:   int = 10      round-wins to take the match (first to N)
    score_to_win:    int = 10      scoreboard target (display only; does not end the match)
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
    drop_after:      int = 0       ticks with no action before an idle client is evicted (0=never, the default)
    collapse:        bool = True   collapsing floor: the arena falls in from the outer ring each round
    collapse_start:  int = 120     grace ticks each round before the outer ring starts cracking
    ring_interval:   int = 70      ticks between successive rings beginning to decay
    decay_ticks:     int = 40      ticks a tile spends visibly cracking before it falls
    decay_stages:    int = 4       number of visible crack stages (0..decay_stages-1)
    keep_rings:      int = 2       innermost rings that never collapse (the core)

The collapsing floor (`collapse=true`) shrinks the arena each round. A cell's ring is
its distance from the grid edge (ring 0 is the outermost row/column). There is no
border wall: the collapsing void forms the arena edge and the grid bounds stop a
fighter leaving the board. Outer rings crack then fall to the void on a fixed,
deterministic schedule measured from the round start; the innermost `keep_rings` never
fall. Walls fall on the same ring schedule as the floor: a wall still blocks movement
and vision while it is solid or cracking, but once its ring falls it becomes a void
hole like any other. A void tile (fallen floor or fallen wall) is a hole: not walkable,
bullets despawn entering it, and it does not block vision (you see across it). Any
fighter on a tile when it falls, alive or already downed, drops into the void and is
OUT for the current round: it scores nothing further that round and, like a downed
fighter, respawns at the start of the next round.
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
            "rounds_to_win": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100,
                              "description": "Round-wins needed to take the match (first to N). A round goes to the last fighter standing; the match ends when a fighter reaches this many round-wins."},
            "score_to_win": {"type": "integer", "default": 10, "minimum": 1, "maximum": 100,
                             "description": "Scoreboard target for frags + capped territory. Display only: it no longer ends the match (rounds_to_win does)."},
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
            "drop_after": {"type": "integer", "default": 0, "minimum": 0, "maximum": 100000,
                           "description": "Ticks with no submitted action before an idle client is evicted from the match. 0 (the default) disables it: a present player is never removed for inactivity, only a value > 0 evicts."},
            "collapse": {"type": "boolean", "default": True,
                         "description": "Enable the collapsing floor: each round the arena falls in from the outer ring. A fighter caught on a tile when it falls is out for the current round (0 points that round) and respawns next round, so the collapse forces every round to resolve. False keeps the floor solid (old behaviour)."},
            "collapse_start": {"type": "integer", "default": 120, "minimum": 0, "maximum": 1_000_000,
                               "description": "Grace ticks each round before the outermost ring starts to crack (timing is relative to the round start, so every round begins solid)."},
            "ring_interval": {"type": "integer", "default": 70, "minimum": 1, "maximum": 1_000_000,
                              "description": "Ticks between successive rings (outer to inner) beginning to decay."},
            "decay_ticks": {"type": "integer", "default": 40, "minimum": 1, "maximum": 1_000_000,
                            "description": "Ticks a tile spends visibly cracking before it falls into the void."},
            "decay_stages": {"type": "integer", "default": 4, "minimum": 1, "maximum": 8,
                             "description": "Number of visible crack stages (0..decay_stages-1) a tile passes through while decaying."},
            "keep_rings": {"type": "integer", "default": 2, "minimum": 1, "maximum": 1000,
                           "description": "Innermost rings that never collapse, so a solid core always remains to fight on and a round can resolve."},
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
                    "out": {"type": "boolean", "description": "You fell into the void this round: out for the rest of the current round (score 0 further this round). You respawn at the start of the next round, like a downed fighter. Round-scoped, cleared on respawn."},
                    "out_reason": {"type": ["string", "null"], "description": "\"fell into the void this round\" when out, else null."},
                    "fell_tick": {"type": ["integer", "null"], "description": "Absolute tick you fell into the void this round, or null if you have not fallen this round. Cleared on respawn."},
                    "round_wins": {"type": "integer", "description": "Rounds you have won this match. The match ends when a fighter reaches rounds_to_win (see the top-level match block)."},
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
                                "what": {"enum": ["wall", "empty", "enemy", "void"], "description": "void is a fallen floor or wall tile: not walkable, bullets despawn entering it, but it does not block vision (you see across the hole). Walls fall on the same ring schedule as the floor; a wall that has not yet fallen still shows as wall (and may be cracking)."},
                                "name": {"type": "string", "description": "Present when what == enemy."},
                                "hp": {"enum": ["full", "wounded", "critical"], "description": "Coarse health of the enemy on this cell (present when what == enemy). full = untouched, critical = one heart left, wounded = in between."},
                                "decay": {"type": "integer", "description": "Crack stage of this cell, 0 (solid) .. decay_stages-1 (about to fall). Present on empty, enemy, and wall cells (walls fall on the same schedule); absent on void."},
                                "falls_in": {"type": ["integer", "null"], "description": "Ticks until this cell becomes void, or null if it never falls (kept core, or collapse disabled). Present on empty, enemy, and wall cells."},
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
                                "hp": {"enum": ["full", "wounded", "critical"], "description": "Coarse health: full = untouched, critical = one heart left, wounded = in between."},
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
                            "x": {"type": "integer"}, "y": {"type": "integer"},
                            "hp": {"enum": ["full", "wounded", "critical"], "description": "Coarse health of the exposed enemy."}}},
                    },
                },
            },
            "arena": {
                "type": "object",
                "description": "Public state of the collapsing floor (environmental, not fog-gated). Use it to steer toward the centre before the edge falls.",
                "properties": {
                    "collapsing": {"type": "boolean", "description": "True if the collapsing floor is enabled (config.collapse)."},
                    "round_elapsed": {"type": "integer", "description": "Ticks since the current round began (collapse timing is measured from here)."},
                    "rings_total": {"type": "integer", "description": "Number of concentric floor rings, 0 (outermost) .. rings_total-1 (centre)."},
                    "keep_rings": {"type": "integer", "description": "Innermost rings that never collapse."},
                    "safe_ring": {"type": "integer", "description": "Outermost ring not yet fully fallen: the current edge of solid ground. Rings with a lower index than this are gone."},
                    "next_fall_tick": {"type": ["integer", "null"], "description": "Absolute tick the next ring fully falls, or null if only the kept core remains (or collapse is disabled)."},
                    "center": {"type": "array", "items": {"type": "integer"}, "description": "[cx, cy] arena centre cell, the safest point."},
                },
            },
            "round": {"type": "integer"},
            "phase": {"enum": ["fighting", "intermission"]},
            "match": {
                "type": "object",
                "description": "Match progress toward the first-to-N round-wins finish.",
                "properties": {
                    "round": {"type": "integer", "description": "Current round number (1-based)."},
                    "rounds_to_win": {"type": "integer", "description": "Round-wins needed to take the match."},
                    "round_wins": {"type": "object", "description": "Rounds won so far, per display name.",
                                   "additionalProperties": {"type": "integer"}},
                },
            },
            "scores": {"type": "object", "description": "Standing (frags + capped territory) per display name. Values are numbers: territory adds a fractional bonus. A fighter that is out (fallen) this round scores 0 until it respawns next round.",
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
    out: bool = False            # fell into the void this round: out for the round, scores 0; respawns next round
    fell_tick: Optional[int] = None  # absolute tick the fighter fell this round, or None
    round_wins: int = 0          # rounds won this match; first to rounds_to_win takes the match


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
    walls: list[list[int]]       # [[x,y], ...] interior cover cells (no border ring)
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
    """Deterministic, relatively-open maze: scattered interior cover (single blocks
    and short segments), with no border wall. Returns (walls, spawn cells). Spawns are
    taken from the largest open region, spread apart, and never on ring 0 (the
    outermost row/column, the first ground to collapse)."""
    g = cfg["grid"]
    rng = random.Random(cfg["seed"])
    walls: set[tuple[int, int]] = set()
    # No border ring: the collapsing void forms the arena edge, and the grid bounds
    # (0 <= x < g) already stop a fighter leaving the board, so a perimeter wall is
    # redundant. Cover stays interior only (off the outermost row and column).
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

    # Spawns: greedily pick cells far from those already chosen. Exclude ring 0 (the
    # outermost row/column, the first ground to collapse) so no fighter starts on
    # ground that falls early; fall back to the full region only if nothing is left.
    pool = sorted(best) or sorted(open_cells)
    inner = [c for c in pool if min(c[0], c[1], g - 1 - c[0], g - 1 - c[1]) >= 1]
    region = inner or pool or [(1, 1)]
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
    """A player's standing: a point per elimination plus capped territory. Used for the
    scoreboard and as the round-win tiebreak when a round ends with nobody in-round; it
    no longer decides the match (round-wins do). A fighter that is out (fell into the
    void) this round scores 0 until it respawns next round."""
    if f.out:
        return 0.0
    return f.frags + min(f.terr, cfg["territory_cap"])


def _coarse_hp(hearts: int, hearts_max: int) -> str:
    """Bucket exact hearts into a coarse health read (full / wounded / critical) so a
    sighting leaks the shape of an enemy's health, not its exact value."""
    if hearts >= hearts_max:
        return "full"
    if hearts <= 1:
        return "critical"
    return "wounded"


def _rings(state: "State") -> tuple[dict[tuple[int, int], int], int, tuple[int, int]]:
    """Map each non-wall floor cell to its ring index: the Chebyshev distance from the
    grid edge, with ring 0 the outermost row/column and the index growing toward the
    centre. Purely geometric and deterministic. Returns (rings, rings_total, center).
    rings_total is computed from the grid alone (independent of where walls sit) so the
    kept core is stable; the centre cell is the highest ring."""
    g = state.grid
    walls = {(x, y) for x, y in state.walls}
    rings: dict[tuple[int, int], int] = {}
    for x in range(g):
        for y in range(g):
            if (x, y) in walls:
                continue
            rings[(x, y)] = min(x, g - 1 - x, y, g - 1 - y)
    rings_total = max(0, (g - 1) // 2)
    c = (g - 1) // 2
    return rings, rings_total, (c, c)


def _tile_status(cfg: dict[str, Any], ring: int, rings_total: int, e: int) -> tuple[str, int, Optional[int]]:
    """Status of a floor tile in ``ring`` at round elapsed ``e``. Pure function of
    (e, ring, config), nothing random or stored. Returns (phase, decay, falls_in):
    phase is "solid" | "cracking" | "void"; decay is the visible crack stage
    (0..decay_stages-1); falls_in is ticks until the tile becomes void, or None if
    it never falls (kept core, or collapse disabled)."""
    if not cfg.get("collapse", True):
        return "solid", 0, None
    keep_from = rings_total - cfg["keep_rings"]
    if ring >= keep_from:
        return "solid", 0, None          # innermost kept core: solid forever
    begin = cfg["collapse_start"] + ring * cfg["ring_interval"]
    fall = begin + cfg["decay_ticks"]
    if e < begin:
        return "solid", 0, fall - e
    if e < fall:
        stages = cfg["decay_stages"]
        stage = int((e - begin) / cfg["decay_ticks"] * stages)
        stage = max(0, min(stages - 1, stage))
        return "cracking", stage, fall - e
    return "void", cfg["decay_stages"] - 1, None


def _safe_ring(cfg: dict[str, Any], rings_total: int, e: int) -> int:
    """The outermost ring (lowest index) not yet fully fallen at elapsed ``e``: the
    current edge of solid ground. Rings below it are void; the kept core bounds it."""
    if not cfg.get("collapse", True):
        return 0
    keep_from = rings_total - cfg["keep_rings"]
    r = 0
    while r < keep_from:
        fall = cfg["collapse_start"] + r * cfg["ring_interval"] + cfg["decay_ticks"]
        if e >= fall:
            r += 1
        else:
            break
    return r


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
            g = state.grid
            free = [(x, y) for x in range(g) for y in range(g)
                    if (x, y) not in walls and (x, y) not in occupied]
            inner = [c for c in free if min(c[0], c[1], g - 1 - c[0], g - 1 - c[1]) >= 1]
            pick = inner or free                    # prefer ring >= 1, off the first ground to fall
            if pick:
                sx, sy = pick[(i * 7) % len(pick)]
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

    def _void_at(self, state: State, e: int) -> set[tuple[int, int]]:
        """Set of cells that are void (fallen) at round elapsed ``e``: every cell,
        floor OR wall, whose ring has collapsed by ``e``. Walls fall on the same ring
        schedule as the floor, so a wall on a fallen ring is a hole like any other.
        Derived from ring geometry and config, never stored."""
        cfg = state.cfg
        if not cfg.get("collapse", True):
            return set()
        g = state.grid
        rings_total = max(0, (g - 1) // 2)
        out: set[tuple[int, int]] = set()
        for x in range(g):
            for y in range(g):
                r = min(x, g - 1 - x, y, g - 1 - y)
                if _tile_status(cfg, r, rings_total, e)[0] == "void":
                    out.add((x, y))
        return out

    def _blocked(self, state: State, cell: tuple[int, int], mover: str) -> bool:
        g = state.grid
        x, y = cell
        if not (0 <= x < g and 0 <= y < g):
            return True
        if cell in self._wallset(state) or cell in self._occupied(state, mover):
            return True
        # A fallen tile is a hole: not walkable. Use the currently-observed elapsed.
        return cell in self._void_at(state, state.tick - state.round_start)

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
        # Tile status for the tick we are advancing into (state.tick increments at the
        # end), so void detection here matches what observe reports afterwards.
        void = self._void_at(state, state.tick - state.round_start + 1)
        speed = cfg["bullet_speed"]
        alive_bullets: list[Bullet] = []
        for b in state.bullets:
            b.x += b.dx * speed * dt
            b.y += b.dy * speed * dt
            b.travelled += speed * dt
            cell = (round(b.x), round(b.y))
            # A void cell is a hole: a bullet entering it despawns, same as a wall.
            if b.travelled > cfg["fire_range"] or cell in walls or cell in void:
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

        # Falling floor: every fighter on a tile that has just become void drops into
        # the hole, whether alive or already downed. Falling puts them OUT for the
        # current round (zero points the rest of this round); _new_round respawns them
        # next round, like a downed fighter. A downed body no longer hovers over a hole.
        # fell_tick is the absolute tick the fall takes effect (the post-increment tick).
        if void:
            for f in state.fighters.values():
                if not f.out and (f.x, f.y) in void:
                    f.alive = False
                    f.out = True
                    f.fell_tick = state.tick + 1

        self._resolve_round(state)
        state.tick += 1

    def _resolve_round(self, state: State) -> None:
        cfg = state.cfg
        if state.phase == "fighting":
            # In-round = still standing this round (alive implies not out/fallen, since
            # both downing and falling clear alive). The round ends when at most one
            # fighter is in-round; that fighter wins it (round_wins += 1). round_limit
            # (off by default) force-resets a stalled round, awarding nothing.
            alive = [f for f in state.fighters.values() if f.alive]
            natural_end = len(alive) <= 1 and len(state.fighters) >= 2
            timed_out = cfg["round_limit"] > 0 and state.tick - state.round_start >= cfg["round_limit"]
            if natural_end or timed_out:
                if natural_end:
                    self._award_round(state, alive)
                state.phase = "intermission"
                state.inter_cd = cfg["intermission"]
                state.bullets = []
            return
        # intermission
        state.inter_cd -= 1
        if state.inter_cd <= 0:
            if max((f.round_wins for f in state.fighters.values()), default=0) >= cfg["rounds_to_win"]:
                return  # leave finished; result() ends the match
            self._new_round(state)

    def _award_round(self, state: State, alive: list[Fighter]) -> None:
        """Credit the round-win when a round ends. The lone in-round survivor wins it.
        If nobody is in-round (all fell or died on the same tick), the round goes to the
        unique highest standing (_points); a tie awards no round-win (a drawn round)."""
        if len(alive) == 1:
            alive[0].round_wins += 1
            return
        if alive:
            return
        top = max((_points(f, state.cfg) for f in state.fighters.values()), default=0.0)
        leaders = [f for f in state.fighters.values() if _points(f, state.cfg) == top]
        if len(leaders) == 1:
            leaders[0].round_wins += 1

    def _new_round(self, state: State) -> None:
        for i, (pid, f) in enumerate(state.fighters.items()):
            # Falling is round-scoped: clear out/fell_tick and respawn the fighter, the
            # same as a downed fighter. round_wins/frags/terr persist across rounds.
            f.out = False
            f.fell_tick = None
            sx, sy = state.spawns[i % len(state.spawns)]
            f.x, f.y, f.facing = sx, sy, i % 4
            f.hearts = state.cfg["hearts"]
            f.alive = True
            f.move_cd = f.fire_cd = 0
            f.idle, f.exposed, f.px, f.py = 0, False, sx, sy   # frags/terr/round_wins persist across rounds
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
        e = state.tick - state.round_start
        # Walls fall on the same ring schedule as the floor. A standing wall (solid or
        # cracking) still blocks movement and line of sight; a fallen wall is a void
        # hole, so it neither blocks nor occludes. "walls" here is the STANDING set:
        # static walls minus any whose ring has already fallen. Void cells are holes:
        # they do not block line of sight and are reported as what == "void".
        void = self._void_at(state, e)
        walls = self._wallset(state) - void
        hearts_max = cfg["hearts"]
        rings_total = max(0, (g - 1) // 2)
        fwd = DIRS[f.facing]
        rt = DIRS[(f.facing + 1) % 4]     # the agent's right-hand direction
        enemy_at = {(en.x, en.y): en for pid, en in state.fighters.items()
                    if en is not f and en.alive}
        cells: list[dict[str, Any]] = []
        enemies: list[dict[str, Any]] = []
        for d in range(1, cfg["sight"] + 1):
            for r in range(-d, d + 1):     # width 2d+1, centred on the forward axis
                cx = f.x + fwd[0] * d + rt[0] * r
                cy = f.y + fwd[1] * d + rt[1] * r
                if not (0 <= cx < g and 0 <= cy < g):
                    continue
                if not self._sees(walls, f.x, f.y, cx, cy):
                    continue               # behind a standing wall (void does not occlude)
                phase, decay, falls_in = _tile_status(
                    cfg, min(cx, g - 1 - cx, cy, g - 1 - cy), rings_total, e)
                if (cx, cy) in walls:      # standing wall: blocks, and may be cracking
                    cells.append({"forward": d, "right": r, "x": cx, "y": cy, "what": "wall",
                                  "decay": decay, "falls_in": falls_in})
                    continue
                if phase == "void":        # fallen floor or fallen wall: a hole
                    cells.append({"forward": d, "right": r, "x": cx, "y": cy, "what": "void"})
                elif (cx, cy) in enemy_at:
                    en = enemy_at[(cx, cy)]
                    hp = _coarse_hp(en.hearts, hearts_max)
                    cells.append({"forward": d, "right": r, "x": cx, "y": cy, "what": "enemy",
                                  "name": en.name, "hp": hp, "decay": decay, "falls_in": falls_in})
                    enemies.append({"name": en.name, "forward": d, "right": r,
                                    "distance": round(math.hypot(cx - f.x, cy - f.y), 2),
                                    "bearing": "ahead" if r == 0 else ("right" if r > 0 else "left"),
                                    "hp": hp})
                else:
                    cells.append({"forward": d, "right": r, "x": cx, "y": cy, "what": "empty",
                                  "decay": decay, "falls_in": falls_in})
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
        view["pinged"] = [{"name": e.name, "x": e.x, "y": e.y,
                           "hp": _coarse_hp(e.hearts, cfg["hearts"])}
                          for pid, e in state.fighters.items()
                          if pid != player_id and e.alive and e.exposed]
        # In-flight bullets within sight, including from behind or beside you (not
        # cone-gated, not occluded), so dodging is possible. Dead fighters see none.
        sight = cfg["sight"]
        view["bullets"] = ([{"x": round(b.x, 2), "y": round(b.y, 2),
                             "dx": round(b.dx, 2), "dy": round(b.dy, 2)}
                            for b in state.bullets
                            if math.hypot(b.x - f.x, b.y - f.y) <= sight] if f.alive else [])
        # Public collapse summary: environmental, not fog-gated, so an agent can steer
        # for the centre. center is the safest cell; safe_ring is the current edge.
        _, rings_total, center = _rings(state)
        e = state.tick - state.round_start
        safe_ring = _safe_ring(cfg, rings_total, e)
        collapsing = bool(cfg.get("collapse", True))
        keep_from = rings_total - cfg["keep_rings"]
        next_fall_tick = (state.round_start + cfg["collapse_start"]
                          + safe_ring * cfg["ring_interval"] + cfg["decay_ticks"]) \
            if collapsing and safe_ring < keep_from else None
        arena = {"collapsing": collapsing, "round_elapsed": e, "rings_total": rings_total,
                 "keep_rings": cfg["keep_rings"], "safe_ring": safe_ring,
                 "next_fall_tick": next_fall_tick, "center": [center[0], center[1]]}
        return {
            # The move and fire cooldowns are independent: you can fire while the move
            # cooldown is active (shoot-and-scoot) and vice versa, so can_fire tracks
            # only the gun's own cooldown.
            "you": {"x": f.x, "y": f.y, "facing": DIR_NAMES[f.facing], "hearts": f.hearts,
                    "alive": f.alive, "can_move": f.move_cd == 0, "can_fire": f.fire_cd == 0,
                    "score": round(_points(f, cfg), 2), "frags": f.frags,
                    "territory": round(f.terr, 2), "exposed": f.exposed,
                    "camp_ticks": f.idle, "expose_at": cfg["expose_ticks"],
                    "last_hit": f.last_hit,
                    "out": f.out,
                    "out_reason": "fell into the void this round" if f.out else None,
                    "fell_tick": f.fell_tick,
                    "round_wins": f.round_wins},
            "view": view,
            "arena": arena,
            "round": state.round,
            "phase": state.phase,
            "match": {"round": state.round, "rounds_to_win": cfg["rounds_to_win"],
                      "round_wins": {f.name: f.round_wins for f in state.fighters.values()}},
            "scores": {f.name: round(_points(f, cfg), 2) for f in state.fighters.values()},
        }

    def render(self, state: State) -> dict[str, Any]:
        cfg = state.cfg
        g = state.grid
        e = state.tick - state.round_start
        rings_total = max(0, (g - 1) // 2)
        c = (g - 1) // 2
        wallset = self._wallset(state)
        # Walls and floor decay on the same ring schedule. The viewer treats any cell
        # not named here as solid floor. Standing walls (solid or cracking) are listed
        # in "walls"; a wall mid-decay also appears in "walls_cracking". Every fallen
        # cell, floor or former wall, is a hole and goes in floor.void; floor cells
        # mid-decay go in floor.cracking.
        walls_standing: list[list[int]] = []
        walls_cracking: list[dict[str, Any]] = []
        floor_cracking: list[dict[str, Any]] = []
        void: list[list[int]] = []
        for x in range(g):
            for y in range(g):
                phase, decay, falls_in = _tile_status(
                    cfg, min(x, g - 1 - x, y, g - 1 - y), rings_total, e)
                if phase == "void":
                    void.append([x, y])                       # every hole, floor or wall
                elif (x, y) in wallset:
                    walls_standing.append([x, y])
                    if phase == "cracking":
                        walls_cracking.append({"x": x, "y": y, "decay": decay, "falls_in": falls_in})
                elif phase == "cracking":
                    floor_cracking.append({"x": x, "y": y, "decay": decay, "falls_in": falls_in})
        return {
            "grid": state.grid,
            "walls": walls_standing,
            "round": state.round,
            "phase": state.phase,
            "score_to_win": cfg["score_to_win"],
            "rounds_to_win": cfg["rounds_to_win"],
            "hearts_max": cfg["hearts"],
            "match": {"round": state.round, "rounds_to_win": cfg["rounds_to_win"],
                      "round_wins": {f.name: f.round_wins for f in state.fighters.values()}},
            "players": [
                {"id": pid, "name": f.name, "x": f.x, "y": f.y,
                 "dx": DIRS[f.facing][0], "dy": DIRS[f.facing][1],
                 "hearts": f.hearts, "alive": f.alive, "exposed": f.exposed,
                 "score": round(_points(f, cfg), 2), "frags": f.frags,
                 "out": f.out, "fell_tick": f.fell_tick, "round_wins": f.round_wins}
                for pid, f in state.fighters.items()
            ],
            "bullets": [{"x": round(b.x, 2), "y": round(b.y, 2)} for b in state.bullets],
            "floor": {"cracking": floor_cracking, "void": void},
            "walls_cracking": walls_cracking,
            "collapse": {"on": bool(cfg.get("collapse", True)), "stages": cfg["decay_stages"],
                         "center": [c, c],
                         "safe_ring": _safe_ring(cfg, rings_total, e)},
        }

    def result(self, state: State) -> Optional[MatchResult]:
        cfg = state.cfg
        scores = {f.name: round(_points(f, cfg), 2) for f in state.fighters.values()}
        n = cfg["rounds_to_win"]
        # First to N round-wins takes the match. Out (fallen) is round-scoped now, so it
        # is not a winner filter; round_wins decides outright. round_wins only changes
        # when a round resolves, so the match ends the instant a fighter reaches N.
        best = max((f.round_wins for f in state.fighters.values()), default=0)
        if state.fighters and best >= n:
            winners = [pid for pid, f in state.fighters.items() if f.round_wins == best]
            return MatchResult(finished_tick=state.tick, winners=winners, scores=scores,
                               reason=f"first to {n} round wins")
        # Safety cap so a stalled match cannot run forever: award to the round-win
        # leader(s); a 0-0 stall has no winner.
        if state.tick >= n * 6000:
            winners = [pid for pid, f in state.fighters.items()
                       if f.round_wins == best and best > 0]
            return MatchResult(finished_tick=state.tick, winners=winners, scores=scores,
                               reason="time cap reached")
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
        # Merge in config defaults so a match created before a config key existed
        # (e.g. rounds_to_win, the collapse knobs) decodes with the new defaults
        # filled in, instead of raising KeyError when the rules read a missing key.
        return State(
            cfg=merge_defaults(self.meta, data["cfg"]), grid=data["grid"], walls=data["walls"], spawns=data["spawns"],
            fighters={pid: Fighter(**f) for pid, f in data["fighters"].items()},
            bullets=[Bullet(**b) for b in data["bullets"]],
            round=data["round"], phase=data["phase"], inter_cd=data["inter_cd"],
            round_start=data.get("round_start", 0), tick=data["tick"],
        )


register(Skirmish())

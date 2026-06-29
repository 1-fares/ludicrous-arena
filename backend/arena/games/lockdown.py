"""Lockdown -- a cooperative escape room with fog of war.

The team is dropped into a dark grid. Data fragments are scattered around it;
the team wins by collecting every fragment and carrying them out through the
exit before the timer expires. Each agent can only see the 3x3 cells around its
own position (``observe`` enforces this), so the agents must *communicate* out of
band -- which is exactly the collaboration this whole project is built to provoke.

This is the second reference game: it shares nothing with deathmatch except the
:class:`Game` interface, which is the point. Cooperative mode, partial
observation, a shared win condition, and a timer-driven loss condition.

Actions:
    {"type": "move", "dir": "N" | "S" | "E" | "W"}   step one cell

Config (all optional):
    grid:        int = 8     side length
    fragments:   int = 4     fragments to collect
    time_limit_ticks: int = 1200   (~4 min at 5 tps)
    sight:       int = 1     fog radius (cells visible around the player)
    seed:        int = 1     deterministic layout seed
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional

from arena.config import merge_defaults
from arena.models import GameMeta, GameMode, MatchResult, PlayerSlot
from arena.registry import register

META = GameMeta(
    id="lockdown",
    title="Lockdown",
    description="Cooperative escape room. Collect every data fragment and reach the exit before the clock runs out. Fog of war: each agent sees only its surroundings.",
    mode=GameMode.cooperative,
    min_players=1,
    max_players=4,
    teams=[],
    tick_rate=5.0,
    config_schema={
        "type": "object",
        "properties": {
            "grid": {"type": "integer", "default": 8},
            "fragments": {"type": "integer", "default": 4},
            "time_limit_ticks": {"type": "integer", "default": 1200},
            "sight": {"type": "integer", "default": 1},
            "seed": {"type": "integer", "default": 1},
        },
    },
    action_schema={
        "type": "object", "required": ["type", "dir"],
        "properties": {"type": {"const": "move"},
                       "dir": {"enum": ["N", "S", "E", "W"]}},
    },
    observation_schema={
        "type": "object",
        "description": "Partial view: only cells within `sight` of your pawn. Coordinate frame: origin (0,0) is the top-left, x grows east (right), y grows south (down); the exit is the bottom-right cell (grid-1, grid-1). Move N decreases y, S increases y, E increases x, W decreases x. Agents see only their own surroundings, so coordinate the team out of band.",
        "properties": {
            "grid": {"type": "integer", "description": "Side length; cells are 0..grid-1 on each axis."},
            "sight": {"type": "integer", "description": "Fog radius (cells visible around you)."},
            "time_left": {"type": "integer", "description": "Ticks remaining before the timer expires and the team loses. The tick rate is 5/second."},
            "self": {"type": "object", "properties": {
                "player_id": {"type": "string", "description": "Your own pawn id (matches an id in another agent's nearby_players, and the envelope seat.player_id)."},
                "x": {"type": "integer"}, "y": {"type": "integer"},
                "carrying": {"type": "integer", "description": "Fragments you hold but have not yet delivered to the exit."}}},
            "visible": {"type": "object", "properties": {
                "fragments": {"type": "array", "description": "Uncollected fragment cells within sight.", "items": {"type": "object", "properties": {
                    "x": {"type": "integer"}, "y": {"type": "integer"}}}},
                "exit_visible": {"type": "boolean", "description": "True when the exit cell is within sight."},
                "exit": {"type": "object", "description": "The exit cell, present only when exit_visible is true. (It is always (grid-1, grid-1).)",
                         "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}},
                "nearby_players": {"type": "array", "description": "OTHER pawns within sight (you are excluded).", "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"},
                    "x": {"type": "integer"}, "y": {"type": "integer"},
                    "carrying": {"type": "integer", "description": "Fragments that teammate is carrying."}}}}}},
            "team": {"type": "object", "description": "Global team progress (no position leak). delivered + carried + uncollected == total.", "properties": {
                "delivered": {"type": "integer", "description": "Fragments carried out through the exit so far."},
                "total": {"type": "integer", "description": "Fragments to extract to win."},
                "carried": {"type": "integer", "description": "Fragments currently held by the team (sum across all pawns), not yet delivered."},
                "uncollected": {"type": "integer", "description": "Fragments still lying on the floor somewhere, not yet picked up."}}},
        },
    },
)

_DELTA = {"N": (0, -1), "S": (0, 1), "E": (1, 0), "W": (-1, 0)}


@dataclass
class Pawn:
    player_id: str
    name: str
    x: int
    y: int
    carrying: int = 0  # fragments held but not yet delivered


@dataclass
class State:
    cfg: dict[str, Any]
    grid: int
    exit: tuple[int, int]
    fragments: set[tuple[int, int]]  # uncollected fragment cells
    pawns: dict[str, Pawn]
    total: int
    delivered: int = 0
    tick: int = 0
    pending: dict[str, str] = field(default_factory=dict)  # player_id -> queued dir


class Lockdown:
    meta = META

    def init_state(self, config: dict[str, Any], players: list[PlayerSlot]) -> State:
        cfg = merge_defaults(self.meta, config)
        g = cfg["grid"]
        rng = random.Random(cfg["seed"])
        exit_cell = (g - 1, g - 1)
        frags: set[tuple[int, int]] = set()
        while len(frags) < cfg["fragments"]:
            c = (rng.randrange(g), rng.randrange(g))
            if c != exit_cell and c != (0, 0):
                frags.add(c)
        pawns = {p.player_id: Pawn(p.player_id, p.display_name, 0, 0) for p in players}
        return State(cfg=cfg, grid=g, exit=exit_cell, fragments=frags,
                     pawns=pawns, total=cfg["fragments"])

    def validate(self, state: State, player_id: str, action: dict[str, Any]) -> Optional[str]:
        if action.get("type") != "move":
            return f"unknown action type: {action.get('type')!r}"
        if action.get("dir") not in _DELTA:
            return "dir must be one of N,S,E,W"
        return None

    def apply(self, state: State, player_id: str, action: dict[str, Any]) -> None:
        # Record the latest move per player; resolution happens in tick() so all
        # pawns move simultaneously regardless of submission order.
        state.pending[player_id] = action["dir"]

    def tick(self, state: State, dt: float) -> None:
        g = state.grid
        for pid, d in state.pending.items():
            pawn = state.pawns[pid]
            dx, dy = _DELTA[d]
            pawn.x = min(g - 1, max(0, pawn.x + dx))
            pawn.y = min(g - 1, max(0, pawn.y + dy))
            cell = (pawn.x, pawn.y)
            if cell in state.fragments:
                state.fragments.discard(cell)
                pawn.carrying += 1
            if cell == state.exit and pawn.carrying:
                state.delivered += pawn.carrying
                pawn.carrying = 0
        state.pending = {}
        state.tick += 1

    def _visible(self, state: State, me: str, cx: int, cy: int) -> dict[str, Any]:
        r = state.cfg["sight"]
        frags = [{"x": x, "y": y} for (x, y) in state.fragments
                 if abs(x - cx) <= r and abs(y - cy) <= r]
        exit_visible = abs(state.exit[0] - cx) <= r and abs(state.exit[1] - cy) <= r
        # nearby_players are OTHER pawns within sight (you are excluded). Include each
        # one's carry count so the team can decide who should run fragments to the exit.
        others = [{"id": p.player_id, "name": p.name, "x": p.x, "y": p.y, "carrying": p.carrying}
                  for p in state.pawns.values()
                  if p.player_id != me and abs(p.x - cx) <= r and abs(p.y - cy) <= r]
        vis: dict[str, Any] = {"fragments": frags, "exit_visible": exit_visible,
                               "nearby_players": others}
        if exit_visible:    # give the actual cell, not just a boolean, so you can path to it
            vis["exit"] = {"x": state.exit[0], "y": state.exit[1]}
        return vis

    def observe(self, state: State, player_id: str) -> dict[str, Any]:
        p = state.pawns[player_id]
        # Team aggregates are global progress, not a fog-of-war position leak: every
        # fragment is either delivered, carried by someone, or still on the floor, so
        # delivered + carried + uncollected == total. They let any one agent tell how
        # many fragments are still out there without seeing where.
        carried = sum(q.carrying for q in state.pawns.values())
        return {
            "grid": state.grid,
            "sight": state.cfg["sight"],
            "time_left": max(0, state.cfg["time_limit_ticks"] - state.tick),
            "self": {"player_id": p.player_id, "x": p.x, "y": p.y, "carrying": p.carrying},
            "visible": self._visible(state, player_id, p.x, p.y),
            "team": {"delivered": state.delivered, "total": state.total,
                     "carried": carried, "uncollected": len(state.fragments)},
        }

    def render(self, state: State) -> dict[str, Any]:
        # Spectators see the whole room; that is the fun of watching blind agents.
        return {
            "grid": state.grid,
            "exit": {"x": state.exit[0], "y": state.exit[1]},
            "fragments": [{"x": x, "y": y} for (x, y) in state.fragments],
            "players": [{"id": p.player_id, "name": p.name, "x": p.x, "y": p.y,
                         "carrying": p.carrying} for p in state.pawns.values()],
            "delivered": state.delivered,
            "total": state.total,
            "time_left": max(0, state.cfg["time_limit_ticks"] - state.tick),
        }

    def encode_state(self, state: State) -> dict[str, Any]:
        # Sets and tuples are not JSON types; flatten them to lists.
        return {
            "cfg": state.cfg,
            "grid": state.grid,
            "exit": list(state.exit),
            "fragments": [list(c) for c in state.fragments],
            "pawns": {pid: {"player_id": p.player_id, "name": p.name,
                            "x": p.x, "y": p.y, "carrying": p.carrying}
                      for pid, p in state.pawns.items()},
            "total": state.total,
            "delivered": state.delivered,
            "tick": state.tick,
            "pending": state.pending,
        }

    def decode_state(self, data: dict[str, Any]) -> State:
        return State(
            cfg=data["cfg"],
            grid=data["grid"],
            exit=tuple(data["exit"]),
            fragments={tuple(c) for c in data["fragments"]},
            pawns={pid: Pawn(**p) for pid, p in data["pawns"].items()},
            total=data["total"],
            delivered=data["delivered"],
            tick=data["tick"],
            pending=data["pending"],
        )

    def result(self, state: State) -> Optional[MatchResult]:
        if state.delivered >= state.total:
            return MatchResult(finished_tick=state.tick,
                               winners=[p for p in state.pawns],
                               scores={"team": float(state.delivered)},
                               reason="all fragments extracted")
        if state.tick >= state.cfg["time_limit_ticks"]:
            return MatchResult(finished_tick=state.tick, winners=[],
                               scores={"team": float(state.delivered)},
                               reason="time expired -- the team is locked in")
        return None


register(Lockdown())

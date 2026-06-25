"""Deathmatch -- the reference game. Free-for-all top-down arena shooter.

Demonstrates every part of the framework: continuous movement, a fire cooldown,
projectiles with collision, health/respawn, scoring, and a win condition. The
arena is a square in the XZ plane; the viewer renders entities as cubes and
projectiles as small spheres.

Actions (one or more per tick):
    {"type": "move",  "dx": <-1..1>, "dy": <-1..1>}   set movement direction
    {"type": "fire",  "angle": <radians>}             shoot, if cooldown ready

Config (all optional):
    arena_size:        float = 20.0     side length of the square arena
    score_limit:       int   = 10       kills to win
    time_limit_ticks:  int   = 6000     hard cap (~10 min at 10 tps)
    move_speed:        float = 4.0      units/second
    projectile_speed:  float = 14.0     units/second
    fire_cooldown:     int   = 5        ticks between shots
    respawn_delay:     int   = 20       ticks dead before respawn
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from arena.config import merge_defaults
from arena.models import GameMeta, GameMode, MatchResult, PlayerSlot
from arena.registry import register


def _num(v: Any) -> bool:
    """A finite, non-bool number. JSON Schema `number` excludes booleans, and a
    non-finite float (inf/NaN) would crash math.cos or serialize to invalid JSON,
    so both are rejected at validation, never reaching apply/tick."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

META = GameMeta(
    id="deathmatch",
    title="Deathmatch",
    description="Free-for-all top-down arena shooter. First to the score limit wins.",
    mode=GameMode.deathmatch,
    min_players=2,
    max_players=8,
    teams=[],
    tick_rate=10.0,
    config_schema={
        "type": "object",
        "properties": {
            "arena_size": {"type": "number", "default": 20.0},
            "score_limit": {"type": "integer", "default": 10},
            "time_limit_ticks": {"type": "integer", "default": 6000},
            "move_speed": {"type": "number", "default": 4.0},
            "projectile_speed": {"type": "number", "default": 14.0},
            "fire_cooldown": {"type": "integer", "default": 5},
            "respawn_delay": {"type": "integer", "default": 20},
        },
    },
    action_schema={
        "oneOf": [
            {"type": "object", "required": ["type", "dx", "dy"],
             "properties": {"type": {"const": "move"},
                            "dx": {"type": "number"}, "dy": {"type": "number"}}},
            {"type": "object", "required": ["type", "angle"],
             "properties": {"type": {"const": "fire"}, "angle": {"type": "number"}}},
        ]
    },
    observation_schema={
        "type": "object",
        "description": "Full arena (no fog of war): same as the spectator render.",
        "properties": {
            "arena_size": {"type": "number"},
            "players": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "name": {"type": "string"},
                "x": {"type": "number"}, "y": {"type": "number"}, "heading": {"type": "number"},
                "hp": {"type": "integer"}, "score": {"type": "integer"}, "alive": {"type": "boolean"}}}},
            "projectiles": {"type": "array", "items": {"type": "object", "properties": {
                "x": {"type": "number"}, "y": {"type": "number"}}}},
            "scores": {"type": "object", "additionalProperties": {"type": "integer"}},
        },
    },
)

@dataclass
class Entity:
    player_id: str
    name: str
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    heading: float = 0.0
    hp: int = 100
    score: int = 0
    cooldown: int = 0
    dead_for: int = 0  # ticks remaining until respawn, 0 == alive


@dataclass
class Projectile:
    owner: str
    x: float
    y: float
    vx: float
    vy: float
    ttl: int  # ticks before it despawns


@dataclass
class State:
    cfg: dict[str, Any]
    entities: dict[str, Entity]
    projectiles: list[Projectile] = field(default_factory=list)
    tick: int = 0


class Deathmatch:
    meta = META

    def init_state(self, config: dict[str, Any], players: list[PlayerSlot]) -> State:
        cfg = merge_defaults(self.meta, config)
        size = cfg["arena_size"]
        n = len(players)
        ents: dict[str, Entity] = {}
        for i, p in enumerate(players):
            # Spread spawns evenly around a circle inscribed in the arena.
            a = 2 * math.pi * i / max(1, n)
            ents[p.player_id] = Entity(
                player_id=p.player_id,
                name=p.display_name,
                x=size / 2 + 0.35 * size * math.cos(a),
                y=size / 2 + 0.35 * size * math.sin(a),
            )
        return State(cfg=cfg, entities=ents)

    def validate(self, state: State, player_id: str, action: dict[str, Any]) -> Optional[str]:
        t = action.get("type")
        if t == "move":
            if not _num(action.get("dx")) or not _num(action.get("dy")):
                return "move requires finite numeric dx,dy"
            return None
        if t == "fire":
            if not _num(action.get("angle")):
                return "fire requires a finite numeric angle"
            e = state.entities[player_id]
            if e.dead_for > 0:
                return "cannot fire while dead"
            if e.cooldown > 0:
                return "weapon on cooldown"
            return None
        return f"unknown action type: {t!r}"

    def apply(self, state: State, player_id: str, action: dict[str, Any]) -> None:
        e = state.entities[player_id]
        if e.dead_for > 0:
            return
        speed = state.cfg["move_speed"]
        if action["type"] == "move":
            dx, dy = float(action["dx"]), float(action["dy"])
            mag = math.hypot(dx, dy)
            if mag > 0:
                e.vx, e.vy = dx / mag * speed, dy / mag * speed
                e.heading = math.atan2(dy, dx)
            else:
                e.vx = e.vy = 0.0
        elif action["type"] == "fire":
            ang = float(action["angle"])
            ps = state.cfg["projectile_speed"]
            state.projectiles.append(Projectile(
                owner=player_id, x=e.x, y=e.y,
                vx=math.cos(ang) * ps, vy=math.sin(ang) * ps, ttl=60,
            ))
            e.cooldown = state.cfg["fire_cooldown"]
            e.heading = ang

    def tick(self, state: State, dt: float) -> None:
        size = state.cfg["arena_size"]
        # entities
        for e in state.entities.values():
            if e.dead_for > 0:
                e.dead_for -= 1
                if e.dead_for == 0:
                    e.hp = 100
                continue
            if e.cooldown > 0:
                e.cooldown -= 1
            e.x = min(size, max(0.0, e.x + e.vx * dt))
            e.y = min(size, max(0.0, e.y + e.vy * dt))
        # projectiles + collision
        alive: list[Projectile] = []
        for pr in state.projectiles:
            pr.x += pr.vx * dt
            pr.y += pr.vy * dt
            pr.ttl -= 1
            if pr.ttl <= 0 or not (0 <= pr.x <= size and 0 <= pr.y <= size):
                continue
            hit = False
            for e in state.entities.values():
                if e.player_id == pr.owner or e.dead_for > 0:
                    continue
                if math.hypot(e.x - pr.x, e.y - pr.y) <= 0.6:  # player radius
                    e.hp -= 34
                    hit = True
                    if e.hp <= 0:
                        e.dead_for = state.cfg["respawn_delay"]
                        e.vx = e.vy = 0.0
                        state.entities[pr.owner].score += 1
                    break
            if not hit:
                alive.append(pr)
        state.projectiles = alive
        state.tick += 1

    def render(self, state: State) -> dict[str, Any]:
        return {
            "arena_size": state.cfg["arena_size"],
            "players": [
                {"id": e.player_id, "name": e.name, "x": round(e.x, 3), "y": round(e.y, 3),
                 "heading": round(e.heading, 3), "hp": e.hp, "score": e.score,
                 "alive": e.dead_for == 0}
                for e in state.entities.values()
            ],
            "projectiles": [{"x": round(p.x, 3), "y": round(p.y, 3)} for p in state.projectiles],
            "scores": {e.player_id: e.score for e in state.entities.values()},
        }

    def observe(self, state: State, player_id: str) -> dict[str, Any]:
        # No fog of war in deathmatch: agents see the whole arena, same as viewers.
        return self.render(state)

    def encode_state(self, state: State) -> dict[str, Any]:
        # Flat dataclasses: asdict recurses through the entities dict and the
        # projectiles list, producing a fully JSON-safe structure.
        return {
            "cfg": state.cfg,
            "entities": {pid: asdict(e) for pid, e in state.entities.items()},
            "projectiles": [asdict(p) for p in state.projectiles],
            "tick": state.tick,
        }

    def decode_state(self, data: dict[str, Any]) -> State:
        return State(
            cfg=data["cfg"],
            entities={pid: Entity(**e) for pid, e in data["entities"].items()},
            projectiles=[Projectile(**p) for p in data["projectiles"]],
            tick=data["tick"],
        )

    def result(self, state: State) -> Optional[MatchResult]:
        leader = max(state.entities.values(), key=lambda e: e.score, default=None)
        scores = {e.player_id: float(e.score) for e in state.entities.values()}
        if leader and leader.score >= state.cfg["score_limit"]:
            return MatchResult(finished_tick=state.tick, winners=[leader.player_id],
                               scores=scores, reason="score limit reached")
        if state.tick >= state.cfg["time_limit_ticks"]:
            top = max(scores.values(), default=0.0)
            return MatchResult(finished_tick=state.tick,
                               winners=[pid for pid, s in scores.items() if s == top],
                               scores=scores, reason="time limit reached")
        return None


register(Deathmatch())

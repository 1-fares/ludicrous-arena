"""Wire schemas, the contract an external agent codes against.

Every model here becomes a schema in the OpenAPI document, so fields carry
descriptions and the key request/response bodies carry examples. Game-specific
payloads (an action you submit, the observation you read, the spectator scene)
are intentionally open ``dict`` objects: their shape differs per game and is
published as JSON Schema in ``GameMeta.action_schema`` (read ``GET /v1/games``).
Keep these stable; add fields rather than renaming.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class GameMode(str, Enum):
    """Coarse classification of a game, for display and matchmaking filters."""

    deathmatch = "deathmatch"      # last-one-standing / score race, adversarial
    cooperative = "cooperative"    # players share a win condition (escape room)
    competitive = "competitive"    # scored, non-violent (resource / business games)
    scenario = "scenario"          # solve an industry problem; ranked by domain score
    sandbox = "sandbox"            # no win condition


class MatchPhase(str, Enum):
    """Lifecycle phase of a match."""

    lobby = "lobby"        # accepting joins, not yet started
    running = "running"    # in progress; submit actions and read state
    finished = "finished"  # result decided, state frozen


class GameMeta(BaseModel):
    """Static description of a game type. Returned by ``GET /v1/games``. The
    ``config_schema`` and ``action_schema`` are JSON Schema documents you can use
    to validate your requests before sending them."""

    id: str = Field(description="Stable game id, used as `game_id` when creating a match.", examples=["skirmish"])
    title: str = Field(description="Human-readable name.", examples=["Skirmish"])
    description: str = Field(description="One-paragraph summary of the game.")
    mode: GameMode = Field(description="Coarse classification of the game.")
    min_players: int = Field(default=1, description="Players required before the match can start.")
    max_players: int = Field(default=8, description="Maximum players the match accepts.")
    teams: list[str] = Field(default_factory=list, description="Team names, or empty for free-for-all.")
    tick_rate: float = Field(default=10.0, description="Simulation ticks per second.")
    realtime: bool = Field(default=True, description="True: the world advances on the wall clock. False: turn-paced (the world advances exactly one tick per submitted action batch).")
    config_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema for the `config` accepted by POST /v1/matches.")
    action_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema for the actions accepted by POST /v1/matches/{id}/actions.")
    observation_schema: dict[str, Any] = Field(default_factory=dict, description="JSON Schema for the `observation` field returned by GET /v1/matches/{id}/state for this game. This is the partial, per-agent view you code against.")


class PlayerSlot(BaseModel):
    """A participant in a match."""

    player_id: str = Field(description="Match-local id (e.g. `p1`). Stable for the match; this is what appears in observations and results.", examples=["p1"])
    user_id: str = Field(description="The user behind the slot (derived from the bearer token).")
    display_name: str = Field(description="Name shown in the spectator view, set at join time.", examples=["Hunter"])
    team: Optional[str] = Field(default=None, description="Assigned team, or null for free-for-all games.")


class MatchResult(BaseModel):
    """Terminal outcome of a match. Present once `phase` is `finished`. Note the
    keying: `winners` holds `player_id`s, while `scores` is keyed by display name."""

    finished_tick: int = Field(description="Tick at which the match ended.")
    winners: list[str] = Field(default_factory=list, description="Winning `player_id`s (or team names for team games).", examples=[["p3"]])
    scores: dict[str, float] = Field(default_factory=dict, description="Final score per display name (or team).", examples=[{"Hunter": 10.0, "Vega": 7.0}])
    reason: str = Field(default="", description="Why the match ended.", examples=["reached the score limit"])


class MatchInfo(BaseModel):
    """Match metadata. Returned by the discovery and lifecycle endpoints."""

    match_id: str = Field(description="Unique match id, used in all per-match endpoints.", examples=["ab12cd34ef56"])
    game_id: str = Field(description="Which game this match runs.", examples=["skirmish"])
    phase: MatchPhase
    tick: int = Field(default=0, description="Last recorded tick (0 until started; final tick once finished).")
    config: dict[str, Any] = Field(default_factory=dict, description="Effective config for this match.")
    autostart: bool = Field(default=True, description="Whether the match starts automatically once `min_players` have joined.")
    players: list[PlayerSlot] = Field(default_factory=list, description="Current roster.")
    max_players: int = Field(default=8, description="Maximum players the match accepts.")
    created_at: str = Field(description="ISO-8601 creation time (UTC).", examples=["2026-06-25T13:00:00Z"])
    result: Optional[MatchResult] = Field(default=None, description="Terminal result, or null until finished.")


# ---- request bodies -------------------------------------------------------

class CreateMatchRequest(BaseModel):
    """Body for POST /v1/matches."""

    game_id: str = Field(description="Game to host (see GET /v1/games).", examples=["skirmish"])
    config: dict[str, Any] = Field(default_factory=dict, description="Overrides for the game's config (see that game's `config_schema`). Omit to use defaults.", examples=[{"grid": 13, "score_to_win": 10}])
    autostart: bool = Field(default=True, description="If true, the match starts as soon as `min_players` have joined. If false, start it explicitly with POST /v1/matches/{id}/start.")


class JoinMatchRequest(BaseModel):
    """Body for POST /v1/matches/{id}/join."""

    display_name: Optional[str] = Field(default=None, description="Name shown above your character in the spectator view. Defaults to your token's name.", examples=["Hunter"])
    team: Optional[str] = Field(default=None, description="Preferred team (team games only); ignored for free-for-all.")


class ActionRequest(BaseModel):
    """Body for POST /v1/matches/{id}/actions. One or more game-specific actions,
    applied in order at the next tick. The action shape is per-game; see that
    game's `action_schema` (GET /v1/games)."""

    actions: list[dict[str, Any]] = Field(
        description="Actions to apply. Each is a game-specific object.",
        examples=[[{"type": "turn", "to": "left"}, {"type": "fire"}]],
    )


# ---- response bodies ------------------------------------------------------

class HealthResponse(BaseModel):
    """Liveness probe response."""

    ok: bool = Field(description="True when the server is healthy.")
    games: list[str] = Field(description="IDs of the registered game types.", examples=[["skirmish", "deathmatch", "lockdown", "trading_desk"]])


class YouBlock(BaseModel):
    """Who you are in the match, plus feedback on your last submission."""

    player_id: str = Field(description="Your match-local id.", examples=["p1"])
    team: Optional[str] = Field(default=None, description="Your team, or null for free-for-all.")
    rejected: list[str] = Field(default_factory=list, description="Reasons any actions from your previous submission were rejected (e.g. on cooldown, blocked by a wall).", examples=[["gun is reloading"]])


class StateResponse(BaseModel):
    """Your private view of a match, projected to 'now'. Poll this while playing.
    `observation` is game-specific and may be partial (fog of war)."""

    match_id: str
    tick: int = Field(description="Current simulation tick.")
    phase: MatchPhase
    you: YouBlock
    observation: dict[str, Any] = Field(description="Game-specific observation, often partial (fog of war). For its exact shape see that game's `observation_schema` from GET /v1/games.")
    result: Optional[MatchResult] = Field(default=None, description="Present once the match is finished, so you can detect your own outcome.")


class SceneResponse(BaseModel):
    """The full spectator render of a match, projected to 'now'. Public (no auth).
    This is what the three.js viewer polls. `scene` is game-specific and
    omniscient."""

    match_id: str
    tick: int
    phase: MatchPhase
    scene: Optional[dict[str, Any]] = Field(default=None, description="Game-specific full render, or null before the match has started.")
    result: Optional[MatchResult] = Field(default=None, description="Present once the match is finished.")


class ActionAck(BaseModel):
    """Acknowledgement for a submitted batch of actions."""

    queued: int = Field(description="Number of actions accepted in this submission.")
    tick: int = Field(description="Tick the submission was applied at.")


class ErrorResponse(BaseModel):
    """Standard error body."""

    detail: str = Field(description="Human-readable reason.", examples=["not a participant in this match"])


MatchInfo.model_rebuild()

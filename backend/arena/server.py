"""HTTP surface, the entire public API of the arena.

Runs three ways off the same app object:
  local dev:   uvicorn arena.server:app --reload        (ARENA_STORE=memory)
  Lambda:      handler = Mangum(app)                     (ARENA_STORE=dynamo)
  tests:       starlette TestClient against `app`

There is no WebSocket and no server loop: the world advances on demand inside
these request handlers (see engine.py). Spectators and agents poll. That is what
keeps the service at zero cost when nobody is playing.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware

from arena import auth, registry, store
from arena.engine import Engine, NotParticipant
from arena.models import (
    ActionAck,
    ActionRequest,
    CreateMatchRequest,
    ErrorResponse,
    GameMeta,
    HealthResponse,
    JoinMatchRequest,
    MatchInfo,
    SceneResponse,
    StateResponse,
)
from arena.store import _Identity

import arena.games  # noqa: F401  -- import side effect registers all bundled games

DESCRIPTION = """
**Ludicrous Arena** is a game server you play **through this API, not a
controller**. You point an agent (a script, an LLM loop, anything that speaks
HTTP) at a match with a secret token, and it drives one character. A separate
three.js page lets people *spectate*; nobody plays from it.

### The headline game: Skirmish

A grid-maze tactical shooter. Each character stands on a cell of a walled maze and
faces one of four directions.

- **Object**: hunt the others. Every enemy you shoot down scores a point, so the
  way to win is to find and eliminate people, not to hide. Two rules punish camping:
  sit in one cell too long and you are **exposed** (your position is broadcast to
  every enemy through walls until you move), and each **new cell** you enter earns a
  sliver of score (capped, so kills still decide).
- **Move** one cell at a time (forward or backward), **turn** in 90-degree steps.
  Walls and other characters block you; you cannot leave the grid.
- **See only forward**, through a narrow (~30-degree) cone: you learn the distance
  to the wall ahead and to any enemies in that cone with line of sight. There is
  no map, so you sense the maze by moving and looking.
- **Shoot** forward, at most once per second (unlimited ammo). A shot travels a
  few cells then fades. **Three hits** and you are eliminated for the round and
  lie on the floor.
- **A match is a series of rounds.** Each elimination you land scores +1, new ground
  a sliver more (`score = frags + capped territory`); the downed respawn at the next
  round, so surviving a round is worth nothing on its own. First to the score limit
  (default 10) wins the match, which then ends.

Other games are available too (a free-for-all deathmatch, a cooperative escape
room, a trading-desk scenario). List them all, with their per-game **action,
config, and observation** JSON Schemas, at `GET /v1/games`. Those schemas are the
source of truth for what you can send (`action_schema`) and what you will read
back (`observation_schema`).

### Authentication

Every play request carries your token:

```
Authorization: Bearer <your-token>
```

The token *is* your identity, there is no login or session. Discovery endpoints
(`/v1/games`, `/v1/matches`) and the spectator scene are public. Tokens are issued
out of band (see `scripts/issue-token.py`); a local dev server seeds `dev-token`
and `dev-token-2` .. `dev-token-4` for multi-agent play.

### How a match goes

1. `POST /v1/matches` to create one (or pick an open one from `GET /v1/matches`).
2. `POST /v1/matches/{id}/join`.
3. Wait until the match `phase` is `running` by polling the public
   `GET /v1/matches/{id}` (your private `/state` returns 409 until the match starts).
4. Loop: `GET /v1/matches/{id}/state` -> decide -> `POST /v1/matches/{id}/actions`,
   about once per tick. `state.you.player_id` is your id, `state.you.rejected` tells
   you why an action was dropped, and `state.observation` has the shape of that
   game's `observation_schema`.
5. Stop when `state.phase` is `finished`; `state.result` holds the winners.

The world is advanced *on demand* when you read or act, so polling always returns
the world as of now. A reference client is in `examples/python-agent/`. The whole
loop in `curl`:

```bash
H='-H "Authorization: Bearer dev-token" -H "Content-Type: application/json"'
MID=$(curl -s $H -d '{"game_id":"skirmish"}' localhost:8080/v1/matches | jq -r .match_id)
curl -s $H -d '{"display_name":"Hunter"}' localhost:8080/v1/matches/$MID/join
# ... once phase is "running":
curl -s $H localhost:8080/v1/matches/$MID/state
curl -s $H -d '{"actions":[{"type":"turn","to":"left"},{"type":"fire"}]}' \
     localhost:8080/v1/matches/$MID/actions
```
"""

TAGS = [
    {"name": "discovery", "description": "List games and open matches. No auth."},
    {"name": "match lifecycle", "description": "Create, inspect, join, and start matches."},
    {"name": "play", "description": "Read your view, read the spectator scene, submit actions."},
    {"name": "health", "description": "Liveness."},
]

app = FastAPI(
    title="Ludicrous Arena",
    version="0.1.0",
    summary="API-controlled multiplayer game arena. Drive a character with a token.",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    contact={"name": "Arena docs", "url": "https://github.com/"},
)

# The viewer is served from CloudFront, a different origin, and polls this API.
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

_STORE = store.from_env()
auth.set_store(_STORE)
ENGINE = Engine(_STORE)

# Reusable documented error responses.
E_AUTH = {401: {"model": ErrorResponse, "description": "Missing or invalid bearer token."}}
E_NOTFOUND = {404: {"model": ErrorResponse, "description": "Unknown game or match."}}
E_FORBIDDEN = {403: {"model": ErrorResponse, "description": "You are not a participant in this match."}}
E_CONFLICT = {409: {"model": ErrorResponse, "description": "Illegal state transition (e.g. joining a running match, or acting before it has started)."}}

_MATCH_ID = Path(description="Match id from POST /v1/matches.", examples=["ab12cd34ef56"])


@app.get("/healthz", response_model=HealthResponse, tags=["health"], summary="Liveness probe")
def healthz() -> dict[str, Any]:
    """Returns `ok: true` and the list of registered game ids. Used by the load
    balancer / Lambda health check."""
    return {"ok": True, "games": [g.meta.id for g in registry.all_games()]}


# -- discovery --------------------------------------------------------------

@app.get("/v1/games", response_model=list[GameMeta], tags=["discovery"],
         summary="List game types")
def list_games() -> list[GameMeta]:
    """Every available game with its metadata, including the JSON Schemas for its
    config and its actions. Code your client against these schemas. No auth."""
    return [g.meta for g in registry.all_games()]


@app.get("/v1/matches", response_model=list[MatchInfo], tags=["discovery"],
         summary="List matches")
def list_matches() -> list[MatchInfo]:
    """All known matches and their phase (`lobby` / `running` / `finished`). No
    auth. Use this to find an open match to join, or to drive a spectator UI."""
    return ENGINE.list_matches()


# -- match lifecycle --------------------------------------------------------

@app.post("/v1/matches", response_model=MatchInfo, tags=["match lifecycle"],
          summary="Create a match",
          responses={**E_AUTH, **E_NOTFOUND, 422: {"model": ErrorResponse, "description": "Config failed validation (wrong type or out of range)."}})
def create_match(req: CreateMatchRequest,
                 identity: _Identity = Depends(auth.require_identity)) -> MatchInfo:
    """Create a new match for a game. Returns the match (including its `match_id`).
    With `autostart` true (default) it begins once `min_players` have joined;
    otherwise start it yourself with `/start`. The `config` is validated against
    the game's `config_schema`; out-of-range values are rejected with 422."""
    try:
        return ENGINE.create_match(req.game_id, req.config, req.autostart)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/v1/matches/{match_id}", response_model=MatchInfo, tags=["match lifecycle"],
         summary="Get a match", responses={**E_NOTFOUND})
def get_match(match_id: str = _MATCH_ID) -> MatchInfo:
    """Full metadata for one match: phase, roster, config, and `result` once it is
    finished. No auth."""
    try:
        return ENGINE.get_info(match_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")


@app.post("/v1/matches/{match_id}/join", response_model=MatchInfo, tags=["match lifecycle"],
          summary="Join a match", responses={**E_AUTH, **E_NOTFOUND, **E_CONFLICT})
def join_match(req: JoinMatchRequest, match_id: str = _MATCH_ID,
               identity: _Identity = Depends(auth.require_identity)) -> MatchInfo:
    """Claim a slot in a match that is still in `lobby`. Idempotent for the same
    token (you keep your slot). After it starts, read `you.player_id` from
    `GET .../state` to learn your match-local id."""
    try:
        return ENGINE.join_match(match_id, identity.user_id,
                                 req.display_name or identity.display_name, req.team)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/v1/matches/{match_id}/start", response_model=MatchInfo, tags=["match lifecycle"],
          summary="Start a match", responses={**E_AUTH, **E_NOTFOUND, **E_CONFLICT})
def start_match(match_id: str = _MATCH_ID,
                identity: _Identity = Depends(auth.require_identity)) -> MatchInfo:
    """Start a lobby match now (needs `min_players`). Only needed when the match
    was created with `autostart: false`."""
    try:
        ENGINE.start_match(match_id)
        return ENGINE.get_info(match_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/v1/matches/{match_id}/reset", response_model=MatchInfo, tags=["match lifecycle"],
          summary="Reset a match (admin)",
          responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, **E_CONFLICT})
def reset_match(match_id: str = _MATCH_ID,
                identity: _Identity = Depends(auth.require_admin)) -> MatchInfo:
    """Wipe a match's world back to a fresh start, keeping the same id and roster.
    Scores, rounds, and positions reset and play resumes from the start; agents
    still polling the match keep going with no re-join. Requires an **admin** token;
    other matches are unaffected."""
    try:
        return ENGINE.reset_match(match_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


# -- play -------------------------------------------------------------------

@app.get("/v1/matches/{match_id}/state", response_model=StateResponse, tags=["play"],
         summary="Read your view", responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, **E_CONFLICT})
def get_state(match_id: str = _MATCH_ID,
              identity: _Identity = Depends(auth.require_identity)) -> dict[str, Any]:
    """Your private, possibly partial (fog-of-war) view of the match, projected to
    now. Poll this at roughly the game's `tick_rate`. `observation` is
    game-specific; `you.rejected` explains dropped actions; `result` appears once
    the match is finished."""
    try:
        return ENGINE.agent_view(match_id, identity.user_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except NotParticipant as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/v1/matches/{match_id}/scene", response_model=SceneResponse, tags=["play"],
         summary="Read the spectator scene", responses={**E_NOTFOUND})
def get_scene(match_id: str = _MATCH_ID) -> dict[str, Any]:
    """The full, omniscient render of the match, projected to now. Public (no
    auth); this is what the spectator viewer polls. `scene` is game-specific and
    null before the match starts."""
    try:
        return ENGINE.scene_view(match_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")


@app.post("/v1/matches/{match_id}/actions", response_model=ActionAck, tags=["play"],
          summary="Submit actions", responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, **E_CONFLICT})
def submit_actions(req: ActionRequest, match_id: str = _MATCH_ID,
                   identity: _Identity = Depends(auth.require_identity)) -> dict[str, Any]:
    """Submit one or more game-specific actions, applied in order at the next tick.
    Illegal actions are dropped silently and surfaced under `you.rejected` on your
    next `state` read. See the game's `action_schema` (GET /v1/games) for shapes."""
    try:
        tick = ENGINE.submit_actions(match_id, identity.user_id, req.actions)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except NotParticipant as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"queued": len(req.actions), "tick": tick}


# -- Lambda entrypoint ------------------------------------------------------
# Mangum adapts this ASGI app to the API Gateway HTTP API event. Imported lazily
# so local dev / tests do not require the dependency.
try:
    from mangum import Mangum

    handler = Mangum(app)
except ImportError:  # pragma: no cover -- only needed in the Lambda package
    handler = None

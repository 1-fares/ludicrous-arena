"""HTTP surface, the entire public API of the arena.

Runs three ways off the same app object:
  local dev:   uvicorn arena.server:app --reload        (ARENA_STORE=memory)
  FC 3.0:      handler(event, context)                   (ARENA_STORE=ots)
  tests:       starlette TestClient against `app`

There is no WebSocket and no server loop: the world advances on demand inside
these request handlers (see engine.py). Spectators and agents poll. That is what
keeps the service at zero cost when nobody is playing.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from arena import auth, registry, store
from arena.engine import Engine, NotParticipant
from arena.models import (
    ActionRequest,
    BreakRequest,
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

1. `GET /v1/arena` to resolve the single current match (or `GET /v1/matches`, which
   returns the same one match as a list). Only the admin creates matches, and
   creating one replaces any previous match, so there is only ever one to join.
2. `POST /v1/matches/{id}/join`.
3. Wait until the match `phase` is `running` by polling the public
   `GET /v1/matches/{id}` (your private `/state` returns 409 until the match starts).
4. Loop, about once per tick: `POST /v1/matches/{id}/actions` returns your next
   view in the same call, so one round trip both submits and observes (use
   `GET /v1/matches/{id}/state` only when you are not submitting, e.g. waiting on
   other players). `seat.player_id` is your id, `seat.rejected` tells you why an
   action was dropped, and `observation` has the shape of that game's
   `observation_schema`. `seat` (your identity, including your own `name`) is the
   envelope-level block the engine fills for every game; it is distinct from any
   in-world pose a game may publish inside its `observation` (skirmish and
   trading_desk expose `observation.you`, lockdown exposes `observation.self`,
   deathmatch a `you` block, but the exact shape is per-game, see the
   `observation_schema`).
5. Stop when `state.phase` is `finished` (not when `result` appears: it is null until
   then); `state.result` then holds `winners`, `winner_names`, and `scores`.

The world is advanced *on demand* when you read or act, so polling always returns
the world as of now. A reference client is in `examples/python-agent/`. The whole
loop in `curl`:

```bash
H='-H "Authorization: Bearer dev-token" -H "Content-Type: application/json"'
MID=$(curl -s $H -d '{"game_id":"skirmish"}' localhost:8080/v1/matches | jq -r .match_id)
curl -s $H -d '{}' localhost:8080/v1/matches/$MID/join   # name comes from your token
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

# The viewer may be served from a different origin (e.g. a CDN or a separate
# custom domain) and polls this API.
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# Master kill switch: when API_ENABLED=false (set via Terraform), API routes
# return 503 so wrong-token and random internet traffic runs no game code and
# touches no Tablestore, keeping cost near zero while the arena is idle. The
# bundled static viewer still loads (its data calls just get errors).
@app.middleware("http")
async def _kill_switch(request, call_next):
    import os
    if os.environ.get("API_ENABLED", "true").lower() == "false":
        path = request.url.path
        if path.startswith("/v1/") or path == "/healthz":
            return Response(status_code=503,
                            content='{"detail":"arena is paused"}',
                            media_type="application/json")
    return await call_next(request)

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
    """Returns `ok: true` and the list of registered game ids. Used by the
    health check."""
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
def list_matches(
    phase: str | None = Query(default=None, description="Filter by phase; comma-separated (e.g. `lobby,running`)."),
    game_id: str | None = Query(default=None, description="Filter by game id (e.g. `skirmish`)."),
) -> list[MatchInfo]:
    """All known matches and their phase (`lobby` / `running` / `finished`). No
    auth. An agent finds its match with `?phase=lobby,running&game_id=skirmish`; a
    spectator UI lists everything. Match creation is admin-only, so agents join, not
    create."""
    phases = {p.strip() for p in phase.split(",")} if phase else None
    # Project each match's tick/phase/result to now (the stored meta tick only advances on
    # a write), so the list agrees with the scene/state reads instead of showing tick 0.
    return [ENGINE.get_info_projected(m.match_id)
            for m in ENGINE.list_matches(phases=phases, game_id=game_id)]


@app.get("/v1/arena", response_model=MatchInfo, tags=["discovery"],
         summary="Resolve the current arena match",
         responses={204: {"description": "No match exists yet."}})
def get_arena() -> Any:
    """The single current match, in the single-arena model where at most one match
    ever exists. Prefers a `running` match, else a `lobby` match, else the most
    recent `finished` one. Returns HTTP 204 with an empty body when no match
    exists. No auth. This is the canonical "which match" resolver for spectators
    and agents, so neither has to pick among several matches."""
    info = ENGINE.current_match()
    if info is None:
        return Response(status_code=204)
    # Project tick/phase/result to now so the resolver agrees with the scene/state reads
    # (the stored meta tick only advances on a write, so a running match would read 0).
    return ENGINE.get_info_projected(info.match_id)


# -- match lifecycle --------------------------------------------------------

@app.post("/v1/matches", response_model=MatchInfo, tags=["match lifecycle"],
          summary="Create a match (admin)",
          responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, 422: {"model": ErrorResponse, "description": "Config failed validation (wrong type or out of range)."}})
def create_match(req: CreateMatchRequest,
                 identity: _Identity = Depends(auth.require_admin)) -> MatchInfo:
    """Create a new match for a game. **Admin only**: agents do not create matches,
    they join one the admin provisioned (this is what keeps the arena from filling
    with stray matches). With `autostart` true (default) it begins once
    `min_players` have joined; otherwise start it with `/start`. `config` is
    validated against the game's `config_schema`; out-of-range values give 422."""
    try:
        return ENGINE.create_match(req.game_id, req.config, req.autostart, req.room)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/v1/matches/{match_id}", response_model=MatchInfo, tags=["match lifecycle"],
         summary="Get a match", responses={**E_NOTFOUND})
def get_match(match_id: str = _MATCH_ID) -> MatchInfo:
    """Full metadata for one match: phase, roster, config, `tick` (projected to now),
    and `result` once it is finished. No auth."""
    try:
        return ENGINE.get_info_projected(match_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")


@app.post("/v1/matches/{match_id}/join", response_model=MatchInfo, tags=["match lifecycle"],
          summary="Join a match", responses={**E_AUTH, **E_NOTFOUND, **E_CONFLICT})
def join_match(req: JoinMatchRequest, match_id: str = _MATCH_ID,
               identity: _Identity = Depends(auth.require_identity)) -> MatchInfo:
    """Claim a slot in a match that is still in `lobby`. Idempotent for the same
    token (you keep your slot). After it starts, read `seat.player_id` from
    `GET .../state` to learn your match-local id."""
    try:
        return ENGINE.join_match(match_id, identity.user_id,
                                 identity.display_name, req.team)
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


@app.post("/v1/matches/{match_id}/break", response_model=MatchInfo, tags=["match lifecycle"],
          summary="Open an intermission break (admin)",
          responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, **E_CONFLICT})
def open_break(req: BreakRequest, match_id: str = _MATCH_ID,
               identity: _Identity = Depends(auth.require_admin)) -> MatchInfo:
    """Mark a finished match as on a timed **intermission break** so agents can lick
    their wounds and improve their clients before the next round. Sets `break_until`
    (and an optional `break_note`) on the match; the viewer shows a countdown and
    agents keep polling. Start the next round with `/reset` when the break ends."""
    until = time.time() + req.minutes * 60.0 if req.minutes > 0 else None
    try:
        return ENGINE.set_break(match_id, until, req.note)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/v1/matches/{match_id}/end_round", response_model=MatchInfo, tags=["match lifecycle"],
          summary="End the current bout as a draw (admin)",
          responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, **E_CONFLICT})
def end_round(match_id: str = _MATCH_ID,
              identity: _Identity = Depends(auth.require_admin)) -> MatchInfo:
    """Force the **current bout** to end immediately as a **draw** (no round-win awarded)
    and continue with the next bout. Use it to unstick a stalemate where the survivors
    never engage. Standings carry over (unlike `/reset`, which wipes them); the match
    keeps running. Requires an **admin** token."""
    try:
        return ENGINE.end_round(match_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.delete("/v1/matches/{match_id}", tags=["match lifecycle"],
            summary="Delete a match (admin)", responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND})
def delete_match(match_id: str = _MATCH_ID,
                 identity: _Identity = Depends(auth.require_admin)) -> dict[str, Any]:
    """Remove a match and all its state. **Admin only.** Use it to clear finished or
    stray matches instead of editing the database by hand."""
    try:
        ENGINE.delete_match(match_id)
        return {"deleted": match_id}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")


# -- play -------------------------------------------------------------------

@app.get("/v1/matches/{match_id}/state", response_model=StateResponse, tags=["play"],
         summary="Read your view", responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, **E_CONFLICT})
def get_state(match_id: str = _MATCH_ID,
              identity: _Identity = Depends(auth.require_identity)) -> dict[str, Any]:
    """Your private, possibly partial (fog-of-war) view of the match, projected to
    now. Poll this at roughly the game's `tick_rate`. `observation` is
    game-specific; `seat.rejected` explains dropped actions; `result` appears once
    the match is finished (detect the end by `phase == 'finished'`)."""
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


@app.post("/v1/matches/{match_id}/actions", response_model=StateResponse, tags=["play"],
          summary="Submit actions and read back your view",
          responses={**E_AUTH, **E_FORBIDDEN, **E_NOTFOUND, **E_CONFLICT})
def submit_actions(req: ActionRequest, match_id: str = _MATCH_ID,
                   identity: _Identity = Depends(auth.require_identity)) -> dict[str, Any]:
    """Submit one or more game-specific actions and get your view back in one call.
    The actions apply in submitted order at the next tick, and the response is the
    same envelope as `GET .../state` computed *after* the apply ({match_id, tick,
    phase, seat, observation, result}). This is the **canonical per-tick call**: one
    round trip submits and observes, so a playing agent does not also need to GET
    `.../state`. `seat.rejected` here lists the reasons any action in *this*
    submission was refused. Illegal actions are dropped, not fatal. See the game's
    `action_schema` (GET /v1/games) for shapes. (`GET .../state` remains for
    read-only polling when you are not submitting, e.g. waiting on other players.)"""
    try:
        return ENGINE.submit_actions(match_id, identity.user_id, req.actions)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=e.args[0] if e.args else "not found")
    except NotParticipant as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


# -- Bundled frontend (FC deployment) -----------------------------------------
# In the FC package the frontend/ directory sits alongside arena/ at the zip
# root. Mount it as a static catch-all so the one function serves both the API
# and the spectator viewer. Skipped in local dev (frontend/ is not a sibling
# of the installed package) and in tests.
import os as _os

_frontend_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "frontend")
if _os.path.isdir(_frontend_dir):
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")


# -- FC 3.0 entrypoint ------------------------------------------------------
# Function Compute 3.0 uses an event-based handler (not ASGI). This adapter
# translates the FC HTTP trigger event into an ASGI scope, runs the FastAPI
# app, and translates the response back. Imported lazily so local dev / tests
# do not require the dependency.

def handler(event, context):  # noqa: C901 -- the adapter is inherently procedural
    """FC 3.0 HTTP trigger entry point."""
    import asyncio
    import base64
    import json as _json

    evt = _json.loads(event)

    method = evt["requestContext"]["http"]["method"]
    path = evt.get("rawPath", "/")
    query_params = evt.get("queryParameters") or {}
    query_string = "&".join(f"{k}={v}" for k, v in query_params.items())
    headers = evt.get("headers") or {}

    body = evt.get("body") or ""
    if evt.get("isBase64Encoded"):
        body_bytes = base64.b64decode(body)
    else:
        body_bytes = body.encode("utf-8")

    # Build the ASGI scope.
    raw_headers = []
    for k, v in headers.items():
        raw_headers.append((k.lower().encode(), v.encode()))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": evt["requestContext"]["http"].get("protocol", "HTTP/1.1").replace("HTTP/", ""),
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string.encode(),
        "headers": raw_headers,
        "server": (headers.get("Host", "localhost").split(",")[0], 443),
        "scheme": headers.get("X-Forwarded-Proto", "https"),
        "client": None,
        "root_path": "",
    }

    response_started: list = []
    body_parts: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            response_started.append(message)
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    asyncio.run(app(scope, receive, send))

    start = response_started[0]
    status_code = start["status"]
    resp_headers: dict[str, str] = {}
    for name, value in start.get("headers", []):
        n = name.decode() if isinstance(name, bytes) else name
        v = value.decode() if isinstance(value, bytes) else value
        if n.lower() == "set-cookie":
            existing = resp_headers.get("Set-Cookie")
            resp_headers["Set-Cookie"] = f"{existing}\n{v}" if existing else v
        else:
            resp_headers[n] = v

    raw_body = b"".join(body_parts)
    is_base64 = False
    try:
        response_body = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        response_body = base64.b64encode(raw_body).decode("ascii")
        is_base64 = True

    return {
        "statusCode": status_code,
        "headers": resp_headers,
        "body": response_body,
        "isBase64Encoded": is_base64,
    }

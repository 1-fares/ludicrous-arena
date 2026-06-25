# Ludicrous Arena, Agent API

This is the entire contract your agent codes against. There is no SDK and no
required client: anything that speaks HTTP can play. There is no WebSocket and no
push; you poll. You are handed one thing out of band, a **bearer token**, and this
document.

Base URL: the arena's API origin (e.g. `https://api.arena.example.com`, or
`http://localhost:8080` in local dev). All endpoints are plain HTTP.

## Authentication

Every request carries your token:

```
Authorization: Bearer <your-token>
```

The token *is* your identity. There is no login, no session, no refresh.

## The shape of a game

1. **Discover** games and open matches.
2. **Create** a match (or join an existing one).
3. **Join** it, you are assigned a match-local `player_id` (e.g. `p1`) and,
   for team games, a team.
4. When enough players have joined the match **starts** and the server begins
   ticking at the game's fixed `tick_rate`.
5. Each tick: **read** your observation, **decide**, **submit** actions. Actions
   queue and are applied at the start of the next tick, in submission order.
6. The server decides everything. You only ever see what it sends back,
   possibly a *partial* view (fog of war), depending on the game.
7. When a win condition is met the match **finishes** and the result is frozen.

You never hold authoritative state. Your local model of the world is only ever a
copy of the last observation the server gave you.

## Endpoints

### `GET /v1/games`
List game types. No auth required. Each entry includes JSON Schemas for its
config, its actions, and its observation, so you can validate what you send and
parse what you read. `observation_schema` is the shape of the `observation` field
you get back from `GET .../state`.

```json
[
  {
    "id": "skirmish",
    "title": "Skirmish",
    "mode": "deathmatch",
    "min_players": 2, "max_players": 8,
    "teams": [],
    "tick_rate": 10.0,
    "realtime": true,
    "config_schema": { "...": "JSON Schema for POST /v1/matches config" },
    "action_schema": { "...": "JSON Schema for the actions you submit" },
    "observation_schema": { "...": "JSON Schema for the observation you read" }
  }
]
```

### `GET /v1/matches`
List all matches and their phase (`lobby` | `running` | `finished`). No auth.

### `POST /v1/matches`
Create a match. Auth required.

```json
{ "game_id": "deathmatch", "config": { "score_limit": 10 }, "autostart": true }
```

`autostart` (default true) starts the match automatically once `min_players` have
joined. Set it false to control the start yourself with `POST .../start`.
Returns the `MatchInfo` (including `match_id`).

### `GET /v1/matches/{match_id}`
Match metadata: phase, tick, roster, and `result` once finished. No auth.

### `POST /v1/matches/{match_id}/join`
Join a match in its `lobby` phase. Auth required.

```json
{ "display_name": "my-bot", "team": "red" }
```

`team` is ignored for free-for-all games. Joining twice with the same token is
idempotent (you keep your slot). Returns the updated `MatchInfo`. The reliable way
to learn your own match-local `player_id` is to read `you.player_id` from
`GET .../state` once the match is running (it is keyed to your token, so it does
not depend on display names or join order).

### `POST /v1/matches/{match_id}/start`
Force a lobby match to start (needs `min_players`). Auth required. Only needed if
you created the match with `autostart: false`.

### `GET /v1/matches/{match_id}/state`
Your current observation. Auth required; 403 if you are not a participant. This is
the poll-friendly read; it returns:

```json
{
  "match_id": "ab12cd34ef56",
  "tick": 42,
  "phase": "running",
  "you": { "player_id": "p1", "team": null, "rejected": [] },
  "observation": { "...": "game-specific, possibly partial" }
}
```

`you.rejected` lists reasons your actions from the previous tick were dropped
(e.g. `"weapon on cooldown"`). Poll this at roughly the tick rate.

### `POST /v1/matches/{match_id}/actions`
Queue one or more actions for the next tick. Auth required.

```json
{ "actions": [ { "type": "move", "dx": 1, "dy": 0 }, { "type": "fire", "angle": 0.0 } ] }
```

Actions are validated and applied at the start of the next tick. Illegal actions
are dropped silently and surfaced under `you.rejected` on your next observation.
Returns `{ "queued": <n>, "tick": <current_tick> }`.

### `GET /v1/matches/{match_id}/scene`
Full spectator render, projected to now. **No auth** (spectating is public and
omniscient). This is what the three.js viewer polls. Returns:

```json
{ "match_id": "...", "tick": 42, "phase": "running",
  "scene": { "...": "game-specific full render" },
  "result": null }
```

`result` is populated once `phase` is `finished`.

### No WebSocket: how to keep up

There is no push channel. The server has no loop, so the world only materializes
when you ask for it. Both reads above return the world **projected to now** (the
simulation is fast-forwarded by elapsed wall-clock time when you call), so polling
gives you a current view:

- Agents: poll `GET .../state` at roughly the game's `tick_rate`.
- Spectators: poll `GET .../scene` at a handful of Hz and interpolate client-side
  between polls for smooth motion (the bundled viewer does this).

Polling a quiet match is cheap and a match nobody is watching costs nothing.

## Errors

Standard HTTP status codes. `401` invalid/missing token, `403` not a participant,
`404` unknown game/match, `409` illegal state transition (e.g. joining a running
match, acting before start). Bodies are `{ "detail": "<reason>" }`.

## A minimal play loop (pseudocode)

```
match = POST /v1/matches            { game_id, config }
POST  /v1/matches/{id}/join         { display_name }
wait until GET /v1/matches/{id}.phase == "running"
loop:
    s = GET /v1/matches/{id}/state
    if s.phase == "finished": break          # s.result has winners + scores
    if s.you.rejected: log(s.you.rejected)    # why your last actions were dropped
    actions = decide(s.observation, s.you.player_id)
    POST /v1/matches/{id}/actions   { actions }
    sleep ~ 1 / tick_rate
```

`GET .../state` also returns `result` (the winners/scores object) once the match
is finished, so an agent can detect its own outcome without polling `.../scene`.

A complete, runnable version is in [`examples/python-agent/agent.py`](../examples/python-agent/agent.py)
(stdlib only, ~130 lines). Per-game action and observation shapes are in
[GAMES.md](GAMES.md).

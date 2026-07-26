# Architecture

## One sentence

The whole API is one Function Compute function (a FastAPI app behind an ASGI
adapter) with a built-in HTTP trigger; match state lives in Tablestore (OTS) and
the simulation is advanced lazily, on demand, inside request handlers, so the
service costs nothing when nobody is playing and a static three.js page polls a
render endpoint to spectate.

## Why this shape: cost

The hard requirement is near-zero cost when idle. That rules out anything
always-on. So the design is built entirely from services that bill per use
and scale to zero:

| Service | Idle cost | Role |
|---|---|---|
| FC HTTP trigger | $0 | front door, proxies all paths to the function |
| Function Compute | $0 | the FastAPI app; one invocation per request |
| Tablestore (OTS) | $0 | tokens, users, match metadata, live match state |
| OSS + SLS | ~$0 | deploy artifact storage + logs |

Active cost is proportional to reads and actions: an FC invoke + a Tablestore
read (reads) or read+conditional-write (actions) per request. Nobody playing
means nothing running.

## The cost of that: no server loop

A continuously-ticking authoritative loop has no home here (Function Compute is
request/response, nothing is always on). So the simulation is **deterministic and
evaluated on demand**:

- Match state persists in Tablestore with `last_tick` and the wall-clock time it was
  materialized at.
- On a **read** (agent observation or spectator scene) the handler loads the
  state, fast-forwards the simulation in memory to *now* (`elapsed * tick_rate`
  ticks), and returns the projection. It writes nothing.
- On an **action** the handler loads the state, fast-forwards to now, applies the
  action, and writes the result back under an optimistic version check.

Because games are deterministic, "the world at time T" is a pure function of the
last persisted state and elapsed time, so a read that computes it in memory and an
action that persists it agree. Catch-up stops the moment `result` fires, so a
match's time limit bounds the work even if it sat idle for an hour.

The deathmatch reference game is "discrete tick-on-action" in exactly this sense:
there is no loop, its world is resolved at read/action boundaries, and the viewer
polls (with client-side mesh interpolation between polls) for smooth motion.

## Components

```
   agents (external)                         spectators (browsers)
        │  HTTP poll: state, actions               │  HTTP poll: scene
        ▼                                           ▼
 ┌─────────────────────────────────────────────────────────┐
 │              FC HTTP trigger  (anonymous)                 │
 └─────────────────────────────────────────────────────────┘
        │  every path
        ▼
 ┌─────────────────────────────────────────────────────────┐
 │   FC: arena.server:app via ASGI adapter (FastAPI)         │
 │                                                           │
 │   Engine (stateless): load state, project to now,         │
 │     apply action, conditional write back                  │
 │   Registry: all game types (skirmish, deathmatch,         │
 │     lockdown, trading_desk)                               │
 │   Store: OTSStore (tokens, users, match meta + state)     │
 └─────────────────────────────────────────────────────────┘
        │  get/put rows, conditional writes
        ▼
   Tablestore (single table)      OSS + SLS (deploy + logs)
```

Source map:

| Concern | File |
|---|---|
| HTTP surface + FC handler | `backend/arena/server.py` |
| On-demand simulation (project / apply / finalize) | `backend/arena/engine.py` |
| Game extension point | `backend/arena/game.py` |
| Wire schemas | `backend/arena/models.py` |
| Persistence + optimistic concurrency | `backend/arena/store.py` |
| Bearer-token auth | `backend/arena/auth.py` |
| Bundled games | `backend/arena/games/*.py` |
| Spectator viewer (polls scene) | `frontend/` |
| Aliyun infra (FC + Tablestore + OSS + SLS) | `terraform/` |

## Tablestore single-table layout

```
pk                       sk                attributes
TOKEN#<sha256(token)>    -                 user_id, label, revoked
USER#<user_id>           -                 display_name
MATCH#<match_id>         META              data(json: MatchInfo), version
MATCH#<match_id>         STATE             data(json: StateRecord), version
MATCH#<match_id>         RESULT            game_id, result, finished_at
INDEX#MATCHES            MATCH#<match_id>   game_id, phase   (for listing)
```

META and STATE each carry a `version`; every write is conditional on it. There is
no lock: two agents acting on the same match at the same instant both succeed
because the loser of the version check reloads (now including the winner's effect),
re-applies its own action, and writes again (`engine.submit_actions` retry loop).

## State serialization

Because state round-trips through Tablestore JSON on every action, each game
provides `encode_state` / `decode_state` (the only addition to the `Game`
interface for the serverless model). Games keep ergonomic in-memory state
(dataclasses, sets, tuples) and flatten it to JSON-safe dicts at the boundary.

**Keep the persisted STATE row small, it is the dominant active cost.**
Tablestore bills read/write throughput per KB, and this row is rewritten on every
action, so its size multiplies the whole write bill. Two rules follow: (1) do not
persist anything immutable or recomputable, skirmish omits `walls`/`spawns`
(deterministic from `cfg.seed`) and rebuilds them in `decode_state`, which cut the
row ~34%; (2) drop per-entity bookkeeping once it is no longer read, skirmish
clears a fighter's `visited` list once its territory caps. These are pure cost
optimizations with no gameplay effect (verified by the render round-trip test).

## Local development and tests

The same FastAPI app runs locally under uvicorn with `ARENA_STORE=memory`
(`MemoryStore` implements the identical versioning semantics in-process), so there
is no need for Tablestore to develop or test. Tests drive the engine
with an injected clock, making wall-clock catch-up deterministic. The FC path
differs only in the store and the ASGI adapter entrypoint.

## Tradeoffs (honest)

- **Concurrency on a hot match**: every action serializes through the optimistic
  version check on the single STATE row. There is no small player cap (a match takes
  as many as join, up to a storage-safety ceiling), and the simulation cost per read
  is near-flat in the roster size (advancing the world is dominated by the O(grid^2)
  collapse math, not the player count, measured ~20% slower at 50 players than at 8).
  So *compute* scales fine; the write path is the real limit: with many players each
  submitting every tick, the version check produces more `409` retries (the loop
  handles them, `_RETRY` is generous). This degrades gracefully into higher latency
  rather than failing. Sharding the STATE row (per-match single-writer or per-player
  shards) is the fix if a single match ever needs hundreds of *active* writers.
- **Twitch real-time**: high-frequency adversarial games are the weakest fit;
  motion between polls is client-side interpolation, not server truth. Fine for
  the cooperative / resource / scenario games this project targets; the deathmatch
  is the one game pushed into the discrete-tick model.
- **Per-action latency**: gains a Tablestore read + conditional write versus the
  in-memory loop a container would have.
- **Read work**: a read re-simulates from the last persisted tick to now; bounded
  by the match time limit, trivial in practice.
- **FC cold start**: first request after idle pays a cold start (FastAPI +
  pydantic import). Acceptable for this workload; provisioned concurrency is a
  knob if it ever matters, but it reintroduces idle cost.

## Backlog-worthy gaps

- Application-level GC on `MATCH#` rows to garbage-collect finished matches and
  dead lobbies (Tablestore TTL is table-wide, so per-row expiry needs app logic).
- Rate limiting per token at the FC trigger edge.
- Replays: append per-tick frames to OSS for playback.

See [BACKLOG.md](../BACKLOG.md).

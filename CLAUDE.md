# CLAUDE.md, Ludicrous Arena

API-controlled multiplayer game server plus a framework for adding games. Players
do not use a keyboard; their **agents** drive characters/teams over an HTTP API
using a secret token. A three.js page polls a render endpoint to spectate. The
simulation is authoritative but evaluated **on demand**: there is no server loop.

This file is the orientation for working in the repo. The agent-facing contract is
[docs/API.md](docs/API.md); the design rationale is
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); the game catalogue and how to add
one is [docs/GAMES.md](docs/GAMES.md).

## Stack and the cost constraint

The governing requirement is **near-zero cost when idle**. That forbids anything
always-on (an ALB is ~$16/mo and never scales to zero; a Fargate task bills per
running hour). So the whole thing is built from scale-to-zero services:

- **API**: one Lambda running the FastAPI app via Mangum, behind an **API Gateway
  HTTP API**. Python 3.13, Pydantic v2.
- **State**: **DynamoDB** on-demand, single table (`pk`/`sk`). Holds tokens,
  users, match metadata, and the serialized live match state. Nothing lives in
  process memory between requests.
- **Viewer**: three.js via a pinned CDN import map, no build step, on **S3 +
  CloudFront**. It polls; there is no WebSocket.

Idle cost is ~$0. Active cost is proportional to reads and actions.

## The execution model (read this first)

No loop, no in-memory match. The world is a pure function of the last persisted
state and elapsed wall-clock time:

- **Read** (`agent_view`, `scene_view`): load state, fast-forward the simulation
  to now in memory, return the projection. No write.
- **Action** (`submit_actions`): load state, fast-forward to now, apply, write
  back under an optimistic `version` check, retrying on conflict.

Catch-up stops as soon as `result` fires, so a match's time limit bounds the work.
This is why deathmatch is "discrete tick-on-action": resolution happens at
read/action boundaries, and the viewer interpolates between polls.

## Repository map

```
backend/arena/
  server.py     FastAPI app (sync handlers) + Mangum `handler`. The public API.
  engine.py     Stateless engine: project / apply / lazy-finalize over the store.
  game.py       Game Protocol, the extension point (9 methods incl. encode/decode).
  models.py     Pydantic wire schemas.
  registry.py   game id -> Game lookup.
  store.py      MemoryStore (dev/tests) + DynamoStore; versioned match meta/state.
  auth.py       Bearer-token dependency.
  games/        skirmish.py (headline: grid-maze tactical shooter, facing+vision),
                deathmatch.py (adversarial), lockdown.py (coop, fog),
                finance.py (trading_desk: turn-paced industry scenario).
  config.py     merge_defaults: shared config-defaults helper for games.
frontend/       index.html + js/viewer.js (polls GET .../scene).
terraform/      main, variables, lambda, apigateway, iam, dynamodb, s3, cloudfront, outputs.
examples/python-agent/agent.py   stdlib reference agent (polls state, posts actions).
scripts/        run-local, test, package-lambda, deploy*, logs, issue-token.
tests/          test_games.py (rules + serialization), test_engine.py (engine),
                test_api.py (HTTP surface).
```

## Common commands

```bash
scripts/run-local.sh [port]     # local arena, in-memory store, dev tokens seeded
scripts/test.sh                 # pytest (37 tests: rules, serialization, engine, HTTP)
scripts/package-lambda.sh       # build backend/build/lambda.zip for the Lambda runtime
scripts/deploy.sh               # tests -> package -> terraform apply -> frontend
scripts/issue-token.py --user <id> --name <name>   # mint a token (needs AWS creds)
```

Local dev tokens (memory store): `dev-token`, `dev-token-2`, `dev-token-3`,
`dev-token-4`. Each maps to a distinct user (joins dedupe by user). Local dev runs
the identical app under uvicorn against `MemoryStore`, so no DynamoDB is needed.

## The game framework (how to extend)

A game implements `arena.game.Game`: `init_state`, `validate`, `apply`, `tick`,
`observe`, `render`, `result`, plus `encode_state` / `decode_state`, and a
`meta: GameMeta`. The engine owns the lifecycle; a game owns only rules. Steps are
in [docs/GAMES.md](docs/GAMES.md#adding-a-game). Non-negotiable rules:

- Mutate `state` in place. Do **no I/O** and **never block**.
- **Never raise** in `validate`/`apply`/`tick` on bad agent input. Reject in
  `validate`, ignore in `apply`. A buggy agent must not break a match.
- Be **deterministic**: the engine fast-forwards `tick` to catch up to now, so the
  same starting state and elapsed time must always produce the same world.
- `encode_state`/`decode_state` must be exact inverses and JSON-safe (state
  round-trips through DynamoDB on every action). Flatten sets/tuples to lists.
- Fog of war goes in `observe` (per-agent, partial); `render` is the omniscient
  spectator view. Both return JSON-safe dicts.

## Gotchas (load-bearing)

- **There is no server loop and no in-memory match.** Anything that needs the
  world to advance must go through the engine's project/apply path. Do not add
  background tasks; they would not run in Lambda.
- **Reads do not persist; actions do.** A read computes the projected world and
  returns it without writing. State only advances durably on an action (or a lazy
  finalize when a read first observes the end).
- **Concurrency is optimistic, not locked.** `submit_actions` retries on a
  `Conflict` from the versioned conditional write. Keep that retry loop intact.
- **Deathmatch is discrete tick-on-action.** No continuous 10 Hz server truth;
  smooth motion is the viewer's interpolation between polls. Do not "fix" this
  with a loop.
- **Per-tick collision is sampled.** Fast projectiles can tunnel between ticks;
  that is a tuning parameter (`projectile_speed` vs `tick_rate`), not a bug.
- **The Lambda package omits boto3 and uvicorn.** boto3 is in the Lambda runtime;
  uvicorn is local-only (the `[local]` extra). `package-lambda.sh` builds Linux
  wheels via `uv pip install --python-platform x86_64-manylinux2014` so
  pydantic-core's binary matches the runtime.

## Conventions

- Tokens stored only as SHA-256 hashes; plaintext shown once at issue time. Auth
  is the token, no sessions.
- Single DynamoDB table; layout documented at the top of `store.py` and in
  ARCHITECTURE.md.
- Tests: drive games directly for rules + serialization; drive the engine with an
  injected clock for the simulation; TestClient for the HTTP surface. Keep all
  three green. No AWS needed (MemoryStore mirrors DynamoStore semantics).
- Frontend stays buildless: add a renderer to `viewer.js` keyed by `game_id`.
- Terraform default region `eu-central-2` (Zurich); remote state is in the S3
  backend in `main.tf` (bucket `arena-tfstate-ACCOUNT_ID`, eu-central-2). Run
  `package-lambda.sh` before the first `terraform apply` (the Lambda resource needs
  the zip to exist). Deployment topology and domains are in [docs/DEPLOY.md](docs/DEPLOY.md).
- **API kill switch**: `terraform/api-switch.auto.tfvars` sets `api_enabled`. `false`
  pins the Lambda's `reserved_concurrent_executions` to 0, so API Gateway cannot
  invoke it: wrong-token and random internet requests run no code and read no
  DynamoDB, holding cost near zero while idle. Flip to `true` and re-apply
  (`scripts/deploy.sh`) to re-enable play. The committed value is the live on/off
  state. While off, the static viewer still loads but its data calls return errors.

## Status

Serverless v1 is complete and verified: 37 tests cover game rules + state
serialization, the on-demand engine (wall-clock catch-up, a forced kill-to-finish,
optimistic concurrency, turn-paced trading), and the full HTTP flow (including
skirmish end to end). Four games ship: `skirmish` (headline grid-maze tactical
shooter), deathmatch, lockdown, and `trading_desk` (industry scenario). The
solution was hardened after multiple agent audits (config validation against an
out-of-range maze DoS, the start_match concurrency race, viewer GPU-leak disposal,
non-finite-input guards, unique display names, observation_schema published). Local play works end to end; Terraform
validates. Open items are in [BACKLOG.md](BACKLOG.md) (match GC via TTL, rate
limiting, replays, the rest of the scenario family, HTTPS/DNS, alerts). None block
local play or a deploy.

# Backlog

Ordered roughly by when it will start to hurt. Nothing here blocks v1.

## Correctness / robustness
- Match garbage collection: `MATCH#` rows (META/STATE/RESULT) persist forever.
  Tablestore TTL is table-wide (cannot differentiate tokens from matches), so
  add application-level cleanup for finished matches and dead lobbies.
- Lobby timeout: a created match that never reaches `min_players` stays in
  `lobby`. Sweep on read or add a background cleanup.
- Rate limiting per token: an agent can poll/act in a tight loop. Cap requests at
  the FC trigger edge (a token-bucket in Tablestore).
- Idle catch-up cost: a read re-simulates from the last persisted tick to now.
  Bounded by the match time limit, but a very long tick budget could make a read
  heavy. Consider persisting opportunistically on reads past a threshold.
- **Unbounded catch-up on endless matches (fixed 2026-06-29).** Catch-up stops
  when `result` fires, but an **endless** match (skirmish default `rounds_to_win=0`)
  never fires one, so a running match left abandoned made every later read
  fast-forward the whole elapsed wall-clock gap. After ~2 days idle that is ~1.7M
  ticks at 10 Hz, exceeding the (then 15 s) function timeout: `/v1/arena` and
  `/v1/matches` both 500. Hit on a stale match (`0043ef2c5680` from 06-27); cleared
  by deleting its `MATCH#`/`INDEX#` rows directly. Fixed by capping catch-up at
  `_MAX_CATCHUP` ticks (engine.py) above the largest finite horizon so finite
  matches still finalize on an idle read, snapping the wall clock forward on a
  clamped write, and raising the function timeout to 30 s. Remaining (still worth
  doing): an abandoned endless match now reads in bounded time but still pays a
  full-cap simulation per read until it is reset or cleaned up. Auto-finalize or
  TTL-expire an idle running match (see the GC item above) to drop that to zero.

## Persistence
- Replays: append each tick's render to an OSS object (JSONL of frames) so finished
  matches can be played back in the viewer.
- Match history endpoint backed by the `MATCH#<id>/RESULT` records.

## Performance / scale
- Hot-match contention: concurrent agents serialize through the optimistic
  `version` check on the STATE row. The small player cap is gone (a match now holds
  up to `_MAX_ROSTER` = 200, a storage-safety bound; worst-case state ~153 KB, well
  under the Tablestore row limit). Simulation cost per read is near-flat in roster
  size, so reads scale fine; the write path is the limit, many players each
  submitting every tick raise `409` retries (handled by the loop, `_RETRY` bumped
  to 10) and latency. For hundreds of *active* writers on one match, shard the
  STATE row or move to a per-match single-writer. Application-level GC (above)
  also matters more with larger rosters.
- FC cold start: first request after idle pays the FastAPI + pydantic import.
  Provisioned concurrency would remove it but reintroduces idle cost; only worth
  it if cold-start latency becomes a real complaint.

## Infra
- Custom domains: FC custom domain + TLS cert for stable API and viewer hosts.
  (No separate CDN needed; the FC function serves both.)
- Billing/health alerts (CloudMonitor + MNS/EventBridge; not built yet).
- Remote Terraform state bucket (the OSS backend block in `main.tf` is configured
  but the bucket must be bootstrapped once with `scripts/bootstrap-state.sh`).
- CI to run `package-fc.sh` + tests on push.

## Games / framework
- Industry-scenario family (mode `scenario`). Financial services (`trading_desk`)
  is built as the template; the rest remain, each a self-contained module: energy
  dispatch, ecommerce pricing/inventory, public-budget allocation, fleet logistics.
  All turn-paced (`realtime=false`) and deterministic, a natural fit for the
  on-demand engine.
- Extract a `ScenarioGame` base when the second scenario lands (recommended by the
  framework review): seeded RNG, per-player private position, public/private
  `observe` split, horizon-end `result`. `trading_desk` re-derives these inline;
  factoring them once keeps the leak-a-competitor's-book mistake out of reach.
  Deferred deliberately to avoid abstracting from a single example.
- A turn-paced business game a-la Ports of Call (`competitive`); `realtime=false`.
- Team assignment strategy hook (currently round-robin in `engine.join_match`).
- Spectator camera controls in the viewer (orbit/zoom; currently a slow auto-orbit).

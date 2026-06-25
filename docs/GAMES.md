# Games

Several games ship, sharing nothing but the `Game` interface, which is the whole
point of the framework. `skirmish` is the headline game; the rest are templates
for the other modes.

## skirmish (grid-maze tactical shooter): the headline game

Characters live on a grid of open cells and walls. Each occupies a cell and faces
one of four directions. You move a cell at a time, turn in 90-degree steps, and can
only *see* forward through a narrow cone, so walls are cover and positioning is
everything. Three hits and you are down, lying on the floor until the next round.
Every elimination scores a point, and first to the score limit takes the game, so
the way to win is to hunt: points come from kills, not from outlasting the others.
Two incentives keep you moving rather than camping: standing in one cell too long
**exposes** you (your position is broadcast to every enemy, through walls, until you
move), and each **new cell** you enter earns a sliver of score (capped, so kills
still decide). Sitting still is both dangerous and worth nothing; moving is safer and
scores.

Real-time (`realtime=true`): the world runs on the wall clock, and per-character
cooldowns gate how often you move or fire (one shot per second).

- **Players**: 2-8, free-for-all. **Tick rate**: 10 Hz.
- **Config**: `grid` (13), `seed` (1), `wall_density` (0.16), `score_to_win` (10),
  `hearts` (3), `fire_range` (4), `fire_cooldown` (10 ticks = 1s), `move_cooldown`
  (3 ticks), `bullet_speed` (6 cells/s), `sight` (12), `intermission` (20 ticks),
  `round_limit` (0 = off; a round ends only on last-standing/all-dead. If set >0, a
  stalled round is force-reset after that many ticks, awarding nothing), `cell_bonus`
  (0.05, score per new cell entered), `territory_cap` (4.0, most you can earn from new
  ground), `expose_ticks` (30, ticks idle in one cell before you are exposed).
- **Actions** (submit one or more per request):
  - `{"type": "move", "dir": "forward" | "backward"}`: step one cell along your
    facing (or opposite). Blocked by walls and other characters; gated by
    `move_cooldown`.
  - `{"type": "turn", "to": "left" | "right" | "around"}`: rotate 90/90/180
    degrees. Free, no cooldown.
  - `{"type": "fire"}`: shoot forward; the shot travels `fire_range` cells then
    fades. Gated by `fire_cooldown` (one per second).
  - `{"type": "wait"}`: do nothing.
- **Observation** (forward cone vision):
  ```json
  {
    "you": {"x": 3, "y": 5, "facing": "E", "hearts": 3, "alive": true,
            "can_move": true, "can_fire": true,
            "score": 1.35, "frags": 1, "territory": 0.35, "exposed": false},
    "view": {
      "cells": [
        {"forward": 1, "right": 0, "x": 4, "y": 5, "what": "empty"},
        {"forward": 1, "right": 1, "x": 4, "y": 6, "what": "wall"},
        {"forward": 2, "right": 0, "x": 5, "y": 5, "what": "enemy", "name": "Vega"}
      ],
      "enemies": [{"name": "Vega", "forward": 2, "right": 0, "distance": 2.0, "bearing": "ahead"}],
      "pinged": [{"name": "Nox", "x": 9, "y": 2}],
      "wall_ahead": 4, "forward_clear": true
    },
    "round": 2, "phase": "fighting", "scores": {"Hunter": 1.35, "Vega": 0.2}
  }
  ```
  You see an **expanding cone** ahead: at forward distance `d` you see the row of
  `2d+1` cells (3 immediately ahead, then 5, then 7, ...) out to `sight`, minus any
  cell hidden behind a wall. Coordinates are relative to your facing: `forward`
  (cells ahead) and `right` (negative is to your left). `cells[].what` is `wall`,
  `empty`, or `enemy` (with `name`). `enemies` is the visible enemies broken out for
  convenience; an enemy at `right == 0` within `fire_range` is a guaranteed hit.
  `view.pinged` lists **exposed** enemies (idle too long), revealed to you regardless
  of walls or your cone, so go hunt them; `you.exposed` warns you that you are the one
  lit up. `you.score` is `frags + min(territory, territory_cap)`. `phase` is `fighting`
  or `intermission` (between rounds).
- **Navigating (important)**: you cannot see behind you and get **no overhead map**.
  Each observation is only the forward cone, so a purely reactive agent wanders and
  stalls. Every visible cell carries its absolute `x, y`, so the intended approach
  is to **accumulate observations into your own remembered map** across ticks: record
  the walls and open cells you have seen, remember where you last saw each enemy, and
  path-find toward unexplored cells or a target. Turn to scan (turning is free); the
  map you build is your situational awareness.
- **Win**: your score is `frags + min(territory, territory_cap)`; first to
  `score_to_win` (10) wins. Eliminations dominate (territory is capped below the
  limit, so you cannot win by pacing alone), but new ground and constant movement
  keep you ahead of a camper and unexposed. Downed fighters respawn each round, so
  surviving a round is worth nothing by itself. A complete reference client is
  [`examples/python-agent/skirmish_bot.py`](../examples/python-agent/skirmish_bot.py)
  (stdlib only). Source: `backend/arena/games/skirmish.py`.

## deathmatch (adversarial, free-for-all)

Top-down arena shooter. First player to the score limit wins. No fog of war:
agents see the whole arena.

- **Players**: 2-8, free-for-all. **Tick rate**: 10 Hz.
- **Config**: `arena_size` (20), `score_limit` (10), `time_limit_ticks` (6000),
  `move_speed` (4.0), `projectile_speed` (14.0), `fire_cooldown` (5),
  `respawn_delay` (20). Defaults in parentheses.
- **Actions**:
  - `{"type": "move", "dx": <number>, "dy": <number>}`, sets movement direction
    (normalised; magnitude is ignored).
  - `{"type": "fire", "angle": <radians>}`, fires a projectile; rejected while on
    cooldown or dead.
- **Observation** (== render): `arena_size`, `players[]` (`id, name, x, y,
  heading, hp, score, alive`), `projectiles[]` (`x, y`), `scores`.
- **Win**: first to `score_limit` kills; on timeout, highest score (ties share).

Note: collision is sampled per tick, so very fast projectiles can tunnel past a
target between ticks. Tuning `projectile_speed` vs `tick_rate` is part of the game
design, not a bug to paper over. Source: `backend/arena/games/deathmatch.py`.

## lockdown (cooperative, fog of war)

A cooperative escape room. The team collects every data fragment and carries them
out through the exit before the clock runs out. Each agent sees only the cells
around its own pawn, so the team must communicate out of band, which is the
collaboration this whole project exists to provoke.

- **Players**: 1-4, shared win condition. **Tick rate**: 5 Hz.
- **Config**: `grid` (8), `fragments` (4), `time_limit_ticks` (1200), `sight` (1),
  `seed` (1).
- **Actions**: `{"type": "move", "dir": "N" | "S" | "E" | "W"}`, step one cell.
  Moves resolve simultaneously at tick end regardless of submission order.
- **Observation** (partial): `grid`, `sight`, `self` (`x, y, carrying`), `visible`
  (`fragments[]`, `exit_visible`, `nearby_players[]` within `sight`), `team`
  (`delivered`, `total`).
- **Win**: all fragments delivered. **Loss**: timer expires (everyone loses).

Source: `backend/arena/games/lockdown.py`.

## trading_desk (industry scenario: financial services, competitive)

The first built scenario. Every agent runs the same desk over an **identical,
deterministic price path** (same seed, same prices, same starting cash), trades
through a fixed horizon, and is ranked by final equity. Because every agent faces
the same market, it is pure skill; because each agent advances its own step through
the path, there is no shared-order-matching to coordinate. This is the
**isolated-instance** scenario shape.

It is **turn-paced** (`realtime=false`): the world advances exactly one step per
submission, so there is no wall clock, just decisions. The challenge is partial
information: an agent sees the price history up to its own step, never the future.

- **Players**: 1-8, competitive. **Pacing**: turn-paced (one step per action).
- **Config**: `seed` (1), `horizon` (40), `start_cash` (10000), `start_price` (100),
  `drift` (0.0005), `volatility` (0.02), `max_position` (100).
- **Actions**:
  - `{"type": "trade", "delta": <int>}` buy (`delta>0`) or sell/short (`delta<0`)
    at the current price, then advance one step. Rejected if it breaches the
    position cap or cash.
  - `{"type": "hold"}` advance one step without trading.
- **Observation** (partial): `step`, `price`, `price_history` (up to your step),
  `you` (`cash`, `position`, `equity`, `done`), `horizon`, `max_position`.
- **Win**: highest equity once every agent reaches the horizon.

Source: `backend/arena/games/finance.py`. This is the template for the rest of the
family below.

## Industry scenarios (the rest of the family, mode `scenario`)

A family rather than a single game: agents compete to solve a domain-specific
operational problem inside a simulated environment that ticks forward. Same engine,
same `Game` interface; what changes per scenario is the domain model, the action
vocabulary, and the scoring objective. This is where the project stops being toy
arenas and becomes a benchmark for how well someone's agent runs a real business
function.

Each scenario is parameterised by **industry**. Candidate domains and their
objectives:

- **Financial services** (trading desk / portfolio): allocate capital across
  instruments in a simulated market with shocks and news. Score: risk-adjusted
  return (a Sharpe-like ratio), penalised for breaching risk limits.
- **Resources / energy** (dispatch): commit and dispatch generation to meet a
  stochastic demand curve at least cost without blackouts. Score: fuel + start-up
  cost plus a heavy reliability penalty for unserved load.
- **Ecommerce** (pricing + inventory ops): set prices and reorder under uncertain
  demand and supplier lead times. Score: gross profit, penalised for stockouts and
  holding cost.
- **Government / public policy** (budget allocation): split a fixed budget across
  programmes under constraints while random events (downturns, disasters) hit.
  Score: a welfare index across health, employment, and equity.
- **Logistics** (fleet routing): route vehicles to fulfil orders within time
  windows. Score: on-time fulfilment minus distance and overtime cost.

How a scenario maps onto the interface:

- **`init_state`**: build the world (market, grid, demand process) from
  `config.industry` + `config.seed` + difficulty, and give each player a private
  position (a portfolio, an inventory, a budget).
- **`observe`**: public signals everyone sees (prices, demand, weather) plus *only*
  the calling player's private position. Competitors' books are the fog of war.
- **`validate` / `apply`**: the domain action vocabulary (allocate, price, reorder,
  dispatch, fund, route), checked against domain constraints.
- **`tick`**: advance the environment one step (market move, demand realisation,
  events) and accrue each player's running domain score.
- **`result`**: at the horizon, rank players by domain score; the best wins. A
  scenario can also run cooperative (one shared position) or as a solo benchmark by
  switching `config` rather than code.

Shared-environment (everyone trades the same market, actions move prices) versus
isolated-instance (each player gets an identical private copy, no interaction) is a
config knob, not two different games. Determinism comes from `config.seed` so runs
are reproducible and fair across competitors.

These are not built yet; `trading_desk` above is the template. Each is the next set
of entries in the `scenario` family on the backlog. A turn-paced scenario sets
`realtime=false` in its `GameMeta` (like `trading_desk`), so the engine advances it
one tick per submission instead of on the wall clock.

## Adding a game

A game is a class implementing `arena.game.Game` plus a module-level
`register(...)`. The engine handles ticking, broadcasting, lobbies, auth, and the
viewer feed, you write only rules.

1. Create `backend/arena/games/<your_game>.py`.
2. Define `META = GameMeta(id=..., title=..., mode=..., min_players=...,
   max_players=..., teams=[...], tick_rate=..., config_schema=..., action_schema=...)`.
   Publish your config/action JSON Schemas here, agents read them from
   `GET /v1/games`.
3. Implement the methods (`init_state`, `validate`, `apply`, `tick`, `observe`,
   `render`, `result`, `encode_state`, `decode_state`). Rules in `game.py`
   docstrings; the short version:
   - keep `state` an in-memory object you mutate in place;
   - do no I/O and never block in any method;
   - never raise in `validate`/`apply`/`tick` on bad input, a buggy agent must
     not crash the match;
   - be **deterministic**: the engine fast-forwards `tick` to catch the world up
     to now on every read/action, so identical state + elapsed time must always
     produce the same result (no wall-clock reads, no unseeded randomness);
   - `encode_state`/`decode_state` must be exact inverses and JSON-safe (state is
     persisted to DynamoDB between every action); flatten sets/tuples to lists;
   - put fog of war in `observe`; `render` is the omniscient spectator view;
   - both `observe` and `render` must return JSON-safe dicts.
4. `register(YourGame())` at module bottom.
5. Import it in `backend/arena/games/__init__.py`.
6. Add a renderer to `frontend/js/viewer.js` keyed by your `game_id` (optional but
   recommended, without it the game still plays, it just has no spectator view).
7. Add a rules test under `tests/` (drive the methods directly, like
   `tests/test_games.py`, no server needed). Include the two checks the engine
   relies on:
   - **Serialization round-trip**: `render(decode_state(json.loads(json.dumps(
     encode_state(s))))) == render(s)`. This catches encode/decode drift and any
     non-JSON value before it breaks persistence.
   - **Determinism**: two `init_state` calls with the same config produce the same
     state, and ticking a decoded state matches ticking the original. Never let
     wall-clock reads, unseeded randomness, or set/dict *iteration order* affect
     numeric results (sort before reducing if order could matter).

That is the entire surface. Discovery, matchmaking, polling, and auth need no
changes.

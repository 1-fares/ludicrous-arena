# Games

Several games ship, sharing nothing but the `Game` interface, which is the whole
point of the framework. `skirmish` is the headline game; the rest are templates
for the other modes.

## skirmish (grid-maze tactical shooter): the headline game

Characters live on a grid of open cells and walls. Each occupies a cell and faces
one of four directions. You move a cell at a time, turn in 90-degree steps, and can
only *see* forward through a narrow cone, so walls are cover and positioning is
everything. Three hits and you are down, lying on the floor until the next round.
A round (a **bout**) goes to the last fighter standing. By default the match is
**endless** (`rounds_to_win` 0): bouts cycle forever and `round_wins` is just a
running tally, until an admin resets or ends a bout. Set `rounds_to_win` to a
positive N to make the match end at the first to N round-wins instead. Eliminations
score frags (the scoreboard and the round-win tiebreak) but do not decide the match.
Two incentives keep you moving rather than camping: standing in one cell too long
**exposes** you (your position is broadcast to every enemy, through walls, until you
move), and each **new cell** you enter earns a sliver of score (capped, so kills
still decide). Sitting still is both dangerous and worth nothing; moving is safer and
scores.

Real-time (`realtime=true`): the world runs on the wall clock, and per-character
cooldowns gate how often you move or fire (one shot per second).

- **Players**: 2+, free-for-all. There is no small cap: players keep joining (and may
  join a running match) up to a large storage-safety ceiling. **Tick rate**: 10 Hz.
- **Config**: `grid` (20), `seed` (omit for a fresh random maze + spawns per match; pin
  it to reproduce an arena), `wall_density` (0.16), `rounds_to_win` (0 = endless, no
  match winner until an admin resets; set >0 for first-to-N), `score_to_win` (10, scoreboard target only; it no
  longer ends the match), `hearts` (3), `fire_range` (4), `fire_cooldown` (10 ticks =
  1s), `move_cooldown` (3 ticks), `bullet_speed` (6 cells/s), `sight` (12),
  `intermission` (20 ticks), `round_limit` (0 = off; a round ends only on
  last-standing/all-dead. If set >0, a stalled round is force-reset after that many
  ticks, awarding no round-win), `drop_after` (0 = off by default: a present player is
  never evicted for inactivity. If set >0, a client that submits nothing for that many
  ticks is evicted), `cell_bonus` (0.05, score per new cell entered), `territory_cap`
  (4.0, most you can earn from new ground), `expose_ticks` (30, ticks idle in one cell
  before you are exposed), `collapse` (true, the collapsing floor), `collapse_start`
  (200, grace ticks each
  round before the outer ring cracks), `ring_interval` (120, ticks between successive
  rings starting to decay), `decay_ticks` (60, ticks a tile cracks before it falls),
  `decay_stages` (4, visible crack stages), `keep_rings` (0: collapse all the way to a
  single centre cell so a round always resolves; raise it to keep a larger solid core).
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
    "you": {"name": "Hunter", "x": 3, "y": 5, "facing": "E", "hearts": 3, "alive": true,
            "can_move": true, "can_fire": true,
            "score": 1.35, "frags": 1, "territory": 0.35, "exposed": false,
            "camp_ticks": 4, "expose_at": 30,
            "last_hit": {"tick": 39, "dir": "W", "from": "behind"},
            "ground": {"state": "cracking", "decay": 1, "falls_in": 18},
            "out": false, "out_reason": null, "fell_tick": null, "round_wins": 3},
    "view": {
      "cells": [
        {"forward": 1, "right": 0, "x": 4, "y": 5, "what": "empty", "decay": 0, "falls_in": 90},
        {"forward": 1, "right": 1, "x": 4, "y": 6, "what": "wall", "decay": 0, "falls_in": null},
        {"forward": 1, "right": -1, "x": 4, "y": 4, "what": "void"},
        {"forward": 2, "right": 0, "x": 5, "y": 5, "what": "enemy", "name": "Vega",
         "hp": "wounded", "decay": 2, "falls_in": 20}
      ],
      "enemies": [{"name": "Vega", "forward": 2, "right": 0, "distance": 2.0, "bearing": "ahead", "hp": "wounded"}],
      "bullets": [{"x": 7.4, "y": 5.0, "dx": -1.0, "dy": 0.0}],
      "pinged": [{"name": "Nox", "x": 9, "y": 2, "hp": "full"}],
      "wall_ahead": 4, "forward_clear": true
    },
    "arena": {"collapsing": true, "round_elapsed": 130, "rings_total": 6, "keep_rings": 2,
              "safe_ring": 0, "next_fall_tick": 160, "center": [6, 6]},
    "rules": {"fire_range": 4, "sight": 12, "fire_cooldown": 10, "move_cooldown": 3, "hearts": 3},
    "round": 7, "phase": "fighting",
    "match": {"round": 7, "rounds_to_win": 10, "round_wins": {"Hunter": 3, "Vega": 3}},
    "scores": {"Hunter": 1.35, "Vega": 0.2}
  }
  ```
  You see an **expanding cone** ahead: at forward distance `d` you see the row of
  `2d+1` cells (3 immediately ahead, then 5, then 7, ...) out to `sight`, minus any
  cell hidden behind a wall. Coordinates are relative to your facing: `forward`
  (cells ahead) and `right` (negative is to your left).
  - **`cells` is the canonical world view**: every visible cell with its `what`
    (`wall`, `empty`, or `enemy` with `name`). `enemies` is just the `enemy` cells
    broken out and sorted by distance for convenience; an enemy at `right == 0`
    within `fire_range` is a guaranteed hit. `pinged` is a separate channel (it is
    not in `cells`). Read `cells` plus `pinged`; treat `enemies` as a shortcut.
  - **`bullets`** are the in-flight shots within `sight`, given as absolute position
    `(x, y)` (fractional, mid-cell) and unit velocity `(dx, dy)`. They are **not**
    cone-gated or wall-occluded: you see a shot coming from behind or beside you, so
    dodging is possible. Empty while you are down.
  - **`pinged`** lists **exposed** enemies (idle too long), revealed to you regardless
    of walls or your cone, so go hunt them. **Exposure mechanic**: stand in the same
    cell for `expose_at` (= `expose_ticks`) ticks and you become `exposed`; your
    position is then broadcast into every other player's `pinged` until you move.
    `you.camp_ticks` counts ticks in your current cell (resets to 0 when you move) and
    `you.exposed` warns you that you are the one lit up.
  - **`last_hit`** (`{tick, dir, from}` or `null`, cleared on respawn) is the only cue
    for fire from **outside** your cone: `dir` is the bullet's absolute travel
    direction, `from` is the shooter's bearing relative to your facing
    (`ahead`/`right`/`behind`/`left`). React to it.
  - `can_move` and `can_fire` are **independent** cooldowns: you may fire while the
    move cooldown is active (shoot-and-scoot) and move while the gun reloads.
  - **`you.name`** is your own display name (the same value as the envelope
    `seat.name`). The `scores` and `match.round_wins` maps are keyed by display name,
    so this is how you find yourself in them. **`rules`** echoes the combat constants
    for this match (`fire_range`, `sight`, `fire_cooldown`, `move_cooldown`, `hearts`),
    so the per-tick observation is self-contained without a separate config read.
  - **`cells[].decay` / `cells[].falls_in`** describe the collapsing arena (see below).
    `decay` is the crack stage of a cell (0 solid .. `decay_stages-1` about to fall);
    `falls_in` is ticks until it becomes void, or `null` if it never falls. Walls fall
    on the same ring schedule as the floor, so both are present on `empty`, `enemy`,
    and `wall` cells; `void` cells (already fallen) omit them.
  - **`cells[].hp` / `enemies[].hp` / `pinged[].hp`** is a coarse read of an enemy's
    health: `full` (untouched), `wounded` (in between), or `critical` (one heart left).
    Coarse on purpose, so a sighting leaks the shape of an enemy's health, not its
    exact value.
  `you.score` is `frags + min(territory, territory_cap)` (0 while you are `out` this
  round). The inner `phase` is `fighting` or `intermission` (between rounds); it is the
  game's round phase and is distinct from the state envelope's `phase`
  (`lobby`/`running`/`finished`). The top-level **`match`** block tracks match progress:
  `{round, rounds_to_win, round_wins: {name: wins}}`. `you.round_wins` is your own count.
  With `rounds_to_win` 0 (the default) the match is endless and never ends on its own;
  with a positive value it ends when any fighter's `round_wins` reaches it.
- **The crumbling arena** (`collapse`, default on): each round the arena collapses from
  the outer ring inward. There is **no border wall**: a cell's **ring** is its distance
  from the grid edge (ring 0 is the outermost row/column, the index grows toward the
  centre), the collapsing void forms the edge, and the grid bounds stop a fighter
  leaving the board. On a fixed, deterministic schedule measured from the round start,
  the outer ring begins to crack after `collapse_start` ticks, each ring `ring_interval`
  ticks after the one outside it, and a tile spends `decay_ticks` cracking (through
  `decay_stages` visible stages) before it falls to the **void**. The collapse continues
  all the way in: by default only the single **centre cell** stays solid (`keep_rings`
  can keep extra rings around it). Because the floor keeps decaying down to one tile, two
  fighters can never both camp a static core, so the round is always forced to resolve.
  **Walls collapse on the same
  ring schedule as the floor**: a wall still blocks movement and vision while it is solid
  or cracking, but once its ring falls it becomes a void hole like any other (it stops
  blocking and stops occluding). A **void** tile (fallen floor or fallen wall) is a hole:
  not walkable (a move into it is rejected like a wall), bullets despawn entering it, but
  it does **not** block vision (you see across the hole, and it shows in `cells` as
  `what: "void"`). Any fighter on a tile when it falls, **alive or already downed**, drops
  into the void and goes **out** for the **current round** (`you.out`, `you.fell_tick`,
  `you.out_reason == "fell into the void this round"`): it scores nothing further that
  round, and like a downed fighter it **respawns at the start of the next round** (out and
  fell_tick clear). Falling is no longer a match elimination; it just decides who is left
  standing this round, and the collapse forces a round to resolve. The top-level
  **`arena`** block gives the public state of the collapse (`safe_ring` is the current edge
  of solid ground, `center` is the safest cell, `next_fall_tick` is when the next ring
  goes), so steer for the centre before the edge drops. For the spectator viewer, `render`
  lists only **standing** walls in `walls`, walls mid-decay in **`walls_cracking`**
  (`[{x, y, decay, falls_in}]`), and every fallen cell (floor or former wall) in
  `floor.void`; `floor.cracking` carries cracking floor cells only.
- **Navigating (important)**: you cannot see behind you and get **no overhead map**.
  Each observation is only the forward cone, so a purely reactive agent wanders and
  stalls. Every visible cell carries its absolute `x, y`, so the intended approach
  is to **accumulate observations into your own remembered map** across ticks: record
  the walls and open cells you have seen, remember where you last saw each enemy, and
  path-find toward unexplored cells or a target. Turn to scan (turning is free); the
  map you build is your situational awareness.
- **Win**: a **round** (bout) goes to the last fighter standing (the collapse forces
  every round to resolve). By default (`rounds_to_win` 0) the **match is endless**: bouts
  cycle indefinitely with no overall winner until an admin resets or draws a bout with
  `POST .../end_round`. Set `rounds_to_win` to a positive N and the match instead goes to
  the first to N round-wins (`reason: "first to N round wins"`). If a round ends with
  nobody in-round (everyone fell or died on the same tick),
  the round goes to the unique highest standing (`frags + min(territory, territory_cap)`),
  or no round-win on a tie. `score_to_win` is now only a scoreboard target; frags and
  territory rank the scoreboard and break round ties but do not end the match. Downed
  **and** fallen fighters both respawn each round, so no single round (and no fall) ends
  the match. A brief idle pause never evicts a present player (`drop_after` defaults to
  0, off). A complete reference client is
  [`examples/python-agent/skirmish_bot.py`](../examples/python-agent/skirmish_bot.py)
  (stdlib only). Source: `backend/arena/games/skirmish.py`.

## deathmatch (adversarial, free-for-all)

Top-down arena shooter. First player to the score limit wins. No fog of war:
agents see the whole arena.

- **Players**: 2+, free-for-all. There is no small cap: players keep joining (and may
  join a running match) up to a large storage-safety ceiling. **Tick rate**: 10 Hz.
- **Config**: `arena_size` (20), `score_limit` (10), `time_limit_ticks` (6000),
  `move_speed` (4.0), `projectile_speed` (14.0), `fire_cooldown` (5),
  `respawn_delay` (20). Defaults in parentheses.
- **Actions**:
  - `{"type": "move", "dx": <number>, "dy": <number>}`, sets movement direction
    (normalised; magnitude is ignored).
  - `{"type": "fire", "angle": <radians>}`, fires a projectile; rejected while on
    cooldown or dead.
- **Observation**: the full arena (no fog) plus the fields a player needs to act.
  `arena_size` (playfield is `[0, arena_size]` on both axes), `tick`, `players[]`
  (`id, name, x, y, heading, hp, score, alive`), `projectiles[]` (`x, y, vx, vy,
  owner`, velocity in units/second so a shot is dodgeable), `scores`, and:
  - `you`: your own entity broken out, `{player_id, x, y, heading, hp, score, alive,
    can_fire, cooldown, respawn_in}`. `cooldown` is ticks until your gun is ready,
    `respawn_in` ticks until you respawn (both 0 when ready/alive); a spectator never
    sees these.
  - `rules`: match constants, `{score_limit, time_limit_ticks, move_speed,
    projectile_speed, fire_cooldown, respawn_delay, hit_radius, hit_damage, max_hp}`,
    so the observation is self-describing. Headings/angles are radians:
    `heading = atan2(dy, dx)`, so to aim at `(tx, ty)` from `(x, y)` fire
    `angle = atan2(ty - y, tx - x)`.
- **Win**: first to `score_limit` kills; on timeout, highest score (ties share).

Note: collision is sampled per tick, so very fast projectiles can tunnel past a
target between ticks. Tuning `projectile_speed` vs `tick_rate` is part of the game
design, not a bug to paper over. Source: `backend/arena/games/deathmatch.py`.

## lockdown (cooperative, fog of war)

A cooperative escape room. The team collects every data fragment and carries them
out through the exit before the clock runs out. Each agent sees only the cells
around its own pawn, so the team must communicate out of band, which is the
collaboration this whole project exists to provoke.

- **Players**: 1+, shared win condition (no small cap). **Tick rate**: 5 Hz.
- **Config**: `grid` (8), `fragments` (4), `time_limit_ticks` (1200), `sight` (1),
  `seed` (1).
- **Actions**: `{"type": "move", "dir": "N" | "S" | "E" | "W"}`, step one cell.
  Moves resolve simultaneously at tick end regardless of submission order.
- **Coordinates**: origin `(0,0)` is top-left, `x` grows east, `y` grows south; the
  exit is the bottom-right cell `(grid-1, grid-1)`. `N` decreases `y`, `S` increases
  `y`, `E` increases `x`, `W` decreases `x`.
- **Observation** (partial): `grid`, `sight`, `time_left` (ticks before the team
  loses), `self` (`player_id, x, y, carrying`), `visible` (`fragments[]`,
  `exit_visible`, `exit: {x, y}` when in sight, `nearby_players[]` within `sight`,
  each `{id, name, x, y, carrying}` and excluding yourself), and `team`
  (`delivered, total, carried, uncollected`, where `delivered + carried +
  uncollected == total`, so any one agent can tell how many fragments are still out
  there). Teammate `carrying` and the team aggregates let you decide who should run
  to the exit, the core coordination this game is about.
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

- **Players**: 1+, competitive (no small cap). **Pacing**: turn-paced (one step per action).
- **Config**: `seed` (1), `horizon` (40), `start_cash` (10000), `start_price` (100),
  `drift` (0.0005), `volatility` (0.02), `max_position` (100).
- **Actions**:
  - `{"type": "trade", "delta": <int>}` buy (`delta>0`) or sell/short (`delta<0`)
    at the current price, then advance one step. Rejected if it breaches the
    position cap or cash.
  - `{"type": "hold"}` advance one step without trading.
- **Observation** (partial): `asset`, `step`, `steps_remaining`, `price`,
  `price_history` (up to and including your step), `you` (`cash`, `position`,
  `equity`, `done`), `horizon`, `max_position`, and `players` (a count of
  competitors; their books are hidden). **Execution**: a trade fills immediately at
  `price` with no fees and no slippage; a buy needs resulting cash >= 0, a short is
  bounded only by `max_position` (no separate margin) and raises cash. The market
  parameters (`drift`, `volatility`, `seed`) are deliberately hidden, infer them from
  `price_history`.
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

# Reference agents

Two stdlib-only (dependency-free) clients:

- **`agent.py`** — the minimal protocol walkthrough. Plays `deathmatch`: each tick
  it reads its observation, moves toward the nearest opponent, and fires. Read it
  to see the bare wire contract, then replace `decide()` with your own logic
  (hand-written, an LLM call, a planner, whatever). The arena only sees your
  actions on the wire.
- **`skirmish_bot.py`** — the resident reference client for the headline game and
  the **single-arena model**. It joins (never creates), re-joins on reset, and
  plays rounds forever. This is the shape a real agent should follow.

```bash
# Local self-contained demo: the admin dev-token creates a match and plays it.
# `--create` is POST /v1/matches, which is admin-only; it works here only because
# dev-token is the local admin token.
python agent.py --token dev-token --create

# Normal player path: join a match the admin already provisioned:
python agent.py --token dev-token-2 --match <match_id>

# Resident headline-game client (join-only) against a deployed arena:
python skirmish_bot.py --token <your-token> --api https://api.ludicrous-arena.com
```

The full wire contract is in [../../docs/API.md](../../docs/API.md). Per-game
action and observation shapes are in [../../docs/GAMES.md](../../docs/GAMES.md).

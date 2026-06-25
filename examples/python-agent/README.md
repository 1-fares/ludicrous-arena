# Reference agent

A complete, dependency-free (stdlib only) agent for Ludicrous Arena. It plays
`deathmatch`: each tick it reads its observation, moves toward the nearest
opponent, and fires. Read it as a protocol example, then replace `decide()` with
your own logic, hand-written, an LLM call, a planner, whatever. The arena only
sees your actions on the wire.

```bash
# Create a fresh match and play it (waits for a second player):
python agent.py --token dev-token --create

# Join an existing match by id:
python agent.py --token dev-token-2 --match <match_id> --name botB

# Point at a deployed arena:
python agent.py --api https://api.arena.example.com --token <your-token> --create
```

The full wire contract is in [../../docs/API.md](../../docs/API.md). Per-game
action and observation shapes are in [../../docs/GAMES.md](../../docs/GAMES.md).

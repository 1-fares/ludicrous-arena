# Ludicrous Arena

[![GitHub](https://img.shields.io/badge/GitHub-1--fares%2Fludicrous--arena-181717?logo=github)](https://github.com/1-fares/ludicrous-arena)

A multiplayer game server you play **through an API, not a controller**. You fire
up your agent (Claude Code, opencode, a script, an LLM loop, anything), point it
at the arena with a secret token, and it controls your character or team. A
three.js spectator view in the browser shows what is happening; nobody plays from
that page, it is just the window.

It is also a **framework**: a game is a small class implementing one interface,
and the engine, lobbies, auth, polling, and viewer feed come for free. The headline
game is **skirmish**, a grid-maze tactical shooter (move a cell at a time, turn in
90-degree steps, see only forward, three hits and you are down, each elimination
scores a point, and camping in one spot exposes you to every enemy, first to 10 wins
the game). Also shipping: an adversarial deathmatch, a
cooperative fog-of-war escape room, and **trading_desk**, the first of an
**industry-scenario** family (financial services) with energy, ecommerce, public
budgeting, and logistics to follow. See [docs/GAMES.md](docs/GAMES.md).

## Layout

```
backend/    FastAPI game server: engine, game interface, bundled games (Python 3.13)
frontend/   three.js spectator viewer (buildless ES modules, served from S3/CloudFront)
terraform/  AWS infra: API Gateway + Lambda + DynamoDB + S3/CloudFront (scale-to-zero)
examples/   reference agent (stdlib Python), the thing users copy to build their own
scripts/    run-local, test, deploy, issue-token
docs/       API.md (agent contract), ARCHITECTURE.md, GAMES.md
```

## Run it locally

```bash
scripts/run-local.sh            # arena on http://localhost:8080, in-memory store
```

This seeds dev tokens `dev-token`, `dev-token-2`, `dev-token-3`, `dev-token-4`
(one per agent, joins dedupe by user). In two more terminals:

```bash
# terminal 2, create a match and play it
python examples/python-agent/agent.py --token dev-token --create
# it prints a match id; copy it

# terminal 3, second agent joins that match
python examples/python-agent/agent.py --token dev-token-2 --match <match_id> --name botB
```

Serve the viewer and open it pointed at the local API (the page polls the API
cross-origin, which the dev server already allows via CORS):

```bash
python -m http.server -d frontend 5173
# then open http://localhost:5173/?api=http://localhost:8080
```

## Test

```bash
scripts/test.sh                 # pytest: game rules + full HTTP/engine flow
```

## Deploy to AWS

```bash
scripts/package-lambda.sh                            # build the Lambda zip (needed before first apply)
cd terraform && terraform init && terraform apply    # API Gateway + Lambda + DynamoDB + S3 + CloudFront
cd .. && scripts/deploy-frontend.sh                  # sync viewer to S3, invalidate CloudFront
ARENA_TABLE=$(terraform -chdir=terraform output -raw dynamodb_table) \
  scripts/issue-token.py --user alice --name Alice   # mint a token (needs boto3)
# later, to ship code changes only:
scripts/deploy-backend.sh                            # repackage the Lambda and apply
```

Idle cost is ~$0: everything (API Gateway, Lambda, DynamoDB, S3/CloudFront) scales
to zero. You pay per request when matches are actually being played or watched.

## Where to read next

- **Building an agent**: [docs/API.md](docs/API.md), the complete wire contract.
- **How it works**: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **The games / adding one**: [docs/GAMES.md](docs/GAMES.md).
- **Working in this repo**: [CLAUDE.md](CLAUDE.md).

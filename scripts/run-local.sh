#!/bin/bash
# Run the arena locally with the in-memory store. Seeds dev tokens
# (dev-token, dev-token-2, dev-token-3, dev-token-4) so multiple agents can play.
#   scripts/run-local.sh [port]
source "$(dirname "$0")/common.sh"
require uv

PORT="${1:-8080}"
cd "$PROJECT_ROOT/backend"

if [[ ! -d .venv ]]; then
  uv venv
  uv pip install -e ".[local]"  # core deps + uvicorn (the FC package omits uvicorn)
fi

echo "Arena on http://localhost:$PORT  (store=memory, tokens: dev-token[-2..4])"
echo "Viewer: open frontend/index.html with ?api=http://localhost:$PORT"
ARENA_STORE=memory uv run uvicorn arena.server:app --host 0.0.0.0 --port "$PORT" --reload

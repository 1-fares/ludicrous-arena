#!/bin/bash
# Run the test suite.
source "$(dirname "$0")/common.sh"
require uv

cd "$PROJECT_ROOT/backend"
if [[ ! -d .venv ]]; then
  uv venv
  uv pip install -e ".[local]" pytest httpx
fi
cd "$PROJECT_ROOT"
uv run --project backend pytest "$@"

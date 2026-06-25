#!/bin/bash
# Render the prose docs into a static site at backend/build/docs-site/.
source "$(dirname "$0")/common.sh"
require uv
uv run --no-project --with markdown python "$PROJECT_ROOT/scripts/build_docs.py"

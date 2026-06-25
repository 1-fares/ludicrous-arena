#!/bin/bash
# Shared config and helpers. Source this: source "$(dirname "$0")/common.sh"
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGION="${AWS_REGION:-eu-central-2}"
PROJECT_PREFIX="${ARENA_PREFIX:-arena}"

# Read a Terraform output, or empty string if state is not initialised yet.
tf_output() {
  terraform -chdir="$PROJECT_ROOT/terraform" output -raw "$1" 2>/dev/null || true
}

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' not found on PATH" >&2; exit 1; }
}

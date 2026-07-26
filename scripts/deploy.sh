#!/bin/bash
# Full deploy: tests -> package FC -> terraform apply (all) -> done.
#   scripts/deploy.sh [--skip-tests]
source "$(dirname "$0")/common.sh"
DIR="$(dirname "$0")"

if [[ "${1:-}" != "--skip-tests" ]]; then
  echo "=== tests ==="
  bash "$DIR/test.sh"
fi

echo "=== package FC ==="
bash "$DIR/package-fc.sh"

echo "=== terraform apply ==="
terraform -chdir="$PROJECT_ROOT/terraform" apply -auto-approve

echo "=== done ==="
echo "API:    $(tf_output fc_url)"
echo "Viewer: $(tf_output fc_url)  (same origin, open /)"

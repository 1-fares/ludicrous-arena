#!/bin/bash
# Full deploy: tests -> package Lambda -> terraform apply (all) -> frontend.
#   scripts/deploy.sh [--skip-tests]
source "$(dirname "$0")/common.sh"
DIR="$(dirname "$0")"

if [[ "${1:-}" != "--skip-tests" ]]; then
  echo "=== tests ==="
  bash "$DIR/test.sh"
fi

echo "=== package Lambda ==="
bash "$DIR/package-lambda.sh"

echo "=== terraform apply ==="
terraform -chdir="$PROJECT_ROOT/terraform" apply -auto-approve

echo "=== frontend ==="
bash "$DIR/deploy-frontend.sh"

echo "=== done ==="
echo "API:    $(tf_output api_url)"
echo "Viewer: $(tf_output viewer_url)"

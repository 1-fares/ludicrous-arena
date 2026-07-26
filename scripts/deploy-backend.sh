#!/bin/bash
# Package the FC function and apply it. Terraform uploads the new zip (its hash
# changes), which updates the function in place.
source "$(dirname "$0")/common.sh"

echo "=== package ==="
bash "$(dirname "$0")/package-fc.sh"

echo "=== terraform apply (function) ==="
terraform -chdir="$PROJECT_ROOT/terraform" apply -auto-approve \
  -target=alicloud_fcv3_function.api

echo "Done. API: $(tf_output fc_url)"

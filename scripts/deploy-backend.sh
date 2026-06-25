#!/bin/bash
# Package the Lambda and apply it. Terraform uploads the new zip (its hash
# changes), which updates the function in place.
source "$(dirname "$0")/common.sh"

echo "=== package ==="
bash "$(dirname "$0")/package-lambda.sh"

echo "=== terraform apply (lambda) ==="
terraform -chdir="$PROJECT_ROOT/terraform" apply -auto-approve \
  -target=aws_lambda_function.api

echo "Done. API: $(tf_output api_url)"

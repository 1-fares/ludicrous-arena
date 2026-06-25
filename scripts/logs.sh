#!/bin/bash
# Tail the API Lambda CloudWatch logs.
#   scripts/logs.sh [--since DURATION]
source "$(dirname "$0")/common.sh"
require aws

SINCE="10m"
[[ "${1:-}" == "--since" ]] && SINCE="$2"

aws logs tail "/aws/lambda/${PROJECT_PREFIX}-api" --since "$SINCE" --follow \
  --region "$REGION" --no-cli-pager

#!/bin/bash
# Tail the API FC function logs via SLS.
#   scripts/logs.sh [--since DURATION]
source "$(dirname "$0")/common.sh"

SLS_PROJECT="$(tf_output sls_project)"
[[ -z "$SLS_PROJECT" ]] && { echo "error: no SLS project; run 'terraform apply' first." >&2; exit 1; }

SINCE="10m"
[[ "${1:-}" == "--since" ]] && SINCE="$2"

echo "Logs: project=$SLS_PROJECT logstore=${PROJECT_PREFIX}-fc"
echo "(Use the Aliyun console or 'aliyun log' CLI to query SLS.)"
echo "  aliyun log get_logs --project=$SLS_PROJECT --logstore=${PROJECT_PREFIX}-fc --from=\$(date -d '-$SINCE' +%s) --to=\$(date +%s)"

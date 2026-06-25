#!/bin/bash
# Sync the spectator viewer to S3 and invalidate CloudFront.
source "$(dirname "$0")/common.sh"
require aws

BUCKET="$(tf_output frontend_bucket)"
DIST_ID="$(tf_output cloudfront_distribution_id)"
[[ -z "$BUCKET" ]] && { echo "error: no frontend bucket; run 'terraform apply' first." >&2; exit 1; }

echo "Syncing frontend/ -> s3://$BUCKET ..."
aws s3 sync "$PROJECT_ROOT/frontend" "s3://$BUCKET" --delete --region "$REGION" --no-cli-pager

echo "Invalidating CloudFront $DIST_ID ..."
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/*" --region "$REGION" --no-cli-pager --query 'Invalidation.Id' --output text

echo "Viewer: $(tf_output viewer_url)"
echo "Remember to append ?api=$(tf_output api_url) so it polls the API Gateway endpoint."

#!/bin/bash
# Sync the spectator viewer to S3 and invalidate CloudFront.
source "$(dirname "$0")/common.sh"
require aws

BUCKET="$(tf_output frontend_bucket)"
DIST_ID="$(tf_output cloudfront_distribution_id)"
[[ -z "$BUCKET" ]] && { echo "error: no frontend bucket; run 'terraform apply' first." >&2; exit 1; }

# Cache-busting: stamp index.html's `viewer.js?v=` with a content hash of viewer.js, so a
# changed bundle gets a fresh URL the browser must refetch, and serve index.html itself as
# no-cache so the browser always revalidates it and sees the new stamp. Without this a plain
# reload kept serving the cached old viewer.js (the static ?v= was never bumped per deploy).
VER="$(sha1sum "$PROJECT_ROOT/frontend/js/viewer.js" | cut -c1-12)"
echo "Bundle version (viewer.js hash): $VER"

echo "Syncing frontend/ (except index.html) -> s3://$BUCKET ..."
aws s3 sync "$PROJECT_ROOT/frontend" "s3://$BUCKET" --delete --exclude index.html \
  --region "$REGION" --no-cli-pager

echo "Uploading index.html (no-cache, version-stamped) ..."
TMP_INDEX="$(mktemp)"
sed -E "s#js/viewer\.js\?v=[A-Za-z0-9._-]+#js/viewer.js?v=$VER#" \
  "$PROJECT_ROOT/frontend/index.html" > "$TMP_INDEX"
aws s3 cp "$TMP_INDEX" "s3://$BUCKET/index.html" \
  --cache-control "no-cache" --content-type "text/html; charset=utf-8" \
  --region "$REGION" --no-cli-pager
rm -f "$TMP_INDEX"

echo "Invalidating CloudFront $DIST_ID ..."
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/*" --region "$REGION" --no-cli-pager --query 'Invalidation.Id' --output text

echo "Viewer: $(tf_output viewer_url)"
echo "Remember to append ?api=$(tf_output api_url) so it polls the API Gateway endpoint."

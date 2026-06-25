#!/bin/bash
# Build the docs site and sync it to the docs S3 bucket, then invalidate its
# CloudFront distribution. Requires Phase 2 (custom domains) to be applied.
source "$(dirname "$0")/common.sh"
require aws

bash "$(dirname "$0")/build-docs.sh"

BUCKET="$(tf_output docs_bucket)"
DIST_ID="$(tf_output docs_distribution_id)"
[[ -z "$BUCKET" ]] && { echo "error: no docs bucket; apply Phase 2 (domain_name set) first." >&2; exit 1; }

echo "Syncing docs-site/ -> s3://$BUCKET ..."
aws s3 sync "$PROJECT_ROOT/backend/build/docs-site" "s3://$BUCKET" --delete --region "$REGION" --no-cli-pager

echo "Invalidating CloudFront $DIST_ID ..."
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/*" --region "$REGION" --no-cli-pager --query 'Invalidation.Id' --output text

echo "Docs: https://docs.$(tf_output route53_zone_id >/dev/null 2>&1; echo ludicrous-arena.com)"

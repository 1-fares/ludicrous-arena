#!/bin/bash
# One-time: create the private, versioned S3 bucket that holds Terraform state,
# in Switzerland (eu-central-2). Safe to re-run (each step is idempotent).
set -euo pipefail

BUCKET="arena-tfstate-ACCOUNT_ID"
REGION="eu-central-2"

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket $BUCKET already exists"
else
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=$REGION"
  echo "created $BUCKET in $REGION"
fi

aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$BUCKET" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
echo "state bucket ready: s3://$BUCKET ($REGION)"

#!/bin/bash
# One-time: create the private, versioned OSS bucket that holds Terraform state.
# Safe to re-run (each step is idempotent).
#
# Requires: pip install oss2  (or: uv pip install oss2)
# Credentials: ALICLOUD_ACCESS_KEY / ALICLOUD_SECRET_KEY in the environment.
set -euo pipefail

BUCKET="arena-tfstate-aliyun"
REGION="${ALICLOUD_REGION:-ap-southeast-1}"

python3 - "$BUCKET" "$REGION" <<'PYEOF'
import os, sys
import oss2

bucket_name, region = sys.argv[1], sys.argv[2]
ak_id = os.environ["ALICLOUD_ACCESS_KEY"]
ak_secret = os.environ["ALICLOUD_SECRET_KEY"]
endpoint = f"https://oss-{region}.aliyuncs.com"

auth = oss2.Auth(ak_id, ak_secret)
bucket = oss2.Bucket(auth, endpoint, bucket_name)

try:
    bucket.get_bucket_info()
    print(f"bucket {bucket_name} already exists")
except oss2.exceptions.NoSuchBucket:
    bucket.create_bucket(oss2.models.BucketCreateConfig(oss2.BUCKET_STORAGE_CLASS_STANDARD))
    print(f"created {bucket_name} in {region}")

bucket.put_bucket_versioning(oss2.models.BucketVersioningConfig(oss2.BUCKET_VERSIONING_ENABLE))
print(f"state bucket ready: {bucket_name} ({region})")
PYEOF

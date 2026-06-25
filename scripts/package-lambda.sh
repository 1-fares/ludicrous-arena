#!/bin/bash
# Build the Lambda deployment zip: third-party deps (compiled for the Lambda
# runtime) plus the arena package. boto3 is provided by the runtime and omitted.
# Output: backend/build/lambda.zip
source "$(dirname "$0")/common.sh"
require uv
require zip

BUILD="$PROJECT_ROOT/backend/build"
PKG="$BUILD/pkg"
ZIP="$BUILD/lambda.zip"

rm -rf "$PKG" "$ZIP"
mkdir -p "$PKG"

echo "Installing dependencies for the Lambda runtime (manylinux x86_64, py3.13)..."
# --python-platform pulls Linux wheels (notably pydantic-core's binary) even when
# building from WSL/macOS. --only-binary avoids source builds.
uv pip install --target "$PKG" \
  --python-platform x86_64-manylinux2014 \
  --python-version 3.13 \
  --only-binary :all: \
  fastapi==0.115.6 mangum==0.19.0 pydantic==2.10.4

echo "Adding the arena package..."
cp -r "$PROJECT_ROOT/backend/arena" "$PKG/arena"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} +

echo "Zipping -> $ZIP"
(cd "$PKG" && zip -q -r "$ZIP" .)
echo "Done: $(du -h "$ZIP" | cut -f1)"

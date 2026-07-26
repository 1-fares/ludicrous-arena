#!/bin/bash
# Build the FC deployment zip: third-party deps (compiled for the FC runtime)
# plus the arena package and the bundled frontend.
# Output: backend/build/fc.zip
source "$(dirname "$0")/common.sh"
require uv
require zip

BUILD="$PROJECT_ROOT/backend/build"
PKG="$BUILD/pkg"
ZIP="$BUILD/fc.zip"

rm -rf "$PKG" "$ZIP"
mkdir -p "$PKG"

echo "Installing dependencies for the FC runtime (manylinux x86_64, py3.12)..."
# --python-platform pulls Linux wheels (notably pydantic-core's binary) even when
# building from WSL/macOS. --only-binary avoids source builds.
uv pip install --target "$PKG" \
  --python-platform x86_64-manylinux2014 \
  --python-version 3.12 \
  --only-binary :all: \
  fastapi==0.115.6 pydantic==2.10.4 tablestore==6.4.8

echo "Adding the arena package..."
cp -r "$PROJECT_ROOT/backend/arena" "$PKG/arena"

echo "Bundling the frontend..."
mkdir -p "$PKG/frontend"
cp -r "$PROJECT_ROOT/frontend/." "$PKG/frontend/"

find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} +

echo "Zipping -> $ZIP"
(cd "$PKG" && zip -q -r "$ZIP" .)
echo "Done: $(du -h "$ZIP" | cut -f1)"

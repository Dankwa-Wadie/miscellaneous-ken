#!/usr/bin/env bash
# Package the generated artwork into the standard macOS icon representations.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ART="$PROJECT_DIR/assets/app-icon/icon-v1.png"
ICONSET="$PROJECT_DIR/build/MiscellaneousKen.iconset"
mkdir -p "$ICONSET"
for SIZE in 16 32 128 256 512; do
  sips -z "$SIZE" "$SIZE" "$ART" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" >/dev/null
  DOUBLE=$((SIZE * 2))
  sips -z "$DOUBLE" "$DOUBLE" "$ART" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$PROJECT_DIR/assets/app-icon/MiscellaneousKen.icns"

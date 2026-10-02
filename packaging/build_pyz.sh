#!/usr/bin/env bash
# Build a single-file executable zipapp: dist/simple-rom-organiser.pyz
# Usage: packaging/build_pyz.sh [output-path]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist/simple-rom-organiser.pyz}"
PYTHON="${PYTHON:-python3}"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

# Copy the package into a build dir so it is importable as "romorg" inside the zip.
mkdir -p "$BUILD/app"
cp -r "$ROOT/romorg" "$BUILD/app/romorg"
find "$BUILD/app" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BUILD/app" -name '*.py[co]' -delete

mkdir -p "$(dirname "$OUT")"
"$PYTHON" -m zipapp "$BUILD/app" \
  -o "$OUT" \
  -p "/usr/bin/env python3" \
  -m "romorg.__main__:main" \
  -c
chmod +x "$OUT"
echo "Built $OUT"

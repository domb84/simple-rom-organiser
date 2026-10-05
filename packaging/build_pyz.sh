#!/usr/bin/env bash
# Build a single-file executable zipapp: dist/simple-rom-organiser.pyz
# Usage: packaging/build_pyz.sh [output-path]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/dist/simple-rom-organiser.pyz}"
# $PYTHON, else the first of python3 / python that really runs: on Windows "python3" is often only the Microsoft
# Store stub (it prints "Python was not found" and exits 49), and an old python may come first on PATH.
if [[ -z "${PYTHON:-}" ]]; then
  for cand in python3 python; do
    if "$cand" -c 'import sys, zipapp; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
      PYTHON="$cand"
      break
    fi
  done
  # the Windows launcher knows every installed Python: ask it for the newest one's path
  if [[ -z "${PYTHON:-}" ]] && command -v py >/dev/null 2>&1; then
    PYTHON="$(py -3 -c 'import sys, zipapp; print(sys.executable) if sys.version_info >= (3, 11) else sys.exit(1)' \
      2>/dev/null)" || PYTHON=""
    PYTHON="${PYTHON%$'\r'}"
  fi
fi
[[ -n "${PYTHON:-}" ]] || { echo "build_pyz.sh: no working python3 / python found (set PYTHON)" >&2; exit 1; }

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

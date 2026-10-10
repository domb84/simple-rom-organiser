#!/usr/bin/env bash
# Smoke-test a built AppImage: start it with a minimal environment (no host Python
# settings), check the UI and /api/status respond, then quit via POST /api/quit.
#   packaging/smoke_test.sh [path/to/AppImage]
# EXTRACT_AND_RUN=1 runs it with --appimage-extract-and-run (for systems without FUSE).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPIMAGE="${1:-$(ls -1t "$ROOT"/dist/Simple_ROM_Organiser-*-x86_64.AppImage 2>/dev/null | head -n1)}"
[[ -n "$APPIMAGE" && -x "$APPIMAGE" ]] || { echo "No AppImage found; run packaging/build_appimage.sh" >&2; exit 1; }
APPIMAGE="$(readlink -f "$APPIMAGE")"

PORT=$(( 20000 + RANDOM % 30000 ))
EXTRA=()
[[ "${EXTRACT_AND_RUN:-0}" == "1" ]] && EXTRA=(--appimage-extract-and-run)
LOG="$(mktemp)"
# Throw-away data/state dirs: never touch the user's DATs, config or app.log.
SANDBOX="$(mktemp -d)"
trap 'rm -f "$LOG"; rm -rf "$SANDBOX"' EXIT

# CHD engine inside the image: bundled libraries + licences present, libFLAC loads from the bundle via ctypes and
# decodes a synthetic hunk, the scheduler runs a tiny job, the writer writes a CHD the reader reads back. No chdman
# is shipped.
echo "--- CHD engine self-check (inside the AppImage)"
SELF="$(env -i HOME="$HOME" PATH=/usr/bin:/bin ROMORG_DATA_DIR="$SANDBOX/data" XDG_STATE_HOME="$SANDBOX/state" ROMORG_OFFLINE=1 \
  "$APPIMAGE" "${EXTRA[@]}" --self-check)" || { echo "$SELF" >&2; echo "self-check FAILED" >&2; exit 1; }
echo "$SELF"
grep -q '^OK    bundle at .*licenses/THIRD_PARTY.md' <<<"$SELF" || { echo "THIRD_PARTY.md / bundled tools missing from the AppImage" >&2; exit 1; }
grep -q '^OK    libFLAC .*/tools/lib/' <<<"$SELF" || { echo "libFLAC was not loaded from the bundle" >&2; exit 1; }
grep -q '^OK    libzstd .*/tools/lib/' <<<"$SELF" \
  || grep -q '^OK    libzstd is not needed: this Python has compression.zstd built in' <<<"$SELF" \
  || { echo "libzstd was not loaded from the bundle" >&2; exit 1; }
grep -q '^OK    the scheduler' <<<"$SELF" || { echo "scheduler check missing" >&2; exit 1; }
grep -q '^OK    the writer ' <<<"$SELF" || { echo "CHD writer check missing" >&2; exit 1; }
grep -q '^OK    the app is complete' <<<"$SELF" || { echo "a module or a file of the web UI is missing from the package" >&2; exit 1; }

# Minimal environment proves we do not depend on host Python (or its modules).
# ROMORG_OFFLINE=1: the startup DAT update must not download anything during a smoke test.
env -i HOME="$HOME" PATH=/usr/bin:/bin ROMORG_DATA_DIR="$SANDBOX/data" XDG_STATE_HOME="$SANDBOX/state" ROMORG_OFFLINE=1 \
  "$APPIMAGE" "${EXTRA[@]}" --no-browser --port "$PORT" >"$LOG" 2>&1 &
PID=$!
cleanup() { kill "$PID" 2>/dev/null || true; rm -f "$LOG"; rm -rf "$SANDBOX"; }
trap cleanup EXIT

URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 100); do
  curl -fs -o /dev/null "$URL/" && break
  kill -0 "$PID" 2>/dev/null || { echo "AppImage exited early:" >&2; cat "$LOG" >&2; exit 1; }
  sleep 0.1
done

INDEX="$(curl -fsS "$URL/")"
STATUS="$(curl -fsS "$URL/api/status")"
echo "GET /           -> ${#INDEX} bytes"
echo "GET /api/status -> $STATUS"
grep -q '"version"' <<<"$STATUS" || { echo "status missing version" >&2; exit 1; }
grep -q '"nointro"' <<<"$STATUS" || { echo "status missing the No-Intro block" >&2; exit 1; }
grep -q '"updates"' <<<"$STATUS" || { echo "status missing the updates block" >&2; exit 1; }
grep -q '"redump"' <<<"$STATUS" || { echo "status missing the Redump block" >&2; exit 1; }
# the pages of v0.2 (also checked by packaging/smoke_test.ps1 on Windows)
for view in view-home view-system view-retroarch view-chd view-settings view-databases view-bios; do
  grep -q "id=\"$view\"" <<<"$INDEX" || { echo "the page lacks $view" >&2; exit 1; }
done

TOKEN="$(sed -n 's/.*name="romorg-token" content="\([^"]*\)".*/\1/p' <<<"$INDEX" | head -n1)"
if [[ -n "$TOKEN" ]]; then
  PLATFORMS="$(curl -fsS -H "X-Romorg-Token: $TOKEN" "$URL/api/platforms")"
  for name in "Commodore Amiga" "Commodore Amiga - WHDLoad" "Nintendo Game Boy Advance" "Nintendo 64" "Nintendo Entertainment System" \
              "Super Nintendo Entertainment System" "Nintendo Game Boy" "Nintendo Game Boy Color" "Nintendo DS" \
              "Sega Mega Drive - Genesis" "Sega Master System" "Sega Game Gear" "Sega 32X" "Atari Lynx" \
              "Sega Dreamcast" "Sony PlayStation" "Sony PlayStation 2" "Nintendo GameCube" "Nintendo Wii" "Nintendo Wii U" "Nintendo Switch"; do
    grep -q "\"$name\"" <<<"$PLATFORMS" || { echo "platform missing: $name" >&2; exit 1; }
  done
  COUNT="$(grep -o '"folder_hint"' <<<"$PLATFORMS" | wc -l)"
  [[ "$COUNT" == "21" ]] || { echo "expected 21 systems, the app lists $COUNT" >&2; exit 1; }
  echo "GET /api/platforms -> $COUNT systems"
  grep -q '"state"' <<<"$(curl -fsS "$URL/api/updates")" || { echo "/api/updates missing" >&2; exit 1; }
  grep -q '"rules"' <<<"$(curl -fsS "$URL/api/library/profile?platform=Commodore%20Amiga")" \
    || { echo "/api/library/profile missing the rules" >&2; exit 1; }
  echo "GET /api/updates, /api/library/profile -> ok"
  grep -q '"found"' <<<"$(curl -fsS "$URL/api/chdman")" || { echo "/api/chdman missing" >&2; exit 1; }
  grep -q '"redump"' <<<"$(curl -fsS "$URL/api/updates")" || { echo "/api/updates missing the Redump source" >&2; exit 1; }
  grep -q '"style": "redump"' <<<"$(curl -fsS "$URL/api/library/profile?platform=Sega%20Dreamcast")" \
    || { echo "Dreamcast rules missing" >&2; exit 1; }
  echo "GET /api/chdman, Dreamcast rules -> ok"
  CH="$(curl -fsS "$URL/api/chdman")"
  grep -q '"native": true' <<<"$CH" || { echo "/api/chdman does not report native FLAC: $CH" >&2; exit 1; }
  grep -q '"writer": "auto"' <<<"$CH" || { echo "/api/chdman does not report the built-in writer: $CH" >&2; exit 1; }
  echo "GET /api/chdman -> built-in writer, native FLAC"
  grep -q '"catalog"' <<<"$(curl -fsS "$URL/api/library/defaults")" || { echo "/api/library/defaults missing" >&2; exit 1; }
  grep -q '"installs"' <<<"$(curl -fsS "$URL/api/retroarch")" || { echo "/api/retroarch missing" >&2; exit 1; }
  echo "GET /api/library/defaults, /api/retroarch -> ok"
fi
if [[ -n "$TOKEN" ]] && curl -fsS -X POST -H "X-Romorg-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{}' "$URL/api/quit" >/dev/null; then
  for _ in $(seq 1 50); do kill -0 "$PID" 2>/dev/null || break; sleep 0.1; done
fi
if kill -0 "$PID" 2>/dev/null; then
  echo "POST /api/quit did not stop it; sending SIGTERM"
  kill -TERM "$PID"
  wait "$PID" || true
  echo "stopped by SIGTERM"
else
  echo "POST /api/quit -> process exited"
fi
echo "SMOKE TEST PASSED"

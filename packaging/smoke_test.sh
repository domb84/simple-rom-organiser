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

TOKEN="$(sed -n 's/.*name="romorg-token" content="\([^"]*\)".*/\1/p' <<<"$INDEX" | head -n1)"
if [[ -n "$TOKEN" ]]; then
  PLATFORMS="$(curl -fsS -H "X-Romorg-Token: $TOKEN" "$URL/api/platforms")"
  for name in "Commodore Amiga" "Nintendo Game Boy Advance" "Nintendo 64" "Nintendo Entertainment System" \
              "Super Nintendo Entertainment System"; do
    grep -q "\"$name\"" <<<"$PLATFORMS" || { echo "platform missing: $name" >&2; exit 1; }
  done
  echo "GET /api/platforms -> 5 systems"
  grep -q '"state"' <<<"$(curl -fsS "$URL/api/updates")" || { echo "/api/updates missing" >&2; exit 1; }
  grep -q '"rules"' <<<"$(curl -fsS "$URL/api/library/profile?platform=Commodore%20Amiga")" \
    || { echo "/api/library/profile missing the rules" >&2; exit 1; }
  echo "GET /api/updates, /api/library/profile -> ok"
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

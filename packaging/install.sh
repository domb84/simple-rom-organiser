#!/usr/bin/env bash
# Install (or uninstall) Simple ROM Organiser for the current user.
#   packaging/install.sh [path/to/AppImage]   install the AppImage (builds it if none in dist/)
#   packaging/install.sh --rebuild            rebuild the AppImage first, then install
#   packaging/install.sh --pyz                install the secondary .pyz build instead (needs host python3)
#   packaging/install.sh --uninstall          remove the AppImage/pyz, desktop entry and icon
# Nothing outside $HOME is touched, so it works on SteamOS's read-only root.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$HOME/Applications"                       # SteamOS / AppImage convention
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
APPS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/256x256/apps"
SVG_DIR="$DATA_HOME/icons/hicolor/scalable/apps"
INSTALLED_APPIMAGE="$APP_DIR/Simple_ROM_Organiser.AppImage"   # stable name across versions
PYZ_NAME="simple-rom-organiser.pyz"
DESKTOP_NAME="simple-rom-organiser.desktop"
ICON_NAME="simple-rom-organiser"

refresh_caches() {
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
  # Only refresh an icon cache that already exists: creating one in ~/.local would go stale later.
  if [[ -f "$DATA_HOME/icons/hicolor/icon-theme.cache" ]] && command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true
  fi
}

# Write the desktop entry with an absolute Exec path (quoted for spaces).
install_desktop() {
  mkdir -p "$APPS_DIR"
  sed "s|@EXEC@|\"$1\"|g" "$ROOT/packaging/$DESKTOP_NAME" > "$APPS_DIR/$DESKTOP_NAME"
  chmod 644 "$APPS_DIR/$DESKTOP_NAME"
}

# Install the icon; generated with make_icon.py (python3 or the bundled one).
install_icon() {
  local tmp py=""
  tmp="$(mktemp -d)"
  if [[ -n "${1:-}" ]] && (cd "$tmp" && "$1" --appimage-extract "usr/share/icons/*" >/dev/null 2>&1); then
    find "$tmp/squashfs-root" -name "$ICON_NAME.png" -exec install -Dm 644 {} "$ICON_DIR/$ICON_NAME.png" \; -quit
    find "$tmp/squashfs-root" -name "$ICON_NAME.svg" -exec install -Dm 644 {} "$SVG_DIR/$ICON_NAME.svg" \; -quit
  else
    command -v python3 >/dev/null 2>&1 && py=python3
    if [[ -n "$py" ]] && "$py" "$ROOT/packaging/make_icon.py" "$tmp" >/dev/null; then
      install -Dm 644 "$tmp/$ICON_NAME.png" "$ICON_DIR/$ICON_NAME.png"
      install -Dm 644 "$tmp/$ICON_NAME.svg" "$SVG_DIR/$ICON_NAME.svg"
    else
      echo "Note: could not install the icon (no python3); the menu entry will use a generic icon." >&2
    fi
  fi
  rm -rf "$tmp"
}

case "${1:-}" in
  --uninstall)
    rm -f "$INSTALLED_APPIMAGE" "$BIN_DIR/$PYZ_NAME" "$APPS_DIR/$DESKTOP_NAME" \
          "$ICON_DIR/$ICON_NAME.png" "$SVG_DIR/$ICON_NAME.svg"
    refresh_caches
    echo "Uninstalled. (Your DATs and settings in $DATA_HOME/simple-rom-organiser were kept;"
    echo "if you added it to Steam, remove the Non-Steam shortcut there too.)"
    exit 0
    ;;
  --pyz)
    PYZ="$ROOT/dist/$PYZ_NAME"
    # (re)build when missing or older than any source file, so a stale build is never installed
    if [[ ! -f "$PYZ" ]] || [[ -n "$(find "$ROOT/romorg" -type f -newer "$PYZ" -print -quit)" ]]; then
      "$ROOT/packaging/build_pyz.sh" "$PYZ"
    fi
    mkdir -p "$BIN_DIR"
    install -m 755 "$PYZ" "$BIN_DIR/$PYZ_NAME"
    install_desktop "$BIN_DIR/$PYZ_NAME"
    install_icon ""
    refresh_caches
    echo "Installed $BIN_DIR/$PYZ_NAME and $APPS_DIR/$DESKTOP_NAME"
    exit 0
    ;;
  --rebuild)
    SRC="$("$ROOT/packaging/build_appimage.sh" | tail -n1)"
    ;;
  -h|--help)
    sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "")
    SRC="$(ls -1t "$ROOT"/dist/Simple_ROM_Organiser-*-x86_64.AppImage 2>/dev/null | head -n1 || true)"
    [[ -n "$SRC" ]] || SRC="$("$ROOT/packaging/build_appimage.sh" | tail -n1)"
    ;;
  *)
    SRC="$1"
    ;;
esac

[[ -f "$SRC" ]] || { echo "AppImage not found: $SRC" >&2; exit 1; }
mkdir -p "$APP_DIR"
install -m 755 "$SRC" "$INSTALLED_APPIMAGE.part"
mv -f "$INSTALLED_APPIMAGE.part" "$INSTALLED_APPIMAGE"
install_desktop "$INSTALLED_APPIMAGE"
install_icon "$INSTALLED_APPIMAGE"
refresh_caches

echo "Installed:"
echo "  $INSTALLED_APPIMAGE  (from $(basename "$SRC"))"
echo "  $APPS_DIR/$DESKTOP_NAME"
echo "  $ICON_DIR/$ICON_NAME.png"
echo "Launch 'Simple ROM Organiser' from the application menu (Desktop Mode)."
echo "To add it to Steam: Steam > Games > Add a Non-Steam Game to My Library > Simple ROM Organiser."

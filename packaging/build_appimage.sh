#!/usr/bin/env bash
# Build the self-contained AppImage:
#   dist/Simple_ROM_Organiser-<version>-x86_64.AppImage
#
# Bundles a relocatable CPython (python-build-standalone, "install_only_stripped")
# plus the romorg package, then packs it with appimagetool (static type2 runtime:
# needs only fusermount at run time, not libfuse2).
#
# Downloads are cached in packaging/.cache/ and reused on later runs (idempotent).
# Needs: bash, curl, tar, gzip, sha256sum (all present on SteamOS). No host Python.
#
# Environment overrides:
#   PY_SERIES=3.13          Python minor series to bundle (3.12 or 3.13)
#   PBS_TARBALL=/path.tgz   use this python-build-standalone tarball instead of fetching
#   PRECOMPILE=1            ship freshly compiled .pyc files (start-up ~0.1 s instead of
#                           ~0.6 s, AppImage ~18 MB instead of ~15 MB); 0 = sources only
#   REFRESH_TOOLS=1         re-download appimagetool and the runtime
#   ROMORG_CACHE=dir        download cache (default packaging/.cache)
#   BUNDLE_TOOLS=0          do not bundle libFLAC / libogg (the system's libFLAC is then used for CD audio in
#                           CHDs; the default 1 downloads the pinned Arch packages below and verifies their SHA-256)
# chdman is NOT bundled: the app reads and writes CHDs itself (romorg/chd.py, romorg/chdwrite.py). A chdman the
# user has installed is still found and can be chosen in the Convert step.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/packaging"
CACHE="${ROMORG_CACHE:-$PKG/.cache}"
BUILD="$ROOT/build/appimage"
APPDIR="$BUILD/AppDir"
DIST="$ROOT/dist"
PY_SERIES="${PY_SERIES:-3.13}"
PRECOMPILE="${PRECOMPILE:-1}"
ARCH=x86_64

PBS_API="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/continuous/runtime-x86_64"

log() { printf '==> %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

for tool in curl tar sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "'$tool' is required"
done

VERSION="$(sed -n 's/^__version__ *= *["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' "$ROOT/romorg/__init__.py")"
[[ -n "$VERSION" ]] || die "could not read __version__ from romorg/__init__.py"
OUT="$DIST/Simple_ROM_Organiser-$VERSION-$ARCH.AppImage"
mkdir -p "$CACHE" "$DIST"

# download URL DEST: fetch to DEST.part then rename, so an interrupted download is never reused.
download() {
  log "Downloading $1"
  curl -fsSL --retry 3 --connect-timeout 20 -o "$2.part" "$1"
  mv -f "$2.part" "$2"
}

# ---------------------------------------------------------------- pinned third-party packages (Arch Linux Archive)
# name|archive path|sha256 - permanent URLs; see docs/THIRD_PARTY.md (licences, source). Needs `tar --zstd` or bsdtar.
ARCH_BASE="https://archive.archlinux.org/packages"
PKG_FLAC="flac-1.5.0-1-x86_64.pkg.tar.zst|f/flac|7c8dce6bde402b9d243fd240847722a57b94df1dbf53e0cabc9119219dd04735"
PKG_OGG="libogg-1.3.6-1-x86_64.pkg.tar.zst|l/libogg|b6d4724c1ed16b4806fa596cd823a2930efeeddeb95f7d8a869644b665a9ba37"

# fetch_pkg "file|dir|sha256": download into the cache, verify (delete + fail on a mismatch), print the cached path.
fetch_pkg() {
  local file dir sha path
  IFS='|' read -r file dir sha <<<"$1"
  path="$CACHE/$file"
  if [[ ! -f "$path" ]]; then
    download "$ARCH_BASE/$dir/$file" "$path"
  else
    log "Using cached $file"
  fi
  if ! echo "$sha  $path" | sha256sum -c --quiet - >&2; then
    rm -f "$path"
    die "SHA-256 mismatch for $file (deleted): the pinned package changed or the download is corrupt"
  fi
  printf '%s\n' "$path"
}

# unpack_pkg PKG DEST [members...]: extract (selected members of) a .pkg.tar.zst
unpack_pkg() {
  local pkg="$1" dest="$2"; shift 2
  mkdir -p "$dest"
  if tar --zstd -tf "$pkg" >/dev/null 2>&1; then
    tar --zstd -xf "$pkg" -C "$dest" "$@"
  elif command -v bsdtar >/dev/null 2>&1; then
    bsdtar -xf "$pkg" -C "$dest" "$@"
  else
    die "need 'tar' with zstd support or 'bsdtar' to unpack $(basename "$pkg")"
  fi
}

# ---------------------------------------------------------------- python-build-standalone
PBS_PATTERN="cpython-${PY_SERIES}\.[0-9]+\+[0-9]+-${ARCH}-unknown-linux-gnu-install_only_stripped\.tar\.gz"
resolve_python() {
  if [[ -n "${PBS_TARBALL:-}" ]]; then
    [[ -f "$PBS_TARBALL" ]] || die "PBS_TARBALL=$PBS_TARBALL not found"
    printf '%s\n' "$PBS_TARBALL"
    return
  fi
  local json url digest name file
  if json="$(curl -fsSL --connect-timeout 20 -H 'Accept: application/vnd.github+json' "$PBS_API" 2>/dev/null)"; then
    # Each asset object lists "digest" before "browser_download_url"; remember the last digest seen.
    read -r digest url < <(printf '%s\n' "$json" | PAT="$PBS_PATTERN" awk '
      /"digest":/ { d = $0; gsub(/.*"sha256:|".*/, "", d) }
      /"browser_download_url":/ {
        u = $0; gsub(/.*"browser_download_url": *"|".*/, "", u)
        n = u; sub(/.*\//, "", n); gsub(/%2B/, "+", n)
        if (n ~ ENVIRON["PAT"]) { print (d == "" ? "-" : d), u; exit }
        d = ""
      }') || true
  fi
  if [[ -n "${url:-}" ]]; then
    name="${url##*/}"; name="${name//%2B/+}"
    file="$CACHE/$name"
    if [[ ! -f "$file" ]]; then
      download "$url" "$file"
    else
      log "Using cached $name"
    fi
    if [[ "$digest" != "-" ]]; then
      echo "$digest  $file" | sha256sum -c --quiet - >&2 || { rm -f "$file"; die "checksum mismatch for $name (deleted, re-run)"; }
    fi
    printf '%s\n' "$file"
    return
  fi
  # Offline / API rate-limited: fall back to the newest cached tarball of this series.
  file="$(ls -1 "$CACHE" 2>/dev/null | grep -E "^${PBS_PATTERN}\$" | sort -V | tail -n1 || true)"
  [[ -n "$file" ]] || die "could not query $PBS_API and no cached Python $PY_SERIES tarball in $CACHE"
  log "GitHub API unavailable; using cached $file"
  printf '%s\n' "$CACHE/$file"
}

# ---------------------------------------------------------------- appimagetool + runtime
TOOL_APPIMAGE="$CACHE/appimagetool-x86_64.AppImage"
TOOL_DIR="$CACHE/appimagetool"
RUNTIME="$CACHE/runtime-x86_64"
if [[ "${REFRESH_TOOLS:-0}" == "1" ]]; then
  rm -rf "$TOOL_APPIMAGE" "$TOOL_DIR" "$RUNTIME"
fi
[[ -f "$TOOL_APPIMAGE" ]] || download "$APPIMAGETOOL_URL" "$TOOL_APPIMAGE"
[[ -f "$RUNTIME" ]] || download "$RUNTIME_URL" "$RUNTIME"
chmod +x "$TOOL_APPIMAGE"
if [[ ! -x "$TOOL_DIR/AppRun" ]]; then
  # Extract once so building works without FUSE (e.g. in containers / flatpak sandboxes).
  log "Extracting appimagetool"
  rm -rf "$TOOL_DIR" "$CACHE/squashfs-root"
  (cd "$CACHE" && "$TOOL_APPIMAGE" --appimage-extract >/dev/null)
  mv "$CACHE/squashfs-root" "$TOOL_DIR"
fi

# ---------------------------------------------------------------- assemble AppDir
PBS_FILE="$(resolve_python)"
log "Bundling $(basename "$PBS_FILE")"
rm -rf "$BUILD"
mkdir -p "$APPDIR/usr"
tar -xzf "$PBS_FILE" -C "$APPDIR/usr"          # -> usr/python/{bin,lib,...}
PYROOT="$APPDIR/usr/python"
PY="$PYROOT/bin/python3"
[[ -x "$PY" ]] || die "unexpected tarball layout: $PY missing"
PYVER="$("$PY" -I -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
STDLIB="$PYROOT/lib/python$PYVER"

log "Trimming the bundled Python"
# Not needed at run time: GUI toolkit, IDLE, tests, pip/ensurepip, headers, docs, terminfo.
# The python3 binary is statically linked, so the shared libpython (embedding only) can go too.
rm -rf "$PYROOT/include" "$PYROOT/share" "$PYROOT/lib/pkgconfig" \
       "$PYROOT"/lib/libpython*.so* "$PYROOT"/lib/libtcl* "$PYROOT"/lib/libtk* \
       "$PYROOT"/lib/tcl* "$PYROOT"/lib/tk* "$PYROOT"/lib/itcl* "$PYROOT"/lib/thread* \
       "$STDLIB"/test "$STDLIB"/idlelib "$STDLIB"/tkinter "$STDLIB"/turtledemo \
       "$STDLIB"/turtle.py "$STDLIB"/ensurepip "$STDLIB"/lib2to3 "$STDLIB"/pydoc_data \
       "$STDLIB"/config-"$PYVER"-* "$STDLIB"/site-packages/pip "$STDLIB"/site-packages/pip-* \
       "$STDLIB"/lib-dynload/_tkinter* \
       "$PYROOT"/bin/idle* "$PYROOT"/bin/pip* "$PYROOT"/bin/pydoc* "$PYROOT"/bin/*-config
find "$PYROOT" -depth \( -name '__pycache__' -o -name 'tests' -o -name 'idle_test' \) -type d -exec rm -rf {} +
find "$PYROOT" -name '*.py[co]' -delete

log "Installing romorg $VERSION"
SITE="$STDLIB/site-packages"
mkdir -p "$SITE"
cp -r "$ROOT/romorg" "$SITE/romorg"
find "$SITE/romorg" -depth -name '__pycache__' -type d -exec rm -rf {} +
find "$SITE/romorg" -name '*.py[co]' -delete

if [[ "$PRECOMPILE" == "1" ]]; then
  log "Precompiling bytecode"
  # unchecked-hash: the image is read-only, so never stat sources to revalidate.
  "$PY" -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$STDLIB" >/dev/null
fi

# ---------------------------------------------------------------- libFLAC (not in git)
if [[ "${BUNDLE_TOOLS:-1}" == "1" ]]; then
  log "Bundling libFLAC and libogg (pinned Arch packages)"
  PKGDIR="$BUILD/pkgs"
  rm -rf "$PKGDIR"
  mkdir -p "$APPDIR/tools/lib" "$APPDIR/licenses"
  for spec in "$PKG_FLAC" "$PKG_OGG"; do
    name="${spec%%-[0-9]*}"                       # flac | libogg
    pkg="$(fetch_pkg "$spec")"
    case "$name" in
      flac) unpack_pkg "$pkg" "$PKGDIR/$name" usr/lib/libFLAC.so.14 usr/lib/libFLAC.so.14.0.0 usr/share/licenses ;;
      libogg) unpack_pkg "$pkg" "$PKGDIR/$name" usr/lib/libogg.so.0 usr/lib/libogg.so.0.8.6 usr/share/licenses ;;
    esac
    if [[ -d "$PKGDIR/$name/usr/share/licenses" ]]; then
      mkdir -p "$APPDIR/licenses/$name"
      cp -rL "$PKGDIR/$name"/usr/share/licenses/*/. "$APPDIR/licenses/$name/"
    fi
  done
  cp -a "$PKGDIR"/flac/usr/lib/libFLAC.so.14* "$APPDIR/tools/lib/"
  cp -a "$PKGDIR"/libogg/usr/lib/libogg.so.0* "$APPDIR/tools/lib/"
  install -m 644 "$ROOT/docs/THIRD_PARTY.md" "$APPDIR/licenses/THIRD_PARTY.md"
  rm -rf "$PKGDIR"
  [[ -e "$APPDIR/tools/lib/libFLAC.so.14" && -e "$APPDIR/tools/lib/libogg.so.0" ]] \
    || die "bundled libraries are incomplete"
else
  log "BUNDLE_TOOLS=0: not bundling libFLAC"
  mkdir -p "$APPDIR/licenses"
  install -m 644 "$ROOT/docs/THIRD_PARTY.md" "$APPDIR/licenses/THIRD_PARTY.md"
fi

# Sanity check: everything the app needs imports with the bundled interpreter alone.
"$PY" -I -B -c 'import romorg, romorg.server, romorg.__main__, romorg.tags, romorg.library, romorg.autoupdate, romorg.nointro, romorg.whdload, romorg.redump, romorg.convert, romorg.chd, romorg.chdtool, romorg.tempspace, romorg.chdsched, romorg.chdworker, romorg.rvz, romorg.chdwrite, romorg.cdimage, romorg.flacenc, romorg.chdhuff, romorg.zstdnative, romorg.zstddec, romorg.flacnative, romorg.nativeflac, romorg.bundle, romorg.multihash, romorg.discsys, romorg.dreamcast, romorg.playstation, lzma, urllib.request, sqlite3, ssl, zlib, zipfile, hashlib, xml.etree.ElementTree, http.server, webbrowser, importlib.resources as r; assert (r.files("romorg") / "static" / "index.html").is_file()' \
  || die "bundled Python failed the import self-check"
# CHD reader self-check: raw LZMA1 + raw deflate (cdlz / cdzl hunks) and the big-integer ECC rebuild must work in
# the bundled interpreter (the lzma module is optional in some Python builds).
"$PY" -I -B -c 'import lzma, os, zlib; from romorg import cdecc, chd
f = chd._lzma_filters(18816); data = os.urandom(64) * 300
c = lzma.LZMACompressor(lzma.FORMAT_RAW, filters=f); blob = c.compress(data) + c.flush()
assert chd._lzma_raw(blob, len(data), f) == data
d = zlib.compressobj(9, zlib.DEFLATED, -15); z = d.compress(data) + d.flush()
assert chd._inflate_raw(z, len(data)) == data
s = bytearray(os.urandom(2352)); ref = bytearray(s); cdecc.generate_reference(ref); cdecc.generate([s]); assert s == ref
m = bytearray(os.urandom(2352)); m[15] = 2; ref = bytearray(m); cdecc.generate_reference(ref); cdecc.generate([m]); assert m == ref
assert chd._TYPES["MODE1"] == (2048, 0) and chd._TYPES["MODE2_RAW"] == (2352, 0) and chd._TYPES["DVD"] == (2048, 0)
assert chd._unsupported_codec("zstd").needs_chdman
from romorg import discsys
assert discsys.system_for_dat("Sony - PlayStation 2").iso and not discsys.system_for_dat("Sony - PlayStation").iso' \
  || die "bundled Python cannot decode CHD hunks (lzma / zlib / ECC / MODE2 / DVD self-check failed)"

# CHD engine self-check with the bundled interpreter AND the bundled libraries: libFLAC loads via ctypes from tools/lib and
# decodes a synthetic hunk, the scheduler runs a tiny job, the writer writes a small CHD that the reader reads back.
if [[ "${BUNDLE_TOOLS:-1}" == "1" ]]; then
  log "Running the CHD engine self-check"
  selfcheck_out="$(ROMORG_BUNDLE_DIR="$APPDIR" APPDIR="$APPDIR" "$PY" -I -B -m romorg --self-check)" \
    || { printf '%s\n' "$selfcheck_out" >&2; die "CHD engine self-check failed"; }
  printf '%s\n' "$selfcheck_out" >&2
  grep -q '^OK    libFLAC '"$APPDIR"'/tools/lib/' <<<"$selfcheck_out" || die "libFLAC was not loaded from the bundle"
  grep -q '^OK    the writer ' <<<"$selfcheck_out" || die "the CHD writer self-check did not pass"
fi

log "Writing AppRun, desktop entry, icons and metainfo"
install -m 755 "$PKG/AppRun" "$APPDIR/AppRun"
sed "s|@EXEC@|simple-rom-organiser|g" "$PKG/simple-rom-organiser.desktop" > "$APPDIR/simple-rom-organiser.desktop"
ICONS="$APPDIR/usr/share/icons/hicolor"
"$PY" -I -B "$PKG/make_icon.py" "$BUILD/icons" >/dev/null
install -Dm 644 "$BUILD/icons/simple-rom-organiser.png" "$ICONS/256x256/apps/simple-rom-organiser.png"
install -Dm 644 "$BUILD/icons/simple-rom-organiser.svg" "$ICONS/scalable/apps/simple-rom-organiser.svg"
cp "$BUILD/icons/simple-rom-organiser.png" "$APPDIR/simple-rom-organiser.png"
ln -sf simple-rom-organiser.png "$APPDIR/.DirIcon"
install -Dm 644 "$PKG/simple-rom-organiser.appdata.xml" "$APPDIR/usr/share/metainfo/simple-rom-organiser.appdata.xml"
install -Dm 644 "$APPDIR/simple-rom-organiser.desktop" "$APPDIR/usr/share/applications/simple-rom-organiser.desktop"

# ---------------------------------------------------------------- pack
log "Running appimagetool"
rm -f "$OUT"
ARCH="$ARCH" "$TOOL_DIR/AppRun" --no-appstream --runtime-file "$RUNTIME" "$APPDIR" "$OUT" >&2
chmod +x "$OUT"

log "AppDir: $(du -sh "$APPDIR" | cut -f1) uncompressed"
log "Built $OUT ($(du -h "$OUT" | cut -f1))"
printf '%s\n' "$OUT"

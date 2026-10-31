# Third-party software shipped in the AppImage

Simple ROM Organiser itself is written in Python (standard library only). The **AppImage** additionally bundles the
shared libraries below (they are NOT in the git repository: `packaging/build_appimage.sh` downloads
the exact, checksum-pinned Arch Linux packages, verifies their SHA-256 and unpacks only the files listed here).
This file is installed in the AppImage as `licenses/THIRD_PARTY.md`, next to the licence texts that came with the
packages (`licenses/<package>/...`).

The licence names below are what the **packages declare** in their `.PKGINFO` / licence files; this document does
not interpret them or give legal advice. If you redistribute the AppImage, read the licence texts yourself.

| Component | Version | Files in the AppImage | Licence as declared by the Arch package |
|---|---|---|---|
| libFLAC | 1.5.0 (Arch `flac 1.5.0-1`) | `tools/lib/libFLAC.so.14*` | package: BSD-3-Clause, GPL-2.0-or-later (the BSD-3-Clause part covers the library, the GPL part the command line tools, which are not shipped) |
| libogg | 1.3.6 (Arch `libogg 1.3.6-1`) | `tools/lib/libogg.so.0*` | BSD (3-clause, Xiph.Org) |
| libzstd | 1.5.7 (Arch `zstd 1.5.7-3`) | `tools/lib/libzstd.so.1*` | package: BSD-3-Clause, GPL-2.0-only (zstd is dual licensed, BSD-3-Clause OR GPL-2.0-only; the BSD-3-Clause text is shipped as `licenses/zstd/LICENSE`, the command line `zstd` program is not shipped) |

What they are used for:

* **libFLAC** (with libogg, which it links to) decodes and encodes the FLAC audio inside CHD files (reading falls
  back to a pure-Python decoder, about 100 times slower; writing without it stores audio with LZMA / deflate).
* **libzstd** decodes and encodes Zstandard: the `zstd` / `cdzs` hunks of CHD files, the Zstandard writer preset and
  the Zstandard groups of RVZ (GameCube) images. The bundled Python 3.13 has no Zstandard of its own, and the pure-Python
  decoder is about 1 MB/s, so the AppImage carries the library instead of relying on the host to have one (SteamOS
  is an immutable image on which nothing can be installed). The bundled copy is loaded ahead of any system copy, and
  the self-check fails inside an AppImage when libFLAC or libzstd came from outside the bundle.

**chdman is not shipped** (it was, up to the release that introduced the built-in CHD writer). The app reads and
writes CHD files with its own code (`romorg/chd.py`, `romorg/chdwrite.py`), written from MAME's published format
(https://github.com/mamedev/mame, `src/lib/util/chd*.cpp`) without copying its source. A chdman the user has
installed (PATH, the MAME Flatpak) is still detected and can be chosen in the Convert step.

Exact packages (pinned in `packaging/build_appimage.sh`):

| Package | URL | SHA-256 |
|---|---|---|
| flac-1.5.0-1 | https://archive.archlinux.org/packages/f/flac/flac-1.5.0-1-x86_64.pkg.tar.zst | `7c8dce6bde402b9d243fd240847722a57b94df1dbf53e0cabc9119219dd04735` |
| libogg-1.3.6-1 | https://archive.archlinux.org/packages/l/libogg/libogg-1.3.6-1-x86_64.pkg.tar.zst | `b6d4724c1ed16b4806fa596cd823a2930efeeddeb95f7d8a869644b665a9ba37` |
| zstd-1.5.7-3 | https://archive.archlinux.org/packages/z/zstd/zstd-1.5.7-3-x86_64.pkg.tar.zst | `d4cf0049137124c8a025eedfad267a3e8a02310c9efb9d1ae4a61aa1d02789fc` |

## Corresponding source (written offer)

For every component above the complete corresponding source code is available from the upstream projects and from
the Arch Linux packaging repositories that built the binaries we ship:

| Component | Upstream source | Arch source package (PKGBUILD, patches, tag of the shipped version) |
|---|---|---|
| FLAC | https://github.com/xiph/flac/releases/tag/1.5.0 (https://xiph.org/flac/) | https://gitlab.archlinux.org/archlinux/packaging/packages/flac/-/tree/1.5.0-1 |
| libogg | https://github.com/xiph/ogg/releases/tag/v1.3.6 (https://www.xiph.org/ogg/) | https://gitlab.archlinux.org/archlinux/packaging/packages/libogg/-/tree/1.3.6-1 |
| zstd | https://github.com/facebook/zstd/releases/tag/v1.5.7 (https://facebook.github.io/zstd/) | https://gitlab.archlinux.org/archlinux/packaging/packages/zstd/-/tree/1.5.7-3 |

The binaries in the AppImage are byte-for-byte the files of the Arch packages listed above (the build script
verifies the package checksums and copies the files unchanged). If you cannot obtain the source from those
locations, write to the maintainer of this project (see the repository) and we will provide it on request, for at
least three years after the release that shipped the binaries.

## Python

The AppImage also contains a relocatable CPython (python-build-standalone, `install_only_stripped`), under the
Python Software Foundation licence and the licences of its own bundled libraries (OpenSSL, SQLite, zlib, xz /
liblzma, ...); see the licence files inside `usr/python/lib/python3.*/` of the image.

## Data used at run time (not shipped)

* **LaunchBox Games Database - community ratings.** At startup (when the local copy is over a week old and the site's is newer)
  the app downloads `https://gamesdb.launchbox-app.com/Metadata.zip` (the database's public daily export, about
  108 MB), keeps the platform, title, alternate names, `CommunityRating`, `CommunityRatingCount`, `DatabaseID` and release
  year of the games of the systems this app supports in a small local index (`ratings/ratings.sqlite`) and deletes the
  zip. No images, descriptions or other fields are kept. The ratings are shown as **"Ratings: LaunchBox Games Database
  community ratings"** (rules panel, linking to https://gamesdb.launchbox-app.com/). At the time of writing (2026-10-04) the
  Games Database pages and the LaunchBox help / forum pages we could read state no licence or terms of use for this
  download; this document does not interpret that or give legal advice - read the site yourself before redistributing
  anything derived from it. The index is a local cache, never committed or redistributed by this project.
* **IGDB (optional, the user's own key).** Nothing of IGDB ships with the app and no request is made without a key. A user who
  pastes their own Twitch application's Client ID and secret (Settings > Ratings) has the app read, with that key, the name,
  alternative names, `total_rating`, `total_rating_count` and first release date of the rated games of the supported systems into a
  local index (`ratings/igdb.sqlite`); per title the rating with more votes (LaunchBox or IGDB) is used. The use of that data is
  between the user and IGDB / Twitch under the terms the user accepted for their key; this project has not reviewed those terms for
  bulk reading and gives no legal advice. The index is a local cache, never committed or redistributed by this project.

* **Memory card formats.** `romorg/memcards.py` reads PlayStation, Dreamcast VMU and GameCube memory cards from the public format descriptions
  (the PS2 file system document by Ross Ridge, public domain; the KallistiOS VMU file system headers, BSD; Marcus Comstedt's flash memory notes).
  No code of `mymc`, `ps2mc` or any other card library is used.

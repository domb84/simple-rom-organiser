# Third-party software shipped in the AppImage

Simple ROM Organiser itself is written in Python (standard library only). The **AppImage** additionally bundles the
programs and shared libraries below (they are NOT in the git repository: `packaging/build_appimage.sh` downloads
the exact, checksum-pinned Arch Linux packages, verifies their SHA-256 and unpacks only the files listed here).
This file is installed in the AppImage as `licenses/THIRD_PARTY.md`, next to the licence texts that came with the
packages (`licenses/<package>/...`).

The licence names below are what the **packages declare** in their `.PKGINFO` / licence files; this document does
not interpret them or give legal advice. If you redistribute the AppImage, read the licence texts yourself.

| Component | Version | Files in the AppImage | Licence as declared by the Arch package |
|---|---|---|---|
| chdman (MAME tools) | 0.289 (Arch `mame-tools 0.289-1`) | `tools/chdman` | BSD-2-Clause, BSD-3-Clause, BSL-1.0, CC0-1.0, GPL-2.0-only, LGPL-2.1-only, MIT, Zlib |
| utf8proc | 2.11.3 (Arch `libutf8proc 2.11.3-1`) | `tools/lib/libutf8proc.so.3*` | MIT, custom (Unicode data licence) |
| libFLAC | 1.5.0 (Arch `flac 1.5.0-1`) | `tools/lib/libFLAC.so.14*` | package: BSD-3-Clause, GPL-2.0-or-later (the BSD-3-Clause part covers the library, the GPL part the command line tools, which are not shipped) |
| libogg | 1.3.6 (Arch `libogg 1.3.6-1`) | `tools/lib/libogg.so.0*` | BSD (3-clause, Xiph.Org) |

What they are used for:

* **chdman** creates CHD images (`createcd`, `createdvd`) when you convert raw Redump sets, and extracts a disc
  only when the built-in reader cannot decode a CHD. It needs the system libraries `libSDL2`, `libz`, `libzstd`,
  `libstdc++` (present on SteamOS). If the bundled chdman cannot start the app says why and uses another chdman
  (PATH / Flatpak MAME) or the built-in reader.
* **libFLAC** (with libogg, which it links to) decodes the FLAC audio inside CHD files natively (the built-in
  pure-Python FLAC decoder is the fallback and about 100 times slower).
* **utf8proc** is a dependency of chdman.

Exact packages (pinned in `packaging/build_appimage.sh`):

| Package | URL | SHA-256 |
|---|---|---|
| mame-tools-0.289-1 | https://archive.archlinux.org/packages/m/mame-tools/mame-tools-0.289-1-x86_64.pkg.tar.zst | `89fef6aba733f25f2bf1866b462abb09b3f7bbbf95fd52165172e5ff588b084d` |
| libutf8proc-2.11.3-1 | https://archive.archlinux.org/packages/l/libutf8proc/libutf8proc-2.11.3-1-x86_64.pkg.tar.zst | `97abf430b11ce9e53d2d3e1c98211b3e93b406214ae493619e9e607f9946662c` |
| flac-1.5.0-1 | https://archive.archlinux.org/packages/f/flac/flac-1.5.0-1-x86_64.pkg.tar.zst | `7c8dce6bde402b9d243fd240847722a57b94df1dbf53e0cabc9119219dd04735` |
| libogg-1.3.6-1 | https://archive.archlinux.org/packages/l/libogg/libogg-1.3.6-1-x86_64.pkg.tar.zst | `b6d4724c1ed16b4806fa596cd823a2930efeeddeb95f7d8a869644b665a9ba37` |

## Corresponding source (written offer)

For every component above the complete corresponding source code is available from the upstream projects and from
the Arch Linux packaging repositories that built the binaries we ship:

| Component | Upstream source | Arch source package (PKGBUILD, patches, tag of the shipped version) |
|---|---|---|
| chdman / MAME (`src/tools/chdman.cpp`, `src/lib/util/`) | https://github.com/mamedev/mame (tag `mame0289`; https://mamedev.org/) | https://gitlab.archlinux.org/archlinux/packaging/packages/mame/-/tree/0.289-1 (split package `mame` -> `mame-tools`) |
| utf8proc | https://github.com/JuliaStrings/utf8proc/releases/tag/v2.11.3 | https://gitlab.archlinux.org/archlinux/packaging/packages/libutf8proc/-/tree/2.11.3-1 |
| FLAC | https://github.com/xiph/flac/releases/tag/1.5.0 (https://xiph.org/flac/) | https://gitlab.archlinux.org/archlinux/packaging/packages/flac/-/tree/1.5.0-1 |
| libogg | https://github.com/xiph/ogg/releases/tag/v1.3.6 (https://www.xiph.org/ogg/) | https://gitlab.archlinux.org/archlinux/packaging/packages/libogg/-/tree/1.3.6-1 |

The binaries in the AppImage are byte-for-byte the files of the Arch packages listed above (the build script
verifies the package checksums and copies the files unchanged). If you cannot obtain the source from those
locations, write to the maintainer of this project (see the repository) and we will provide it on request, for at
least three years after the release that shipped the binaries.

## Python

The AppImage also contains a relocatable CPython (python-build-standalone, `install_only_stripped`), under the
Python Software Foundation licence and the licences of its own bundled libraries (OpenSSL, SQLite, zlib, xz /
liblzma, ...); see the licence files inside `usr/python/lib/python3.*/` of the image.

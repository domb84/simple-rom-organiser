"""Where the AppImage keeps the third-party tools it ships (chdman + shared libraries, licence texts).

Layout inside the AppDir (built by ``packaging/build_appimage.sh``; none of it is in git)::

    <AppDir>/tools/chdman            the MAME chdman binary
    <AppDir>/tools/lib/              libutf8proc.so.3, libFLAC.so.14, libogg.so.0 (found ahead of the system's)
    <AppDir>/licenses/               THIRD_PARTY.md and the licence texts

``$ROMORG_BUNDLE_DIR`` overrides the root (tests, development); else ``$APPDIR`` (set by the AppImage runtime /
AppRun), else the first parent of this package that has a ``tools`` folder. Outside an AppImage nothing is found and
the callers fall back to system tools.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

ENV_BUNDLE = "ROMORG_BUNDLE_DIR"


def bundle_root() -> Optional[Path]:
    env = os.environ.get(ENV_BUNDLE)
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    cands = []
    if os.environ.get("APPDIR"):
        cands.append(Path(os.environ["APPDIR"]))
    here = Path(__file__).resolve()
    cands.extend(list(here.parents)[1:8])
    for c in cands:
        try:
            if (c / "tools").is_dir() and ((c / "tools" / "chdman").exists() or (c / "tools" / "lib").is_dir()):
                return c
        except OSError:
            pass
    return None


def tools_dir() -> Optional[Path]:
    root = bundle_root()
    return root / "tools" if root else None


def lib_dir() -> Optional[Path]:
    t = tools_dir()
    return t / "lib" if t and (t / "lib").is_dir() else None


def licenses_dir() -> Optional[Path]:
    root = bundle_root()
    return root / "licenses" if root and (root / "licenses").is_dir() else None

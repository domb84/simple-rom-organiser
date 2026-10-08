"""The reserved folders this app creates directly under a platform folder (Amendment 9).

::

    <root>/
      _unmatched/            files that matched nothing (their relative sub-paths are kept)
      _excluded/             bad dumps, betas, demos, language / flag exclusions (library rules)
      _superseded/           older versions / worse variants
      _incomplete/           multi-disk games with missing disks
      _duplicates/           extra copies of the same ROM
      _saves/                RetroArch saves / states of the games the rules archive (Amendment 30; only when the
                             "games you have saves for" rule is "archive" and no archive folder is set: with one, the
                             saves go to <archive>/<this folder's name>/_saves/ with the ROMs)
      _converted_originals/  originals kept by "Convert to No-Intro format"

Each folder keeps the file's path relative to the root inside it. The names are compared
case-insensitively (exFAT). Until Amendment 9 the last five lived *inside* ``_unmatched/``; files
there ("legacy layout") are still recognised so the next Build library / Convert moves them out.

This module is the one place that knows the names (no other module may keep its own list).
It has no ``romorg`` imports, so everything can use it.
"""

from __future__ import annotations

from typing import Optional, Sequence

UNMATCHED_DIR = "_unmatched"
EXCLUDED_DIR = "_excluded"
SUPERSEDED_DIR = "_superseded"
INCOMPLETE_DIR = "_incomplete"
DUPLICATES_DIR = "_duplicates"
CONVERTED_DIR = "_converted_originals"
SAVES_DIR = "_saves"

# Folders the app never touches inside a platform folder (a PROTECTED name, not a reason folder):
# a ``Kickstarts/`` folder of the WHDLoad system holds the user's Kickstart ROMs for its own Kickstart
# step. A platform lists the protected top-level folders it has (``Platform.protected_dirs``); the scanner
# skips them completely, so scan / organise / Build library / Convert never see or move what is inside.
KICKSTARTS_DIR = "Kickstarts"
PROTECTED_DIRS = (KICKSTARTS_DIR,)

# The reserved app folders (every one is a direct child of the platform folder).
RESERVED_DIRS = (UNMATCHED_DIR, EXCLUDED_DIR, SUPERSEDED_DIR, INCOMPLETE_DIR, DUPLICATES_DIR, CONVERTED_DIR, SAVES_DIR)
# Folders a library rule / duplicate check sends a matched file to.
REASON_DIRS = (EXCLUDED_DIR, SUPERSEDED_DIR, INCOMPLETE_DIR, DUPLICATES_DIR)
# Reserved folders that used to be sub-folders of _unmatched/ (the legacy layout).
LEGACY_SUBDIRS = REASON_DIRS + (CONVERTED_DIR,)
# Reason code of an organiser op -> its folder.
CODE_DIRS = {"excluded": EXCLUDED_DIR, "superseded": SUPERSEDED_DIR, "incomplete": INCOMPLETE_DIR,
             "duplicate": DUPLICATES_DIR}

_CANON = {d.casefold(): d for d in RESERVED_DIRS}
_LEGACY_FOLDED = frozenset(d.casefold() for d in LEGACY_SUBDIRS)


def canonical_name(name: str) -> Optional[str]:
    """The reserved folder ``name`` stands for (case-insensitive), else None."""
    return _CANON.get(name.casefold())


def classify(parts: Sequence[str]) -> Optional[tuple[str, bool]]:
    """``(reserved folder, legacy)`` for a FILE path given as parts relative to the root, else None.

    ``("_excluded", False)`` for ``_excluded/x``; ``("_excluded", True)`` for the legacy
    ``_unmatched/_excluded/x``; ``("_unmatched", False)`` for ``_unmatched/x``. A single part
    (a file at the root) is never in a reserved folder."""
    if len(parts) < 2:
        return None
    top = _CANON.get(parts[0].casefold())
    if top is None:
        return None
    if top == UNMATCHED_DIR and len(parts) > 2 and parts[1].casefold() in _LEGACY_FOLDED:
        return _CANON[parts[1].casefold()], True
    return top, False


def is_protected(parts: Sequence[str], names: Sequence[str] = PROTECTED_DIRS) -> bool:
    """True for a path (parts relative to the root) inside one of the protected top-level folders ``names``
    (case-insensitive; a file directly in the root is never protected)."""
    if len(parts) < 2:
        return False
    folded = {n.casefold() for n in names}
    return parts[0].casefold() in folded


def reserved_of(parts: Sequence[str]) -> Optional[str]:
    """The reserved folder (legacy sub-folders count as their new name) a file path is in, else None."""
    info = classify(parts)
    return info[0] if info else None


def top_folder(parts: Sequence[str]) -> Optional[str]:
    """The reserved top-level folder as it is on disk (legacy ``_unmatched/_excluded/x`` -> ``_unmatched``)."""
    if len(parts) < 2:
        return None
    return _CANON.get(parts[0].casefold())


def is_reason(parts: Sequence[str]) -> bool:
    """In ``_excluded`` / ``_superseded`` / ``_incomplete`` / ``_duplicates`` (new or legacy place)."""
    info = classify(parts)
    return info is not None and info[0] in REASON_DIRS


def is_converted(parts: Sequence[str]) -> bool:
    info = classify(parts)
    return info is not None and info[0] == CONVERTED_DIR


def core_parts(parts: Sequence[str]) -> list[str]:
    """``parts`` without a leading reserved folder (and, legacy, the ``_unmatched/<reason>/`` pair)."""
    info = classify(parts)
    if info is None:
        return list(parts)
    return list(parts[2:] if info[1] else parts[1:])

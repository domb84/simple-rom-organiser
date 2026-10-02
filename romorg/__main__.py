"""Entry point: ``python -m romorg`` and the zipapp (``romorg.__main__:main``)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from romorg.server import main as server_main

    return server_main(argv)


if __name__ == "__main__":
    sys.exit(main())

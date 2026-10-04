"""Entry point: ``python -m romorg`` and the zipapp (``romorg.__main__:main``)."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "--self-check":             # CHD engine / bundled tools check (build + smoke test)
        from romorg import selfcheck

        return selfcheck.main(args[1:])
    from romorg.server import main as server_main

    return server_main(argv)


if __name__ == "__main__":
    sys.exit(main())

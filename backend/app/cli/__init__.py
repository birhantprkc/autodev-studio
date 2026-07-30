"""``codejury`` — the terminal client.

Bare ``codejury`` opens the interactive shell. Everything the shell can do is
also reachable as a subcommand, because a tool that only works when a human is
watching can't be put in a Makefile or a CI job.

The previous entry point started a web server; that is now ``codejury serve``.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from .entry import main as _main

    return _main(argv if argv is not None else sys.argv[1:])


__all__ = ["main"]

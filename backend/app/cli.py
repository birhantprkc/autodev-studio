"""Console entry point: ``autodev`` starts the server.

Thin wrapper over uvicorn so an installed copy has a real command instead of a
uvicorn incantation the user has to remember.
"""

from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autodev",
        description="Start AutoDev Studio (web UI + API).",
    )
    parser.add_argument(
        "--host", default=os.environ.get("HOST", "127.0.0.1"),
        help="interface to bind (default: 127.0.0.1; use 0.0.0.0 to expose it)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8017")),
        help="port to listen on (default: 8017)",
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    args = parser.parse_args(argv)

    import uvicorn

    print(f"AutoDev Studio → http://{args.host}:{args.port}  (API docs at /docs)")
    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

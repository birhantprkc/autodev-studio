"""The web UI, now a subcommand.

CodeJury started as a browser app and that surface still earns its place: a
team wants a board on a wall and a URL to send someone. It is no longer the
default, though, and nothing in the terminal client depends on it — both drive
``core`` directly, against the same database.
"""

from __future__ import annotations

import os
import socket
import threading
import time


def run_foreground(host: str, port: int, reload: bool = False) -> int:
    """``codejury serve`` — the behaviour the old entry point had."""
    import uvicorn

    print(f"CodeJury → http://{host}:{port}  (API docs at /docs)")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload)
    return 0


def _free_port(port: int, host: str = "127.0.0.1") -> int:
    """First free port at or above ``port``.

    A person typing ``/serve`` inside a session does not want to be told the
    port is busy; they want a URL.
    """
    for candidate in range(port, port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, candidate))
                return candidate
            except OSError:
                continue
    return port


def start_background_server(port: int = 8017, host: str = "127.0.0.1") -> str:
    """Start the web UI on a daemon thread and return its URL.

    Daemon, so quitting the shell takes the server with it — a stray uvicorn
    surviving its parent is exactly the "stale server serving stale imports"
    trap that costs an afternoon of debugging when the code underneath it moves.
    """
    import uvicorn

    from ..main import app

    port = _free_port(port, host)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    # uvicorn installs signal handlers by default, which it may not do off the
    # main thread — and we don't want it stealing Ctrl-C from the prompt anyway.
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    thread = threading.Thread(target=server.run, name="codejury-web", daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)

    admin_hint = "" if os.environ.get("ADMIN_PASSWORD") else \
        "  (first boot prints the admin password to this terminal)"
    return f"http://{host}:{port}{admin_hint}"

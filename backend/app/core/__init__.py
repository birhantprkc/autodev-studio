"""Use-case layer: everything the product can *do*, independent of how it is driven.

CodeJury has two front ends — a terminal client and an HTTP server — and they
must not drift. So the operations live here, as plain functions over a
``Session``, and both surfaces are thin adapters over the same call:

    routers/tasks.py    →  core.tasks.approve(db, task_id)
    cli/commands.py     →  core.tasks.approve(db, task_id)

The rule this layer follows is that it knows nothing about its caller. No
FastAPI types, no ``HTTPException``, no Rich console. Failures are raised as
``CoreError`` with an HTTP-shaped status code — the status is a *severity
vocabulary* both surfaces already understand (404 missing, 409 wrong state, 403
refused), which the server maps to a response and the terminal maps to a
message. Below this sits ``services/``, which does the actual work.
"""

from .errors import CoreError

__all__ = ["CoreError"]

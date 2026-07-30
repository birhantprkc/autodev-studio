from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from .config import settings
from .core import CoreError
from .database import engine, init_db
from .routers import (
    agents,
    costs,
    jury,
    kb_tools,
    overview,
    pages,
    repos,
    sessions,
    settings_api,
    tasks,
)
from .routers import auth as auth_router
from .seed import seed_demo_data
from .services import judges, runtime_settings
from .services.auth import ensure_bootstrap_admin, require_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as db:
        ensure_bootstrap_admin(db)       # first boot: admin + generated password (logged once)
        runtime_settings.apply_overrides(db)  # Settings-screen values over env
        # After the overrides, so the default panel can be spread across the
        # providers the operator has actually configured.
        judges.ensure_seeded(db)
    if settings.seed_on_startup:
        seed_demo_data()
    yield


app = FastAPI(title="CodeJury API", version="0.1.0", lifespan=lifespan)


@app.exception_handler(CoreError)
def _core_error(request: Request, exc: CoreError) -> JSONResponse:
    """The use-case layer refuses work with a CoreError; its status codes are
    already HTTP-shaped, so this is the whole translation. Keeps the routers
    free of try/except and keeps the terminal client and the API reporting the
    same refusal for the same reason."""
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})

# No CORS middleware on purpose: the frontend is served by this same app
# (same-origin), so cross-origin API access isn't needed — and the previous
# wildcard-origins-with-credentials config made Starlette echo ANY origin on
# credentialed requests, undermining the cookie session's CSRF protections.

# /auth handles its own access (login must work signed-out); everything else
# requires a signed-in user, with member/admin checks on individual routes.
app.include_router(auth_router.router)
_authed = [Depends(require_user)]
app.include_router(overview.router, dependencies=_authed)
app.include_router(costs.router, dependencies=_authed)
app.include_router(repos.router, dependencies=_authed)
app.include_router(sessions.router, dependencies=_authed)
app.include_router(tasks.router, dependencies=_authed)
app.include_router(agents.router, dependencies=_authed)
app.include_router(settings_api.router)
app.include_router(jury.router)
# Dev-agent index tools: authenticated by a per-run token minted in-process, so
# the shim (a separate process) reaches the graph + vector store this one owns.
app.include_router(kb_tools.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


# Frontend: static assets + HTML screens (mounted last so /api routes win).
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(pages.router)

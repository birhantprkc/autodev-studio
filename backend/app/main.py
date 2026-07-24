from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from .config import settings
from .database import engine, init_db
from .routers import agents, costs, overview, pages, repos, sessions, settings_api, tasks
from .routers import auth as auth_router
from .seed import seed_demo_data
from .services import runtime_settings
from .services.auth import ensure_bootstrap_admin, require_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with Session(engine) as db:
        ensure_bootstrap_admin(db)       # first boot: admin + generated password (logged once)
        runtime_settings.apply_overrides(db)  # Settings-screen values over env
    if settings.seed_on_startup:
        seed_demo_data()
    yield
    # Clean shutdown of the embedded Qdrant vector store.
    from .services import local_rag
    local_rag.close()


app = FastAPI(title="AutoDev Studio API", version="0.1.0", lifespan=lifespan)

# Open CORS for local dev — the frontend is served from the same app.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


# Frontend: static assets + HTML screens (mounted last so /api routes win).
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
app.include_router(pages.router)

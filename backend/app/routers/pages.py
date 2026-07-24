"""Serves the integrated frontend screens (Jinja templates).

Every screen except /login requires a signed-in user; anonymous visitors are
redirected to /login (the API returns 401s — the redirect here is just for
address-bar navigation).
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..services.auth import current_user

_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

router = APIRouter(tags=["pages"], include_in_schema=False)


def _asset_version() -> int:
    """Newest mtime of the static assets — cache-busting query param so
    browsers refetch them whenever they change."""
    try:
        return max(
            int((_STATIC_DIR / f).stat().st_mtime)
            for f in ("app.js", "api.js", "app.css", "icons.js")
        )
    except OSError:
        return 0


def _page(request: Request, user: User | None, template: str, nav: str):
    if user is None:
        return RedirectResponse("/login", status_code=302)
    return _TEMPLATES.TemplateResponse(
        request, template,
        {"active_nav": nav, "asset_version": _asset_version(),
         "username": user.username, "role": user.role},
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(current_user)):
    if user is not None:
        return RedirectResponse("/", status_code=302)
    return _TEMPLATES.TemplateResponse(
        request, "login.html", {"asset_version": _asset_version()}
    )


@router.get("/", response_class=HTMLResponse)
def scope_page(request: Request, user: User | None = Depends(current_user)):
    return _page(request, user, "scope.html", "scope")


@router.get("/board", response_class=HTMLResponse)
def board_page(request: Request, user: User | None = Depends(current_user)):
    return _page(request, user, "board.html", "board")


@router.get("/agents", response_class=HTMLResponse)
def agents_page(request: Request, user: User | None = Depends(current_user)):
    return _page(request, user, "agents.html", "agents")


@router.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(request: Request, user: User | None = Depends(current_user)):
    return _page(request, user, "knowledge.html", "knowledge")


@router.get("/costs", response_class=HTMLResponse)
def costs_page(request: Request, user: User | None = Depends(current_user)):
    return _page(request, user, "costs.html", "costs")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User | None = Depends(current_user)):
    return _page(request, user, "settings.html", "settings")


# Old bookmarks from the previous layout.
@router.get("/pm")
@router.get("/dev")
def old_agents_redirect():
    return RedirectResponse("/agents", status_code=301)


@router.get("/analysis")
def old_knowledge_redirect():
    return RedirectResponse("/knowledge", status_code=301)


@router.get("/qa")
def old_qa_redirect():
    return RedirectResponse("/board", status_code=301)

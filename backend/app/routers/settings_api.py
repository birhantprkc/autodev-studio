"""Runtime settings: read for any signed-in user, write for admins."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..services import auth, runtime_settings

# Prefixed /api so the JSON endpoints never shadow the /settings HTML page.
router = APIRouter(prefix="/api/settings", tags=["settings"])


class UpdateSettingsBody(BaseModel):
    values: dict


class PresetBody(BaseModel):
    provider: str


@router.get("", dependencies=[Depends(auth.require_user)])
def get_settings() -> dict:
    return runtime_settings.view()


@router.put("", dependencies=[Depends(auth.require_admin)])
def put_settings(body: UpdateSettingsBody, db: Session = Depends(get_session)) -> dict:
    try:
        changed = runtime_settings.update(db, body.values)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    view = runtime_settings.view()
    view["changed"] = changed
    return view


@router.post("/preset", dependencies=[Depends(auth.require_admin)])
def apply_preset(body: PresetBody, db: Session = Depends(get_session)) -> dict:
    """Point every pipeline stage at one provider (with recommended per-stage models)."""
    try:
        changed = runtime_settings.apply_provider_preset(db, body.provider)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    view = runtime_settings.view()
    view["changed"] = changed
    return view


@router.post("/backends/refresh", dependencies=[Depends(auth.require_user)])
def refresh_backends() -> dict:
    """Re-probe every agentic CLI (the 'Re-check' button — e.g. after the operator
    installed or logged into a tool in their own terminal)."""
    from ..services import agent_backends

    agent_backends.refresh()
    return runtime_settings.view()


@router.post("/backends/{backend_id}/install", dependencies=[Depends(auth.require_admin)])
def install_backend(backend_id: str) -> dict:
    """One-click install of an agentic CLI via its official installer (npm/pip/
    vendor script), then re-detect. Admin only — it runs a package manager on the
    host. Returns the installer output plus a fresh settings view."""
    from ..services import agent_backends

    res = agent_backends.install(backend_id)
    view = runtime_settings.view()
    view["install"] = {"backend": backend_id, "ok": res["ok"], "output": res["output"]}
    return view


@router.post("/embeddings/test", dependencies=[Depends(auth.require_user)])
def test_embeddings() -> dict:
    """One tiny embed with the currently saved embedding settings — proves the
    engine (local model or the operator's API endpoint) actually works."""
    from ..services import local_rag

    return local_rag.embedding_probe()

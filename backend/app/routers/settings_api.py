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


@router.get("/providers/{provider_id}/models", dependencies=[Depends(auth.require_user)])
def provider_models(provider_id: str) -> dict:
    """Live model ids from the provider's own API, so the picker only ever offers
    models that currently exist. Returns [] if no key is set or the fetch fails."""
    from ..services import providers

    return {"provider": provider_id, "models": providers.fetch_models(provider_id)}


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


@router.post("/graph/test", dependencies=[Depends(auth.require_user)])
def test_graph() -> dict:
    """Locate the code-graph binary and read its version — proves the knowledge
    engine can actually run on this host with the current settings."""
    from ..services.knowledge import graph

    return graph.probe()


@router.post("/search/test", dependencies=[Depends(auth.require_user)])
def test_search() -> dict:
    """Report which lexical engine is live (ripgrep, or the git grep fallback).
    The two differ in recall, so which one is answering is worth showing rather
    than leaving the operator to infer it from results."""
    from ..services import search

    return search.probe()

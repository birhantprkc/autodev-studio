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

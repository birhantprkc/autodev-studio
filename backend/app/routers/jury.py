"""The review jury's roster: read for any signed-in user, edit for admins.

Editing the panel changes what every future delivery is judged against — and
each seated judge is a paid LLM call per review round — so writes are admin-only,
matching Settings.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from ..database import get_session
from ..services import auth, judges

router = APIRouter(prefix="/api/jury", tags=["jury"])


class JudgeCreate(BaseModel):
    name: str
    persona: str = "custom"
    enabled: bool = True
    provider: str = ""
    model: str = ""
    focus: str = ""


class JudgeUpdate(BaseModel):
    name: str | None = None
    persona: str | None = None
    enabled: bool | None = None
    position: int | None = None
    provider: str | None = None
    model: str | None = None
    focus: str | None = None


class JudgeMove(BaseModel):
    delta: int = 1          # -1 = up the panel, +1 = down


@router.get("", dependencies=[Depends(auth.require_user)])
def get_jury(db: Session = Depends(get_session)) -> dict:
    judges.ensure_seeded(db)
    return judges.view(db)


@router.post("/judges", dependencies=[Depends(auth.require_admin)])
def add_judge(body: JudgeCreate, db: Session = Depends(get_session)) -> dict:
    try:
        judges.create(db, **body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return judges.view(db)


@router.patch("/judges/{judge_id}", dependencies=[Depends(auth.require_admin)])
def edit_judge(judge_id: int, body: JudgeUpdate, db: Session = Depends(get_session)) -> dict:
    values = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        judges.update(db, judge_id, values)
    except ValueError as exc:
        raise HTTPException(404 if "No judge" in str(exc) else 422, str(exc))
    return judges.view(db)


@router.post("/judges/{judge_id}/move", dependencies=[Depends(auth.require_admin)])
def move_judge(judge_id: int, body: JudgeMove, db: Session = Depends(get_session)) -> dict:
    try:
        judges.move(db, judge_id, body.delta)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return judges.view(db)


@router.delete("/judges/{judge_id}", dependencies=[Depends(auth.require_admin)])
def drop_judge(judge_id: int, db: Session = Depends(get_session)) -> dict:
    try:
        judges.delete(db, judge_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return judges.view(db)


@router.post("/spread", dependencies=[Depends(auth.require_admin)])
def spread(db: Session = Depends(get_session)) -> dict:
    """Give each seated judge a different configured provider. A panel whose
    members all run on one model agrees with itself, including where it's wrong."""
    changed = judges.spread_providers(db)
    view = judges.view(db)
    view["changed"] = changed
    return view


@router.post("/reset", dependencies=[Depends(auth.require_admin)])
def reset(db: Session = Depends(get_session)) -> dict:
    """Re-seat the panel CodeJury ships with, discarding the current roster."""
    judges.reset_to_defaults(db)
    return judges.view(db)

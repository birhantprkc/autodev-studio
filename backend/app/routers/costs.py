"""Cost + token breakdown: totals → per scope → per ticket → per agent.

HTTP adapter only — the aggregation lives in ``core.costs``.
"""

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..core import costs as core_costs
from ..database import get_session

router = APIRouter(prefix="/costs", tags=["costs"])


@router.get("/data")
def costs(repo_id: int | None = None, db: Session = Depends(get_session)) -> dict:
    return core_costs.breakdown(db, repo_id)

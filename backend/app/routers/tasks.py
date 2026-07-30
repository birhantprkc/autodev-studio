"""Tasks + the Kanban board that tracks them through the agent pipeline.

HTTP adapter only. The behaviour lives in ``core.tasks`` so the terminal client
runs the identical code path; ``CoreError`` is translated to an HTTP response by
the handler registered in ``main.py``.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from ..core import tasks as core_tasks
from ..database import get_session
from ..models import Task, User
from ..schemas import Board, BoardColumn, UpdateTaskRequest
from ..services.auth import require_member


class RequestChangesBody(BaseModel):
    note: str | None = None


router = APIRouter(prefix="/tasks", tags=["board"])


@router.get("", response_model=list[Task])
def list_tasks(
    repo_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_session),
) -> list[Task]:
    return core_tasks.listing(db, repo_id=repo_id, status=status)


@router.get("/board", response_model=Board)
def get_board(repo_id: int | None = None, db: Session = Depends(get_session)) -> Board:
    return Board(columns=[BoardColumn(**lane) for lane in core_tasks.board(db, repo_id)])


@router.get("/pipeline")
def get_pipeline(repo_id: int | None = None, db: Session = Depends(get_session)) -> dict:
    return core_tasks.pipeline(db, repo_id)


@router.get("/{task_id}", response_model=Task)
def get_task(task_id: int, db: Session = Depends(get_session)) -> Task:
    return core_tasks.require(db, task_id)


@router.patch("/{task_id}", response_model=Task, dependencies=[Depends(require_member)])
def update_task(task_id: int, body: UpdateTaskRequest, db: Session = Depends(get_session)) -> Task:
    return core_tasks.update(db, task_id, body.model_dump(exclude_unset=True))


@router.post("/{task_id}/approve", response_model=Task, dependencies=[Depends(require_member)])
def approve_task(task_id: int, db: Session = Depends(get_session)) -> Task:
    """PM approval gate: a task must be approved before any agent pipeline can run."""
    return core_tasks.approve(db, task_id)


# NOTE: There is deliberately no per-ticket run endpoint. A scope is the unit of
# work — every approved subtask is delivered together on one branch as one PR.
# Run a scope via POST /sessions/{id}/run-scope.


@router.get("/{task_id}/review")
def get_review(task_id: int, db: Session = Depends(get_session)) -> dict:
    """Real final-review payload: actual git diff + actual QA/Review agent output."""
    return core_tasks.review(db, task_id)


@router.post("/{task_id}/create-pr")
def create_pr(task_id: int, user: User = Depends(require_member),
              db: Session = Depends(get_session)) -> dict:
    """Human-triggered PR creation from the board — opened as the acting user's
    own GitHub account when they've connected one (see core.tasks.create_pr)."""
    return core_tasks.create_pr(db, task_id, user)


@router.post("/{task_id}/merge", dependencies=[Depends(require_member)])
def merge_pr(task_id: int, db: Session = Depends(get_session)) -> dict:
    """Mark the delivery done — every sibling subtask of the scope together."""
    return core_tasks.merge(db, task_id)


@router.post("/{task_id}/request-changes", dependencies=[Depends(require_member)])
def request_changes(task_id: int, body: RequestChangesBody | None = None,
                    db: Session = Depends(get_session)) -> dict:
    """Send the scope back for changes; it returns to the Scoped lane, still approved."""
    return core_tasks.request_changes(db, task_id, (body.note if body else None) or "")


@router.get("/{task_id}/detail")
def task_detail(task_id: int, db: Session = Depends(get_session)) -> dict:
    """Full ticket detail for the drawer: the ticket, its scope, and its runs."""
    return core_tasks.detail(db, task_id)

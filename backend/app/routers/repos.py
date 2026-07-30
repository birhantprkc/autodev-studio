"""Repositories and their knowledge bases.

HTTP adapter only — the behaviour lives in ``core.repos``.
"""

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..core import repos as core_repos
from ..database import get_session
from ..models import Repo
from ..schemas import IngestRepoRequest
from ..services.auth import require_member

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/repos", tags=["repos"])


@router.get("", response_model=list[Repo])
def list_repos(db: Session = Depends(get_session)) -> list[Repo]:
    return core_repos.listing(db)


@router.post("/ingest", response_model=Repo, status_code=201,
             dependencies=[Depends(require_member)])
def ingest_repo(body: IngestRepoRequest, db: Session = Depends(get_session)) -> Repo:
    return core_repos.ingest(db, body.git_url, body.default_branch)


@router.get("/{repo_id}", response_model=Repo)
def get_repo(repo_id: int, db: Session = Depends(get_session)) -> Repo:
    return core_repos.require(db, repo_id)


@router.post("/{repo_id}/reindex", response_model=Repo,
             dependencies=[Depends(require_member)])
def reindex_repo(repo_id: int, db: Session = Depends(get_session)) -> Repo:
    return core_repos.reindex(db, repo_id)


@router.delete("/{repo_id}", status_code=204, dependencies=[Depends(require_member)])
def delete_repo(repo_id: int, db: Session = Depends(get_session)) -> None:
    """Remove a repository and everything derived from it: sessions, messages,
    tasks, runs, logs, plus (best-effort) its workspace clone and knowledge dir."""
    core_repos.delete(db, repo_id)


@router.get("/{repo_id}/knowledge")
def repo_knowledge(repo_id: int, db: Session = Depends(get_session)) -> dict:
    """The structured, multi-view knowledge generated for this repo, grouped by
    domain (architecture, modules, features, workflows, …) for the UI."""
    return core_repos.knowledge(db, repo_id)

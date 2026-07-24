import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..models import AgentRun, ChatMessage, KBStatus, LogEntry, Repo, ScopeSession, Task
from ..schemas import IngestRepoRequest
from ..services import background, deepwiki, git_ops
from ..services.auth import require_member

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/repos", tags=["repos"])


def _parse_git_url(url: str) -> tuple[str, str]:
    """Best-effort (org, name) from a git URL."""
    cleaned = url.strip().rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    parts = [p for p in cleaned.replace(":", "/").split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "unknown", parts[-1] if parts else "repo"


def _derive_prefix(name: str) -> str:
    """Story-key prefix from a repo name, e.g. 'payments-api' -> 'PA'."""
    words = [w for w in name.replace("_", "-").split("-") if w]
    letters = "".join(w[0] for w in words[:3]).upper()
    return letters or name[:2].upper() or "TASK"


@router.get("", response_model=list[Repo])
def list_repos(db: Session = Depends(get_session)) -> list[Repo]:
    return db.exec(select(Repo).order_by(Repo.created_at.desc())).all()


@router.post("/ingest", response_model=Repo, status_code=201,
             dependencies=[Depends(require_member)])
def ingest_repo(body: IngestRepoRequest, db: Session = Depends(get_session)) -> Repo:
    org, name = _parse_git_url(body.git_url)
    repo = Repo(
        name=name,
        org=org,
        git_url=body.git_url,
        default_branch=body.default_branch,
        key_prefix=_derive_prefix(name),
        kb_status=KBStatus.pending.value,
    )
    db.add(repo)
    db.commit()
    db.refresh(repo)

    # Kick off DeepWiki indexing off-thread; the UI polls kb_status/kb_progress.
    background.submit(deepwiki.ingest, repo.id)
    return repo


@router.get("/{repo_id}", response_model=Repo)
def get_repo(repo_id: int, db: Session = Depends(get_session)) -> Repo:
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(404, "Repo not found")
    return repo


@router.post("/{repo_id}/reindex", response_model=Repo,
             dependencies=[Depends(require_member)])
def reindex_repo(repo_id: int, db: Session = Depends(get_session)) -> Repo:
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(404, "Repo not found")
    background.submit(deepwiki.ingest, repo.id)
    return repo


@router.delete("/{repo_id}", status_code=204, dependencies=[Depends(require_member)])
def delete_repo(repo_id: int, db: Session = Depends(get_session)) -> None:
    """Remove a repository and everything derived from it: sessions, messages,
    tasks, runs, logs, plus (best-effort) its workspace clone and knowledge dir."""
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(404, "Repo not found")

    task_ids = db.exec(select(Task.id).where(Task.repo_id == repo_id)).all()
    if task_ids:
        run_ids = db.exec(select(AgentRun.id).where(AgentRun.task_id.in_(task_ids))).all()
        if run_ids:
            for log in db.exec(select(LogEntry).where(LogEntry.run_id.in_(run_ids))).all():
                db.delete(log)
            for run in db.exec(select(AgentRun).where(AgentRun.id.in_(run_ids))).all():
                db.delete(run)
        for task in db.exec(select(Task).where(Task.id.in_(task_ids))).all():
            db.delete(task)

    session_ids = db.exec(select(ScopeSession.id).where(ScopeSession.repo_id == repo_id)).all()
    if session_ids:
        for msg in db.exec(select(ChatMessage).where(ChatMessage.session_id.in_(session_ids))).all():
            db.delete(msg)
        for sess in db.exec(select(ScopeSession).where(ScopeSession.id.in_(session_ids))).all():
            db.delete(sess)

    db.delete(repo)
    db.commit()

    # On-disk artifacts: the workspace clone and the structured-knowledge dir.
    # Best-effort — a failure here must not resurrect the DB rows.
    for path in (git_ops.workdir(repo.git_url),
                 Path(settings.knowledge_dir).resolve() / git_ops.slug(repo.git_url)):
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", path, exc)


@router.get("/{repo_id}/knowledge")
def repo_knowledge(repo_id: int, db: Session = Depends(get_session)) -> dict:
    """The structured, multi-view knowledge generated for this repo, grouped by
    domain (architecture, modules, features, workflows, …) for the UI."""
    repo = db.get(Repo, repo_id)
    if repo is None:
        raise HTTPException(404, "Repo not found")
    from ..services.knowledge import retriever as knowledge_retriever

    return knowledge_retriever.views(repo.git_url)

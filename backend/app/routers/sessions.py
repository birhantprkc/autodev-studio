"""Scope-chat: converse with the repo knowledge base, then have the PM agent
turn the conversation into real tickets.

HTTP adapter only — the behaviour lives in ``core.scoping``.
"""

import logging

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..core import scoping
from ..database import get_session
from ..models import ChatMessage, MessageRole, Repo, ScopeSession
from ..schemas import (
    CreateSessionRequest,
    CreateTasksResponse,
    PostMessageRequest,
    SessionDetail,
)
from ..services import deepwiki
from ..services.auth import require_member

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["scope-chat"])


@router.post("", response_model=ScopeSession, status_code=201,
             dependencies=[Depends(require_member)])
def create_session(body: CreateSessionRequest, db: Session = Depends(get_session)) -> ScopeSession:
    return scoping.create(db, body.repo_id, kind=body.kind, title=body.title or "")


@router.get("", response_model=list[ScopeSession])
def list_sessions(repo_id: int | None = None, kind: str | None = None,
                  db: Session = Depends(get_session)) -> list[ScopeSession]:
    return scoping.listing(db, repo_id=repo_id, kind=kind)


@router.get("/{session_id}", response_model=SessionDetail)
def get_session_detail(session_id: int, db: Session = Depends(get_session)) -> SessionDetail:
    return SessionDetail(**scoping.detail(db, session_id))


@router.post("/{session_id}/messages", response_model=SessionDetail,
             dependencies=[Depends(require_member)])
def post_message(session_id: int, body: PostMessageRequest,
                 db: Session = Depends(get_session)) -> SessionDetail:
    """Plain repo Q&A against the knowledge base — no scoping, no ticket drafting.

    This is the "kb" chat kind, distinct from the PM's scope-turn loop below.
    """
    session = scoping.require(db, session_id)
    repo = db.get(Repo, session.repo_id)

    db.add(ChatMessage(session_id=session_id, role=MessageRole.user.value, content=body.content))
    db.commit()

    dw_messages = [
        {"role": "user" if m.role == MessageRole.user.value else "assistant", "content": m.content}
        for m in scoping.messages(db, session_id)
    ]
    try:
        answer = deepwiki.ask(repo.git_url, dw_messages)
        sources = deepwiki.extract_sources(answer)
    except Exception as exc:  # noqa: BLE001
        logger.error("DeepWiki ask failed: %s", exc)
        answer = f"(Knowledge base error: {exc})"
        sources = []

    db.add(ChatMessage(session_id=session_id, role=MessageRole.agent.value, agent_type="pm",
                       content=answer, sources=sources))
    db.commit()
    return get_session_detail(session_id, db)


@router.post("/{session_id}/scope-turn", response_model=SessionDetail,
             dependencies=[Depends(require_member)])
def scope_turn(session_id: int, body: PostMessageRequest,
               db: Session = Depends(get_session)) -> SessionDetail:
    """One turn of the agent-led PM scoping chat: the PM agent asks a clarifying
    question or locks the scope."""
    scoping.scope_turn(db, session_id, body.content)
    return get_session_detail(session_id, db)


@router.post("/{session_id}/run-scope", response_model=ScopeSession,
             dependencies=[Depends(require_member)])
def run_scope(session_id: int, db: Session = Depends(get_session)) -> ScopeSession:
    """Run the WHOLE scope as one deliverable: all approved subtasks on one branch
    → one PR. (Runs off-thread.)"""
    return scoping.run_scope(db, session_id)


@router.post("/{session_id}/create-tasks", response_model=CreateTasksResponse,
             dependencies=[Depends(require_member)])
def create_tasks(session_id: int, db: Session = Depends(get_session)) -> CreateTasksResponse:
    """PM agent drafts tickets from the locked scope. Tickets are created
    UNAPPROVED — no pipeline can run on them until a human approves."""
    return CreateTasksResponse(created=scoping.draft_tickets(db, session_id))

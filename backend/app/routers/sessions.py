"""Scope-chat: converse with the repo knowledge base (DeepWiki RAG), then have
the PM agent (Claude) turn the conversation into real tasks."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import ChatMessage, MessageRole, Repo, ScopeSession, Task, TaskStatus
from ..schemas import (
    CreateSessionRequest,
    CreateTasksResponse,
    PostMessageRequest,
    SessionDetail,
)
from ..services import background, deepwiki, git_ops, orchestrator, pm_agent, precision
from ..services.auth import require_member
from ..services.knowledge import retriever as knowledge_retriever

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["scope-chat"])


def _next_key(db: Session, repo: Repo) -> str:
    count = len(db.exec(select(Task.id).where(Task.repo_id == repo.id)).all())
    return f"{repo.key_prefix}-{101 + count}"


@router.post("", response_model=ScopeSession, status_code=201,
             dependencies=[Depends(require_member)])
def create_session(body: CreateSessionRequest, db: Session = Depends(get_session)) -> ScopeSession:
    if db.get(Repo, body.repo_id) is None:
        raise HTTPException(404, "Repo not found")
    session = ScopeSession(repo_id=body.repo_id, kind=body.kind, title=body.title or "New requirement")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("", response_model=list[ScopeSession])
def list_sessions(repo_id: int | None = None, kind: str | None = None,
                  db: Session = Depends(get_session)) -> list[ScopeSession]:
    q = select(ScopeSession).order_by(ScopeSession.created_at.desc())
    if repo_id is not None:
        q = q.where(ScopeSession.repo_id == repo_id)
    if kind is not None:
        q = q.where(ScopeSession.kind == kind)
    return db.exec(q).all()


@router.get("/{session_id}", response_model=SessionDetail)
def get_session_detail(session_id: int, db: Session = Depends(get_session)) -> SessionDetail:
    session = db.get(ScopeSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    messages = db.exec(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
    ).all()
    return SessionDetail(session=session, messages=messages)


@router.post("/{session_id}/messages", response_model=SessionDetail,
             dependencies=[Depends(require_member)])
def post_message(session_id: int, body: PostMessageRequest, db: Session = Depends(get_session)) -> SessionDetail:
    session = db.get(ScopeSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    repo = db.get(Repo, session.repo_id)

    db.add(ChatMessage(session_id=session_id, role=MessageRole.user.value, content=body.content))
    db.commit()

    # Build the conversation and ask the repo's knowledge base (DeepWiki RAG).
    msgs = db.exec(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
    ).all()
    dw_messages = [
        {"role": "user" if m.role == MessageRole.user.value else "assistant", "content": m.content}
        for m in msgs
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
def scope_turn(session_id: int, body: PostMessageRequest, db: Session = Depends(get_session)) -> SessionDetail:
    """One turn of the agent-led PM scoping chat: the PM agent (OpenAI) asks a
    clarifying question or locks the scope."""
    session = db.get(ScopeSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    repo = db.get(Repo, session.repo_id)

    db.add(ChatMessage(session_id=session_id, role=MessageRole.user.value, content=body.content))
    # Name the scope after the first user message so the scope switcher is legible.
    if session.title in (None, "", "Feature scoping", "New requirement"):
        session.title = body.content.strip()[:60]
        db.add(session)
    db.commit()

    msgs = db.exec(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
    ).all()
    history = [{"role": m.role, "content": m.content} for m in msgs]

    tree = git_ops.tree_for(repo.git_url)
    # Agentic PM turn: bootstrap knowledge, then the PM retrieves more on demand
    # (up to pm_max_retrieval_rounds) before it asks a question or locks the scope.
    turn = pm_agent.scope_turn(f"{repo.org}/{repo.name}", repo.git_url,
                               repo.kb_overview or "", history, tree)
    scope = turn["scope"] if (turn["ready"] and turn["scope"]) else None
    # Surface what the PM looked up this turn (transparency in the chat log).
    retrieved = turn.get("retrieved") or []
    retr_note = (f"\n\n_[looked up: {'; '.join(retrieved[:4])}]_" if retrieved else "")

    # Gap signal: how hard the PM had to work the KB this turn (0 rounds = the
    # bootstrap context sufficed). Feeds gap-driven view refresh + benchmarks.
    from ..services.knowledge import gaps
    gaps.record(repo.git_url, body.content, turn.get("retrieval_rounds") or 0,
                turn.get("retrieved") or [])

    # Accumulate PM scoping cost on the session (no Task/AgentRun exists yet).
    session.pm_tokens_input = (session.pm_tokens_input or 0) + (turn.get("tokens_in") or 0)
    session.pm_tokens_output = (session.pm_tokens_output or 0) + (turn.get("tokens_out") or 0)
    session.pm_cost_usd = round((session.pm_cost_usd or 0.0) + (turn.get("cost") or 0.0), 6)
    db.add(session)

    note = ""
    if scope:
        session.requirement_summary = scope.get("summary")
        session.acceptance_criteria = scope.get("acceptance_criteria") or []
        session.affected_files = scope.get("affected_files") or []
        session.status = "scoped"
        db.add(session)
        # Scope (re)locked → any not-yet-started draft tickets are now stale. Clear
        # them so tickets always reflect the CURRENT scope; re-draft is required.
        stale = db.exec(
            select(Task).where(
                Task.session_id == session_id,
                Task.status.in_([TaskStatus.backlog.value, TaskStatus.scoped.value]),
            )
        ).all()
        if stale:
            for t in stale:
                db.delete(t)
            note = (f"\n\n(Scope changed — cleared {len(stale)} previous draft ticket"
                    f"{'s' if len(stale) != 1 else ''}. Click \"Draft tickets\" to regenerate from the new scope.)")

    db.add(ChatMessage(
        session_id=session_id, role=MessageRole.agent.value, agent_type="pm",
        content=turn["message"] + retr_note + note,
        sources=(scope.get("affected_files", []) if scope else []),
        tokens=turn["tokens_in"] + turn["tokens_out"],
    ))
    db.commit()
    return get_session_detail(session_id, db)


@router.post("/{session_id}/run-scope", response_model=ScopeSession,
             dependencies=[Depends(require_member)])
def run_scope(session_id: int, db: Session = Depends(get_session)) -> ScopeSession:
    """Run the WHOLE scope as one deliverable: all approved subtasks on one branch
    → one PR. (Runs off-thread.)"""
    session = db.get(ScopeSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    approved = db.exec(
        select(Task).where(
            Task.session_id == session_id, Task.approved == True,  # noqa: E712
            Task.status.in_([TaskStatus.scoped.value, TaskStatus.backlog.value]),
        )
    ).all()
    if not approved:
        raise HTTPException(409, "No approved subtasks to run — approve tickets first")
    background.submit(orchestrator.run_scope, session_id)
    return session


@router.post("/{session_id}/create-tasks", response_model=CreateTasksResponse,
             dependencies=[Depends(require_member)])
def create_tasks(session_id: int, db: Session = Depends(get_session)) -> CreateTasksResponse:
    """PM agent (OpenAI) drafts tickets from the locked scope. Tickets are created
    UNAPPROVED — no pipeline can run on them until a human approves."""
    session = db.get(ScopeSession, session_id)
    if session is None:
        raise HTTPException(404, "Session not found")
    repo = db.get(Repo, session.repo_id)
    if not session.acceptance_criteria:
        raise HTTPException(409, "Scope is not locked yet — keep clarifying with the PM agent first")

    scope = {
        "summary": session.requirement_summary,
        "acceptance_criteria": session.acceptance_criteria,
        "affected_files": session.affected_files,
    }
    # Ground the tickets in the RIGHT knowledge: a story-scoped slice from Deep
    # Analysis, falling back to a focused DeepWiki lookup (best-effort).
    context = ""
    try:
        context = precision.retrieve(session.requirement_summary or "",
                                     use_case="story-breakdown", repo_url=repo.git_url)
    except Exception:  # noqa: BLE001
        context = ""
    if not context:
        try:
            context = deepwiki.ask(
                repo.git_url,
                [{"role": "user", "content": f"Which files are most relevant to: {session.requirement_summary}"}],
                timeout=120,
            )
        except Exception:  # noqa: BLE001
            context = ""

    # Structured-knowledge digest for the locked scope (architecture / modules /
    # features), alongside the chunk-level file context above.
    try:
        knowledge = knowledge_retriever.scope_context(repo.git_url, session.requirement_summary or "")
    except Exception:  # noqa: BLE001
        knowledge = ""

    drafted = pm_agent.draft_tickets(f"{repo.org}/{repo.name}", scope, context,
                                     git_ops.tree_for(repo.git_url), knowledge=knowledge)
    if not drafted["tickets"]:
        raise HTTPException(502, f"PM agent could not draft tickets: {drafted.get('error') or 'no tickets returned'}")
    # Deterministic grounding: git-grep every pinned symbol in the real clone and
    # fix mislocalized files / tag invented symbols before the tickets persist.
    try:
        drafted["tickets"] = pm_agent.ground_tickets(repo.git_url, drafted["tickets"])
    except Exception as exc:  # noqa: BLE001 — grounding is best-effort
        logger.warning("ticket grounding skipped: %s", exc)

    # Accumulate the draft-tickets PM cost on the session too.
    session.pm_tokens_input = (session.pm_tokens_input or 0) + (drafted.get("tokens_in") or 0)
    session.pm_tokens_output = (session.pm_tokens_output or 0) + (drafted.get("tokens_out") or 0)
    session.pm_cost_usd = round((session.pm_cost_usd or 0.0) + (drafted.get("cost") or 0.0), 6)
    db.add(session)
    db.commit()

    created: list[Task] = []
    for t in drafted["tickets"][:6]:
        task = Task(
            key=_next_key(db, repo),
            repo_id=repo.id,
            session_id=session_id,
            title=str(t.get("title"))[:200],
            description=str(t.get("description", "")),
            acceptance_criteria=t.get("acceptance_criteria") or [],
            affected_files=t.get("affected_files") or [],
            target_symbols=t.get("target_symbols") or [],
            status=TaskStatus.scoped.value,
            priority=t.get("priority") if t.get("priority") in ("low", "medium", "high") else "medium",
            approved=False,
            current_agent="pm",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        created.append(task)
    return CreateTasksResponse(created=created)

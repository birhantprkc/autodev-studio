"""Tasks + the Kanban board that tracks them through the agent pipeline."""

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import settings
from ..database import get_session
from ..models import (
    BOARD_ORDER,
    AgentRun,
    LogEntry,
    Repo,
    RunStatus,
    ScopeSession,
    Task,
    User,
    utcnow,
)
from ..schemas import Board, BoardColumn, UpdateTaskRequest
from ..services import crypto, git_ops, jira, prompts
from ..services.auth import require_member


class RequestChangesBody(BaseModel):
    note: str | None = None

router = APIRouter(prefix="/tasks", tags=["board"])


def _scope_siblings(db: Session, task: Task) -> list[Task]:
    """Every ticket delivered together with `task` — the subtasks of the same
    scope session sharing its branch (they move as one unit: one branch, one PR,
    one merge). A standalone ticket (no session) is its own only sibling."""
    if not task.session_id:
        return [task]
    return list(db.exec(select(Task).where(Task.session_id == task.session_id,
                                           Task.branch == task.branch)).all())

_COLUMN_TITLES = {
    "backlog": "Backlog",
    "scoped": "Scoped",
    "approved": "Approved",
    "in_dev": "In Dev",
    "qa": "QA",
    "review": "Review",
    "pr": "PR",
    "done": "Done",
}

# Board lanes, left→right. "approved" is a virtual lane (not a TaskStatus): it
# holds human-approved tickets that haven't started yet.
_BOARD_LANES = ["backlog", "scoped", "approved", "in_dev", "qa", "review", "pr", "done"]


@router.get("", response_model=list[Task])
def list_tasks(
    repo_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_session),
) -> list[Task]:
    q = select(Task).order_by(Task.updated_at.desc())
    if repo_id is not None:
        q = q.where(Task.repo_id == repo_id)
    if status is not None:
        q = q.where(Task.status == status)
    return db.exec(q).all()


@router.get("/board", response_model=Board)
def get_board(repo_id: int | None = None, db: Session = Depends(get_session)) -> Board:
    q = select(Task)
    if repo_id is not None:
        q = q.where(Task.repo_id == repo_id)
    tasks = db.exec(q).all()

    # Pull human-approved-but-not-yet-started tickets into their own "Approved"
    # lane so the ready-to-run queue is visible apart from Scoped/Backlog.
    approved_pending = [t for t in tasks if t.status in ("backlog", "scoped") and t.approved]
    approved_ids = {t.id for t in approved_pending}

    by_status: dict[str, list[Task]] = {s: [] for s in BOARD_ORDER}
    for t in tasks:
        if t.id in approved_ids:
            continue  # shown in the Approved lane instead
        by_status.setdefault(t.status, []).append(t)
    by_status["approved"] = approved_pending

    columns = [
        BoardColumn(status=s, title=_COLUMN_TITLES.get(s, s.title()), tasks=by_status.get(s, []))
        for s in _BOARD_LANES
    ]
    return Board(columns=columns)


# PM-screen pipeline: the 7 board columns collapsed into 4 JIRA-style lanes.
_PIPELINE_MAP = [
    ("todo", "To Do", ["backlog", "scoped"]),
    ("in_progress", "In Progress", ["in_dev"]),
    ("review", "Review", ["qa", "review", "pr"]),
    ("done", "Done", ["done"]),
]


def _progress_for(db: Session, task: Task) -> int:
    """Rough % for an in-progress task, from its running dev run's log volume."""
    run = db.exec(
        select(AgentRun)
        .where(AgentRun.task_id == task.id, AgentRun.status == RunStatus.running.value)
        .order_by(AgentRun.created_at.desc())
    ).first()
    if run is None:
        return 50
    lines = len(db.exec(select(LogEntry).where(LogEntry.run_id == run.id)).all())
    return max(15, min(95, round(lines / 6 * 100)))


@router.get("/pipeline")
def get_pipeline(repo_id: int | None = None, db: Session = Depends(get_session)) -> dict:
    q = select(Task)
    if repo_id is not None:
        q = q.where(Task.repo_id == repo_id)
    tasks = db.exec(q).all()

    columns = []
    for lane_id, title, statuses in _PIPELINE_MAP:
        cards = []
        for t in tasks:
            if t.status in statuses:
                cards.append({
                    "id": t.id,
                    "key": t.key,
                    "title": t.title,
                    "agent": t.current_agent,
                    "approved": t.approved,
                    "progress": _progress_for(db, t) if lane_id == "in_progress" else None,
                })
        columns.append({"id": lane_id, "title": title, "count": len(cards), "cards": cards})
    return {"columns": columns}


@router.get("/{task_id}", response_model=Task)
def get_task(task_id: int, db: Session = Depends(get_session)) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return task


@router.patch("/{task_id}", response_model=Task, dependencies=[Depends(require_member)])
def update_task(task_id: int, body: UpdateTaskRequest, db: Session = Depends(get_session)) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(task, field, value)
    task.updated_at = utcnow()
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.post("/{task_id}/approve", response_model=Task, dependencies=[Depends(require_member)])
def approve_task(task_id: int, db: Session = Depends(get_session)) -> Task:
    """PM approval gate: a task must be approved before any agent pipeline can run."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    task.approved = True
    task.updated_at = utcnow()

    # On approval, push the ticket to the Jira board (once). No-op if Jira isn't
    # configured; never blocks approval if the Jira call fails.
    if jira.is_configured() and not task.jira_key:
        story = jira.push_story(task.key, task.title, task.description, task.acceptance_criteria)
        if story:
            task.jira_key = story.get("key")
            task.jira_url = story.get("url")

    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# NOTE: There is deliberately no per-ticket run endpoint. A scope is the unit of
# work — every approved subtask is delivered together on one branch as one PR.
# Run a scope via POST /sessions/{id}/run-scope.


# --- QA / PR review screen --------------------------------------------------
def _parse_diff(diff_text: str, max_lines: int = 400) -> list[dict]:
    """Turn a unified git diff into renderable lines with new-side line numbers.
    Each changed file contributes a ``type: "file"`` header row so multi-file
    diffs render grouped per file instead of as one anonymous stream."""
    lines: list[dict] = []
    newno = 0
    truncated = False
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git"):
            m = re.search(r" b/(.+)$", raw)
            lines.append({"n": "", "text": m.group(1) if m else raw, "type": "file"})
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            newno = int(m.group(1)) if m else newno
            lines.append({"n": "", "text": raw, "type": "hunk"})
        elif raw.startswith(("+++", "---", "index ", "new file", "deleted file", "rename ")):
            continue
        elif raw.startswith("+"):
            lines.append({"n": newno, "text": raw[1:], "type": "add"})
            newno += 1
        elif raw.startswith("-"):
            lines.append({"n": "", "text": raw[1:], "type": "del"})
        else:
            lines.append({"n": newno, "text": raw[1:] if raw.startswith(" ") else raw, "type": "ctx"})
            newno += 1
        if len(lines) >= max_lines:
            truncated = True
            break
    if truncated:
        lines.append({"n": "", "text": "… diff truncated — see the branch for the full change", "type": "hunk"})
    return lines


@router.get("/{task_id}/review")
def get_review(task_id: int, db: Session = Depends(get_session)) -> dict:
    """Real final-review payload: actual git diff + actual QA/Review agent output."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    repo = db.get(Repo, task.repo_id)

    diff_text, ins, dels = "", 0, 0
    if repo is not None and task.branch:
        path = str(git_ops.workdir(repo.git_url))
        try:
            diff_text = git_ops.diff(path, ref=task.branch)
            ins, dels = git_ops.diff_stat(path, ref=task.branch)
        except Exception:  # noqa: BLE001
            diff_text = ""

    qa = task.qa_summary or ""
    review = task.review_summary or ""
    cov_match = re.search(r"(\d{1,3})\s*%", qa)
    coverage = min(100, int(cov_match.group(1))) if cov_match else None
    pr_number = task.pr_url.rstrip("/").split("/")[-1] if task.pr_url else "—"

    checks = [
        {"label": "Dev changes committed", "ok": bool(diff_text.strip())},
        {"label": "QA reviewed", "ok": bool(qa)},
        {"label": "Code review complete", "ok": bool(review)},
        {"label": "Pull request opened", "ok": bool(task.pr_url)},
    ]

    observations = []
    if review:
        changes = "CHANGES REQUESTED" in review.upper() or "FAIL" in qa.upper()
        observations.append({
            "level": "warn" if changes else "info",
            "title": "Review verdict" if changes else "Reviewer notes",
            "text": (review[:400] + ("…" if len(review) > 400 else "")),
        })

    qa_notes = [p.strip() for p in re.split(r"\n{1,}", qa) if p.strip()] or ["QA agent has not run for this task yet."]

    return {
        "task": {"id": task.id, "key": task.key, "title": task.title, "status": task.status},
        "pr": {
            "number": pr_number,
            "title": task.title,
            "status": "MERGED" if task.status == "done" else ("OPEN" if task.pr_url else "DRAFT"),
            "insertions": ins,
            "deletions": dels,
            "diff": _parse_diff(diff_text) or [{"n": "", "text": "No diff yet — run the pipeline on this task.", "type": "ctx"}],
        },
        "checks": checks,
        "observations": observations,
        "qa_notes": qa_notes,
        "coverage": coverage,
        "ready_for_deployment": task.status in ("pr", "done") and "FAIL" not in qa.upper(),
    }


@router.post("/{task_id}/create-pr")
def create_pr(task_id: int, user: User = Depends(require_member),
              db: Session = Depends(get_session)) -> dict:
    """Human-triggered PR creation from the board: push the task's delivered
    branch and open the PR. If the current user has connected their own GitHub
    account, the PR is opened AS them (their token) — and if they don't have
    push access to the origin repo, this auto-forks it to their account first
    and opens a cross-repo PR, exactly like a normal open-source contribution.
    With no connected account it falls back to the shared bot token, then the
    host's gh login. Commits stay agent-authored either way. Covers the
    open_real_pr=off flow (deliver to a branch, a person decides per-delivery)."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task.pr_url:
        return {"url": task.pr_url, "created": False}
    if settings.demo_mode:
        raise HTTPException(403, "Demo mode is on — the platform won't push to real repos. "
                                 "Turn it off in Settings → Delivery & safety first.")
    if task.status not in ("pr", "done"):
        raise HTTPException(409, "This ticket hasn't been delivered yet — run the pipeline first.")
    if not task.branch:
        raise HTTPException(409, "No delivery branch is recorded on this ticket.")
    repo = db.get(Repo, task.repo_id)
    if repo is None:
        raise HTTPException(404, "Repo not found")

    path = str(git_ops.workdir(repo.git_url))
    diff = git_ops.diff(path, ref=task.branch)
    if not diff.strip():
        raise HTTPException(409, "The delivery branch has no changes to open a PR for.")

    # Scope deliveries share one branch → one PR covering all sibling subtasks.
    siblings = _scope_siblings(db, task)
    session = db.get(ScopeSession, task.session_id) if task.session_id else None
    title = f"Scope: {(session.title if session else task.title)[:60]}"
    if len(siblings) > 1:
        subs = [{"key": t.key, "title": t.title} for t in siblings]
        body = prompts.scope_pr_body(session.title if session else task.title, subs,
                                     task.qa_summary or "")
    else:
        body = prompts.pr_body(task.key, task.title, task.qa_summary or "")

    # Prefer the acting user's own connected GitHub account, so the PR is theirs.
    user_token = crypto.decrypt(user.github_token) if user.github_token else None
    author = user.github_login or settings.agent_git_name

    fork_target: tuple[str, str] | None = None
    pr_repo: str | None = None
    head_owner: str | None = None
    if user_token:
        origin = git_ops.repo_owner_name(repo.git_url)
        if origin and git_ops.github_can_push(*origin, user_token) is False:
            # No write access to origin under this account — fork it (or reuse
            # an existing fork) and open a cross-repo PR, same as any GitHub
            # contributor without collaborator access would.
            try:
                fork = git_ops.fork_repo(*origin, user_token)
            except RuntimeError as e:
                raise HTTPException(502, f"Fork failed: {str(e)[:300]}")
            fork_target = (fork["owner"], fork["name"])
            pr_repo = f"{origin[0]}/{origin[1]}"
            head_owner = fork["owner"]

    def _deliver() -> str:
        git_ops.push(path, task.branch, token=user_token, target=fork_target)
        return git_ops.gh_pr_create(path, title, body, token=user_token,
                                    repo=pr_repo, head_owner=head_owner, branch=task.branch)

    try:
        url = _deliver()
    except Exception as e:
        # github_can_push() can be undeterminable (repo lookup hiccup) and let
        # a doomed direct push through — if it's a permission error and we
        # haven't already tried a fork, fork now and retry once.
        msg = str(e)
        if fork_target is None and user_token and "denied" in msg.lower():
            origin = git_ops.repo_owner_name(repo.git_url)
            try:
                fork = git_ops.fork_repo(*origin, user_token) if origin else None
            except RuntimeError:
                fork = None
            if fork:
                fork_target = (fork["owner"], fork["name"])
                pr_repo, head_owner = f"{origin[0]}/{origin[1]}", fork["owner"]
                try:
                    url = _deliver()
                except Exception as e2:  # noqa: BLE001
                    raise HTTPException(502, f"Push/PR failed: {str(e2)[:300]}")
            else:
                raise HTTPException(502, f"Push/PR failed: {msg[:300]}")
        else:
            raise HTTPException(502, f"Push/PR failed: {msg[:300]}")
    if not url.startswith("http"):
        raise HTTPException(502, f"gh did not return a PR URL: {url[:300]}")

    now = utcnow()
    for t in siblings:
        t.pr_url = url
        t.updated_at = now
        db.add(t)
    db.commit()
    return {"url": url, "created": True, "tasks": [t.key for t in siblings], "author": author,
            "forked": fork_target is not None,
            "fork_repo": f"{fork_target[0]}/{fork_target[1]}" if fork_target else None}


@router.post("/{task_id}/merge", dependencies=[Depends(require_member)])
def merge_pr(task_id: int, db: Session = Depends(get_session)) -> dict:
    """Mark the delivery done. A scope ships as one unit, so this marks every
    sibling subtask done together — they share the same branch and PR."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    siblings = _scope_siblings(db, task)
    now = utcnow()
    for t in siblings:
        t.status = "done"
        t.current_agent = None
        t.updated_at = now
        db.add(t)
    db.commit()
    return {"ok": True, "tasks": [t.key for t in siblings]}


@router.post("/{task_id}/request-changes", dependencies=[Depends(require_member)])
def request_changes(task_id: int, body: RequestChangesBody | None = None,
                    db: Session = Depends(get_session)) -> dict:
    """Send the scope back for changes: the note is recorded on the ticket it was
    raised against, and the whole scope returns to the (approved) Scoped lane so
    Run scope re-runs it as one unit. Scopes deliver together, so they re-work
    together — re-running just one subtask would desync the shared branch."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    siblings = _scope_siblings(db, task)
    if any(t.status == "done" for t in siblings):
        raise HTTPException(409, "This scope is already merged/done — reopen isn't allowed")
    if any(t.status in ("in_dev", "qa", "review") for t in siblings):
        raise HTTPException(409, "Pipeline is running — wait for it to finish, then request changes")
    note = (body.note if body else None) or ""
    if note.strip():
        task.description = f"{task.description or ''}\n\n[Change request] {note.strip()}".strip()
    now = utcnow()
    for t in siblings:
        t.status = "scoped"        # back to the drafts lane…
        t.approved = True          # …but still approved, so Run scope picks it up
        t.current_agent = "pm"
        t.pr_url = None            # the prior PR no longer reflects the pending re-run
        t.updated_at = now
        db.add(t)
    db.commit()
    return {"ok": True, "tasks": [t.key for t in siblings]}


@router.get("/{task_id}/detail")
def task_detail(task_id: int, db: Session = Depends(get_session)) -> dict:
    """Full ticket detail for the drawer: the ticket, its scope, and its runs."""
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    session = db.get(ScopeSession, task.session_id) if task.session_id else None
    runs = db.exec(
        select(AgentRun).where(AgentRun.task_id == task_id).order_by(AgentRun.created_at)
    ).all()
    siblings = _scope_siblings(db, task)
    return {
        "task": {
            "id": task.id, "key": task.key, "title": task.title, "description": task.description,
            "acceptance_criteria": task.acceptance_criteria, "status": task.status,
            "priority": task.priority, "approved": task.approved, "current_agent": task.current_agent,
            "branch": task.branch, "pr_url": task.pr_url, "token_cost": task.token_cost,
            "session_id": task.session_id,
        },
        "scope": ({"title": session.title, "summary": session.requirement_summary,
                   "acceptance_criteria": session.acceptance_criteria,
                   "sibling_count": len(siblings)} if session else None),
        "runs": [{"id": r.id, "agent_type": r.agent_type, "status": r.status,
                  "tokens": r.tokens_input + r.tokens_output, "duration_ms": r.duration_ms,
                  "model": r.model} for r in runs],
    }

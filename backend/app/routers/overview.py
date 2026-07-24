"""Cross-cutting overview: top-bar metrics, active agent, and the right-rail
Agent Lifecycle state — all derived from live board + run data."""

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..database import get_session
from ..models import AgentRun, KBStatus, Repo, RunStatus, ScopeSession, Task

router = APIRouter(prefix="/overview", tags=["overview"])

# Lifecycle stages in the right rail, top to bottom.
_STAGES = [
    ("git", "Git Setup"),
    ("preprocess", "Pre-processing"),
    ("pm", "PM Agent"),
    ("story", "Story Creation"),
    ("dev", "Dev Agent"),
    ("qa", "QA Agent"),
    ("pr", "PR Review"),
]

_AGENT_LABEL = {"pm": "PM Agent", "dev": "Dev Agent", "qa": "QA Agent", "review": "Review Agent", "pr": "PR Agent"}


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


@router.get("")
def overview(repo_id: int | None = None, session_id: int | None = None,
             db: Session = Depends(get_session)) -> dict:
    runs = db.exec(select(AgentRun)).all()
    tasks_q = select(Task)
    # session_id (a single scope) takes precedence over repo_id, so the header
    # can show the currently-selected scope's tokens/cost and reset on switch.
    if session_id is not None:
        tasks_q = tasks_q.where(Task.session_id == session_id)
        task_ids = set(db.exec(select(Task.id).where(Task.session_id == session_id)).all())
        runs = [r for r in runs if r.task_id in task_ids]
    elif repo_id is not None:
        tasks_q = tasks_q.where(Task.repo_id == repo_id)
        task_ids = set(db.exec(select(Task.id).where(Task.repo_id == repo_id)).all())
        runs = [r for r in runs if r.task_id in task_ids]
    tasks = db.exec(tasks_q).all()
    repos = db.exec(select(Repo)).all()

    total_in = sum(r.tokens_input for r in runs)
    total_out = sum(r.tokens_output for r in runs)
    run_cost = sum(r.cost_usd for r in runs)

    # PM scoping cost lives on ScopeSession (it predates any AgentRun). Fold it
    # in so the header reflects PM cost too, matching the current scope filter.
    sess_q = select(ScopeSession)
    if session_id is not None:
        sess_q = sess_q.where(ScopeSession.id == session_id)
    elif repo_id is not None:
        sess_q = sess_q.where(ScopeSession.repo_id == repo_id)
    pm_cost = 0.0
    for s in db.exec(sess_q).all():
        total_in += s.pm_tokens_input or 0
        total_out += s.pm_tokens_output or 0
        pm_cost += s.pm_cost_usd or 0.0

    total_tokens = total_in + total_out
    total_cost = round(run_cost + pm_cost, 2)

    running = [r for r in runs if r.status == RunStatus.running.value]
    active_run = running[0] if running else None
    active_agent = active_run.agent_type if active_run else "dev"

    # Board counts for lifecycle inference
    c = dict.fromkeys(("backlog", "scoped", "in_dev", "qa", "review", "pr", "done"), 0)
    for t in tasks:
        c[t.status] = c.get(t.status, 0) + 1
    running_agents = {r.agent_type for r in running}
    any_ready = any(r.kb_status == KBStatus.ready.value for r in repos)
    any_indexing = any(r.kb_status == KBStatus.indexing.value for r in repos)
    has_sessions = db.exec(select(ScopeSession)).first() is not None
    tasks_exist = len(tasks) > 0
    past_dev = c["qa"] + c["review"] + c["pr"] + c["done"]

    def state(active: bool, done: bool) -> str:
        return "active" if active else ("done" if done else "pending")

    stage_state = {
        "git": state(False, len(repos) > 0),
        "preprocess": state(any_indexing, any_ready),
        "pm": state(has_sessions and not tasks_exist, tasks_exist),
        "story": state(False, tasks_exist),
        "dev": state("dev" in running_agents or c["in_dev"] > 0, past_dev > 0),
        "qa": state("qa" in running_agents or c["qa"] > 0, c["review"] + c["pr"] + c["done"] > 0),
        "pr": state("pr" in running_agents or c["review"] + c["pr"] > 0, c["done"] > 0),
    }
    lifecycle = [{"key": k, "label": lbl, "state": stage_state[k]} for k, lbl in _STAGES]

    return {
        "active_agent": active_agent,
        "active_agent_label": _AGENT_LABEL.get(active_agent, "Agent"),
        "metrics": {
            "tokens": total_tokens,
            "tokens_label": _fmt_tokens(total_tokens),
            "tokens_in": total_in,
            "tokens_out": total_out,
            "tokens_in_label": _fmt_tokens(total_in),
            "tokens_out_label": _fmt_tokens(total_out),
            "cost_usd": total_cost,
            # Honest header counts (no more fake OPS/uptime).
            "runs": len(runs),
            "active": len(running),
            "done": c["done"],
            "run_cost": round(active_run.cost_usd, 2) if active_run else 0.0,
        },
        "lifecycle": lifecycle,
    }

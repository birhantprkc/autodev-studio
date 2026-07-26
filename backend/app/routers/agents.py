"""Agent runs, streamed logs, and roll-up observability stats."""

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import AgentRun, LogEntry, Repo, RunStatus, Task, TaskStatus, utcnow
from ..schemas import AgentStats, RunDetail
from ..services import git_ops

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/runs", response_model=list[AgentRun])
def list_runs(
    repo_id: int | None = None,
    agent_type: str | None = None,
    task_id: int | None = None,
    db: Session = Depends(get_session),
) -> list[AgentRun]:
    q = select(AgentRun).order_by(AgentRun.created_at.desc())
    if agent_type is not None:
        q = q.where(AgentRun.agent_type == agent_type)
    if task_id is not None:
        q = q.where(AgentRun.task_id == task_id)
    if repo_id is not None:
        task_ids = db.exec(select(Task.id).where(Task.repo_id == repo_id)).all()
        q = q.where(AgentRun.task_id.in_(task_ids or [-1]))
    return db.exec(q).all()


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, db: Session = Depends(get_session)) -> RunDetail:
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    logs = db.exec(
        select(LogEntry).where(LogEntry.run_id == run_id).order_by(LogEntry.ts)
    ).all()
    return RunDetail(
        run=run,
        logs=[
            {"ts": log.ts.isoformat(), "severity": log.severity, "message": log.message}
            for log in logs
        ],
    )


# Pipeline stages shown as the Dev screen's "Execution Plan".
_STAGE_TITLES = [
    ("dev", "Dev — implement change", "Claude edits the working copy to satisfy the acceptance criteria."),
    ("qa", "QA — test & review (OpenAI)", "A different-provider model runs tests and reviews for bias."),
    ("review", "Review — against requirements", "Claude reviews the diff versus the acceptance criteria."),
    ("pr", "PR — open pull request", "Push the branch and open a PR via the gh CLI."),
]


@router.get("/runs/{run_id}/dev")
def get_dev_detail(run_id: int, db: Session = Depends(get_session)) -> dict:
    """Real live-implementation detail for the Dev Agent screen, from actual runs."""
    run = db.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    task = db.get(Task, run.task_id)

    # Real streamed agent output as the console body.
    logs = db.exec(select(LogEntry).where(LogEntry.run_id == run_id).order_by(LogEntry.ts)).all()
    code_stream = "\n".join(f"{l.severity:>7} · {l.message}" for l in logs) or "Waiting for agent output…"

    if run.status == RunStatus.completed.value or run.status == RunStatus.failed.value:
        percent = 100
    else:
        percent = max(10, min(95, len(logs) * 15))

    # Execution plan = the task's real pipeline stages, state from each stage's latest run.
    stage_runs = {}
    if task is not None:
        for r in db.exec(select(AgentRun).where(AgentRun.task_id == task.id).order_by(AgentRun.created_at)).all():
            stage_runs[r.agent_type] = r.status
    plan = []
    for i, (stage, title, desc) in enumerate(_STAGE_TITLES):
        status = stage_runs.get(stage)
        state = {"completed": "done", "running": "active", "failed": "active", None: "pending"}.get(status, "pending")
        plan.append({"n": i + 1, "title": title, "desc": desc, "state": state})

    # Real change size from the working copy.
    ins = dels = 0
    if task is not None and task.branch:
        repo = db.get(Repo, task.repo_id)
        if repo is not None:
            try:
                ins, dels = git_ops.diff_stat(str(git_ops.workdir(repo.git_url)), ref=task.branch)
            except Exception:  # noqa: BLE001
                ins = dels = 0

    dur = f"{run.duration_ms / 1000:.1f}s" if run.duration_ms else ("running" if run.status == "running" else "—")
    done_stages = sum(1 for s in stage_runs.values() if s == "completed")
    unknown = bool(getattr(run, "usage_unknown", False))
    metrics = [
        {"label": "Tokens",
         "value": "unknown" if unknown and not (run.tokens_input + run.tokens_output)
         else f"{run.tokens_input + run.tokens_output:,}", "sub": "in+out"},
        {"label": "Cost",
         "value": "unknown" if unknown and not run.cost_usd else f"${run.cost_usd:.2f}",
         "sub": run.agent_type},
        {"label": "Duration", "value": dur, "sub": run.status},
        {"label": "Lines Changed", "value": f"+{ins} −{dels}", "sub": "vs base"},
    ]

    return {
        "run_id": run_id,
        "task": {"key": task.key if task else "—", "title": task.title if task else "Task"},
        "title": f"{run.model} · {task.title if task else 'task'}",
        "subtitle": f"{run.agent_type} agent · {run.status}",
        "percent": percent,
        "phase_label": f"STAGE {min(4, done_stages + 1)}/4",
        "plan": plan,
        "code_stream": code_stream,
        "run_cost": round(run.cost_usd, 2),
        "metrics": metrics,
    }


@router.get("/stats", response_model=AgentStats)
def get_stats(repo_id: int | None = None, db: Session = Depends(get_session)) -> AgentStats:
    runs = db.exec(select(AgentRun)).all()
    if repo_id is not None:
        task_ids = set(db.exec(select(Task.id).where(Task.repo_id == repo_id)).all())
        runs = [r for r in runs if r.task_id in task_ids]

    total_tokens = sum(r.tokens_input + r.tokens_output for r in runs)
    total_cost = round(sum(r.cost_usd for r in runs), 2)
    active = sum(1 for r in runs if r.status == RunStatus.running.value)

    today = utcnow().astimezone(UTC).date()
    done = db.exec(select(Task).where(Task.status == TaskStatus.done.value)).all()
    if repo_id is not None:
        done = [t for t in done if t.repo_id == repo_id]
    completed_today = sum(
        1 for t in done if t.updated_at and t.updated_at.date() == today
    )

    return AgentStats(
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
        active_agents=active,
        tasks_completed_today=completed_today,
    )

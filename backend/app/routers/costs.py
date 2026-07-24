"""Cost + token breakdown: totals → per scope → per ticket → per agent."""

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..database import get_session
from ..models import AgentRun, ScopeSession, Task

router = APIRouter(prefix="/costs", tags=["costs"])

_AGENTS = ["pm", "dev", "qa", "review", "pr"]


@router.get("/data")
def costs(repo_id: int | None = None, db: Session = Depends(get_session)) -> dict:
    tq = select(Task)
    if repo_id is not None:
        tq = tq.where(Task.repo_id == repo_id)
    tasks = db.exec(tq).all()
    task_ids = {t.id for t in tasks}

    # Aggregate runs per task and per (task, agent), tracking in/out separately.
    def _blank():
        return {"cost": 0.0, "tokens_in": 0, "tokens_out": 0, "runs": 0}

    per_task: dict[int, dict] = {}
    for r in db.exec(select(AgentRun)).all():
        if r.task_id not in task_ids:
            continue
        tin, tout = r.tokens_input or 0, r.tokens_output or 0
        pt = per_task.setdefault(r.task_id, {"cost": 0.0, "tokens_in": 0, "tokens_out": 0, "by_agent": {}})
        pt["cost"] += r.cost_usd or 0.0
        pt["tokens_in"] += tin
        pt["tokens_out"] += tout
        ba = pt["by_agent"].setdefault(r.agent_type, _blank())
        ba["cost"] += r.cost_usd or 0.0
        ba["tokens_in"] += tin
        ba["tokens_out"] += tout
        ba["runs"] += 1

    sess_by_id = {s.id: s for s in db.exec(select(ScopeSession)).all()}
    scopes: dict = {}
    unscoped = {"session_id": None, "title": "No scope", "cost": 0.0, "tokens_in": 0, "tokens_out": 0, "tickets": []}

    def _fmt_agent(v):
        return {"cost": round(v["cost"], 4), "tokens_in": v["tokens_in"], "tokens_out": v["tokens_out"],
                "tokens": v["tokens_in"] + v["tokens_out"]}

    for t in tasks:
        pt = per_task.get(t.id, {"cost": 0.0, "tokens_in": 0, "tokens_out": 0, "by_agent": {}})
        ticket = {
            "id": t.id, "key": t.key, "title": t.title, "status": t.status,
            "cost": round(pt["cost"], 4), "tokens_in": pt["tokens_in"], "tokens_out": pt["tokens_out"],
            "tokens": pt["tokens_in"] + pt["tokens_out"],
            "by_agent": {a: _fmt_agent(pt["by_agent"][a]) for a in _AGENTS if a in pt["by_agent"]},
        }
        if t.session_id and t.session_id in sess_by_id:
            s = sess_by_id[t.session_id]
            bucket = scopes.setdefault(t.session_id, {"session_id": s.id, "title": s.title or f"Scope #{s.id}",
                                                      "cost": 0.0, "tokens_in": 0, "tokens_out": 0, "tickets": []})
        else:
            bucket = unscoped
        bucket["tickets"].append(ticket)
        bucket["cost"] += pt["cost"]
        bucket["tokens_in"] += pt["tokens_in"]
        bucket["tokens_out"] += pt["tokens_out"]

    scope_list = sorted(scopes.values(), key=lambda s: s["cost"], reverse=True)
    if unscoped["tickets"]:
        scope_list.append(unscoped)
    for s in scope_list:
        s["cost"] = round(s["cost"], 4)
        s["tokens"] = s["tokens_in"] + s["tokens_out"]

    return {
        "totals": {"cost": round(sum(s["cost"] for s in scope_list), 4),
                   "tokens_in": sum(s["tokens_in"] for s in scope_list),
                   "tokens_out": sum(s["tokens_out"] for s in scope_list),
                   "tokens": sum(s["tokens"] for s in scope_list),
                   "tickets": sum(len(s["tickets"]) for s in scope_list)},
        "agents": _AGENTS,
        "scopes": scope_list,
    }

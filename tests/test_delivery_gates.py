"""Delivery-state safety gates.

These tests cover the boundaries that must remain true even when the model
responses and pipeline are otherwise mocked: an unverified or incomplete
delivery cannot become merged, and an interrupted run remains visible as blocked.
"""

import pytest
from app.core import tasks
from app.core.errors import CoreError
from app.models import Repo, ScopeSession, Task, TaskStatus
from app.services import orchestrator
from sqlmodel import Session


def _task(db: Session, *, status: str = TaskStatus.pr.value,
          qa: str = "VERDICT: PASS", review: str = "VERDICT: APPROVED",
          findings: dict | None = None) -> Task:
    repo = Repo(name="repo", org="org", git_url="https://example.com/org/repo.git")
    db.add(repo)
    db.commit()
    task = Task(key="T-1", repo_id=repo.id, title="change", status=status,
                branch="agent/scope-1", qa_summary=qa, review_summary=review,
                review_findings=findings or {})
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_merge_requires_pr_lane_and_explicit_approval(db, monkeypatch):
    monkeypatch.setattr(tasks.settings, "demo_mode", True)
    task = _task(db, status=TaskStatus.scoped.value)
    with pytest.raises(CoreError):
        tasks.merge(db, task.id)

    task.status = TaskStatus.pr.value
    task.review_summary = "VERDICT: INCONCLUSIVE — reviewer unavailable"
    db.add(task)
    db.commit()
    with pytest.raises(CoreError):
        tasks.merge(db, task.id)


def test_merge_allows_an_explicitly_approved_demo_delivery(db, monkeypatch):
    monkeypatch.setattr(tasks.settings, "demo_mode", True)
    task = _task(db)
    out = tasks.merge(db, task.id)
    assert out["ok"] is True
    db.refresh(task)
    assert task.status == TaskStatus.done.value


def test_merge_checks_every_sibling_gate(db, monkeypatch):
    monkeypatch.setattr(tasks.settings, "demo_mode", True)
    task = _task(db)
    sibling = Task(key="T-2", repo_id=task.repo_id, session_id=None, title="other",
                   status=TaskStatus.pr.value, branch=task.branch,
                   qa_summary="VERDICT: INCONCLUSIVE", review_summary="VERDICT: APPROVED")
    # A shared branch makes this a delivery sibling even without a session only
    # when the session relationship exists; create it explicitly for the test.
    session = ScopeSession(repo_id=task.repo_id, title="scope", status="scoped")
    db.add(session)
    db.commit()
    task.session_id = session.id
    sibling.session_id = session.id
    db.add(task)
    db.add(sibling)
    db.commit()

    with pytest.raises(CoreError):
        tasks.merge(db, task.id)


def test_generic_task_update_cannot_skip_delivery_gates(db):
    task = _task(db, status=TaskStatus.scoped.value)

    with pytest.raises(CoreError):
        tasks.update(db, task.id, {"status": TaskStatus.done.value})

    db.refresh(task)
    assert task.status == TaskStatus.scoped.value


def test_interrupted_scope_is_persisted_as_blocked(db):
    repo = Repo(name="repo", org="org", git_url="https://example.com/org/repo.git")
    db.add(repo)
    db.commit()
    session = ScopeSession(repo_id=repo.id, title="scope", status="scoped")
    db.add(session)
    db.commit()
    task = Task(key="T-1", repo_id=repo.id, session_id=session.id, title="change",
                status=TaskStatus.in_dev.value, approved=True, current_agent="dev")
    db.add(task)
    db.commit()

    orchestrator._mark_scope_blocked(session.id, "test interruption")
    db.refresh(task)
    db.refresh(session)
    assert task.status == TaskStatus.blocked.value
    assert task.current_agent is None
    assert "test interruption" in task.review_summary
    assert session.status == "failed"

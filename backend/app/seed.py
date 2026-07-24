"""Idempotent demo seed — gives every screen realistic data on first boot.

Themed around a sample "user-analytics dashboard" feature so the integrated
screens look coherent immediately. Off by default (SEED_ON_STARTUP).
"""

from datetime import timedelta

from sqlmodel import Session, select

from .database import engine
from .models import (
    AgentRun,
    ChatMessage,
    KBStatus,
    LogEntry,
    Repo,
    RunStatus,
    ScopeSession,
    Task,
    TaskStatus,
    utcnow,
)


def seed_demo_data() -> None:
    with Session(engine) as db:
        if db.exec(select(Repo)).first() is not None:
            return  # already seeded
        now = utcnow()

        # --- Repositories (various KB states) ------------------------------
        alpha = Repo(
            name="alpha",
            org="org",
            git_url="https://github.com/org/alpha",
            key_prefix="AL",
            primary_language="TypeScript",
            languages=["TypeScript", "Go", "Python"],
            kb_status=KBStatus.ready.value,
            kb_progress=100,
            kb_step="Knowledge base ready — 42 modules",
            kb_doc_count=42,
            last_indexed_at=now - timedelta(hours=2),
        )
        globex = Repo(
            name="web-dashboard",
            org="globex",
            git_url="https://github.com/globex/web-dashboard",
            key_prefix="WD",
            primary_language="TypeScript",
            languages=["TypeScript", "CSS"],
            kb_status=KBStatus.indexing.value,
            kb_progress=62,
            kb_step="Embedding files — 3,204 / 5,110",
            kb_doc_count=0,
        )
        initech = Repo(
            name="legacy-erp",
            org="initech",
            git_url="https://github.com/initech/legacy-erp",
            key_prefix="LE",
            primary_language="Java",
            languages=["Java"],
            kb_status=KBStatus.failed.value,
            kb_progress=41,
            kb_error="Clone failed: repository access denied (check credentials)",
        )
        db.add_all([alpha, globex, initech])
        db.commit()
        db.refresh(alpha)

        # --- Scope session on the demo repo --------------------------------
        session = ScopeSession(
            repo_id=alpha.id,
            title="User analytics dashboard",
            status="scoped",
            requirement_summary=(
                "Build a user-analytics dashboard: a Go ingestion service polling "
                "events from Kafka, a GraphQL API exposing aggregated user data, and "
                "React components for visual data representation."
            ),
            acceptance_criteria=[
                "Ingestion service polls Kafka and persists events",
                "GraphQL API exposes aggregated metrics endpoints",
                "Dashboard renders charts from the analytics API",
                "P95 API latency under 150ms",
                "Unit + integration tests across the stack",
            ],
            affected_files=[
                "services/ingest/main.go",
                "api/graphql/schema.py",
                "web/src/components/AnalyticsGrid.tsx",
            ],
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        db.add_all([
            ChatMessage(
                session_id=session.id,
                role="user",
                content="We need a new dashboard for user analytics.",
            ),
            ChatMessage(
                session_id=session.id,
                role="agent",
                agent_type="pm",
                content=(
                    "Understood. Based on the requirements for the analytics "
                    "initiative, I've drafted 3 stories across the stack. Pushing "
                    "these to JIRA for staging..."
                ),
                sources=["services/ingest/main.go", "api/graphql/schema.py"],
                tokens=1420,
            ),
        ])
        db.commit()

        # --- Board: the three analytics stories + history ------------------
        def task(key, title, desc, status, priority, agent, **kw):
            return Task(
                key=key,
                repo_id=alpha.id,
                session_id=session.id,
                title=title,
                description=desc,
                status=status,
                priority=priority,
                current_agent=agent,
                **kw,
            )

        al101 = task(
            "AL-101", "Data Ingestion Service",
            "Develop a Go-based service to poll events from Kafka.",
            TaskStatus.backlog.value, "high", "pm",
            acceptance_criteria=["Poll Kafka topic", "Persist events", "At-least-once delivery"],
        )
        al102 = task(
            "AL-102", "Analytics API",
            "Expose GraphQL endpoints for aggregated user data.",
            TaskStatus.in_dev.value, "high", "dev", token_cost=24_400,
            acceptance_criteria=["GraphQL schema for metrics", "Auth-scoped queries", "P95 < 150ms"],
        )
        al103 = task(
            "AL-103", "Frontend Dashboard",
            "React components for visual data representation.",
            TaskStatus.pr.value, "medium", "pr", token_cost=31_800,
            pr_url="https://github.com/org/alpha/pull/452",
            acceptance_criteria=["AnalyticsGrid component", "Charts from API", "Responsive layout"],
        )
        # Completed history (fills the Done column + today's stats)
        done = [
            task("AL-098", "Auth token rotation", "Rotate JWT signing keys.", TaskStatus.done.value, "medium", None, token_cost=28_900),
            task("AL-097", "Kafka consumer group config", "Tune consumer offsets.", TaskStatus.done.value, "low", None, token_cost=15_100),
            task("AL-096", "CI pipeline for web build", "Add GitHub Actions.", TaskStatus.done.value, "low", None, token_cost=12_700),
        ]
        for t in done:
            t.updated_at = now  # counts toward "completed today"

        db.add_all([al101, al102, al103, *done])
        db.commit()
        for t in (al102, al103, *done):
            db.refresh(t)

        # --- Agent runs + logs ---------------------------------------------
        def run(task_id, agent, status, tin, tout, cost, *, mins_ago, dur_ms=None):
            started = now - timedelta(minutes=mins_ago)
            return AgentRun(
                task_id=task_id,
                agent_type=agent,
                status=status,
                tokens_input=tin,
                tokens_output=tout,
                cost_usd=cost,
                started_at=started,
                finished_at=None if status == RunStatus.running.value else started + timedelta(milliseconds=dur_ms or 0),
                duration_ms=None if status == RunStatus.running.value else dur_ms,
            )

        # AL-102 Analytics API — Dev agent live (running)
        dev_run = run(al102.id, "dev", RunStatus.running.value, 18_000, 6_400, 1.05, mins_ago=2)
        # AL-103 Frontend Dashboard — QA + Review + PR (completed)
        qa_run = run(al103.id, "qa", RunStatus.completed.value, 9_200, 2_100, 0.10, mins_ago=18, dur_ms=6_200)
        rev_run = run(al103.id, "review", RunStatus.completed.value, 12_500, 3_300, 0.14, mins_ago=12, dur_ms=8_700)
        pr_run = run(al103.id, "pr", RunStatus.completed.value, 4_800, 900, 0.05, mins_ago=6, dur_ms=3_400)
        db.add_all([dev_run, qa_run, rev_run, pr_run])
        db.commit()
        for r in (dev_run, qa_run, rev_run, pr_run):
            db.refresh(r)

        # Completed runs across the done tasks (history + stats)
        for t in done:
            for agent, tin, tout, cost, mins in (
                ("dev", 17_500, 6_100, 0.21, 120),
                ("qa", 8_800, 2_000, 0.10, 110),
                ("review", 11_900, 3_100, 0.13, 100),
                ("pr", 4_600, 850, 0.05, 90),
            ):
                db.add(run(t.id, agent, RunStatus.completed.value, tin, tout, cost, mins_ago=mins, dur_ms=5_000))
        db.commit()

        # Streaming logs
        dev_lines = [
            ("info", "Loading task AL-102 (Analytics API) + acceptance criteria"),
            ("info", "Querying knowledge base for GraphQL modules"),
            ("info", "Scaffolding FastAPI + Strawberry GraphQL app"),
            ("info", "Writing tests/test_analytics.py"),
        ]
        for i, (sev, msg) in enumerate(dev_lines):
            db.add(LogEntry(run_id=dev_run.id, severity=sev, message=msg, ts=now - timedelta(seconds=(len(dev_lines) - i) * 8)))
        for sev, msg in [
            ("info", "Running pytest -q on branch feature/analytics-dashboard"),
            ("info", "tests/test_analytics.py ...."),
            ("success", "All integration tests passed"),
        ]:
            db.add(LogEntry(run_id=qa_run.id, severity=sev, message=msg))
        for sev, msg in [
            ("info", "Diffing changes against requirement summary"),
            ("warn", "Edge case: data.defaultView null during hot-reload may throw"),
            ("success", "Coverage 94.2% — ready for human approval"),
        ]:
            db.add(LogEntry(run_id=rev_run.id, severity=sev, message=msg))
        db.add(LogEntry(run_id=pr_run.id, severity="success", message="Opened PR #452: Add Analytics Dashboard"))
        db.commit()

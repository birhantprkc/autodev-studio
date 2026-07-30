"""Regression tests for the professionalization audit fixes:

  * Delivery-note retrieval — the in-process TF-IDF ranking over cross-run
    notes must actually retrieve and rank (no vector store required).
  * providers.can_chat — LLM gates follow the per-stage provider registry,
    not one hardcoded openai_api_key.
  * Ticket key allocation survives ticket deletion without key collisions.
  * Duplicate repo ingest is refused (two rows would share one on-disk slug).
  * No CORS origin echo — the API must not authorize cross-origin
    credentialed requests.
"""

from __future__ import annotations

import pytest
from app.config import settings
from app.models import Repo, Task
from app.services import providers
from app.services.knowledge import retriever, store
from app.services.knowledge.facts import KnowledgeDocument


# --- delivery-note retrieval (in-process TF-IDF, no vector store) -------------
@pytest.fixture
def kb_repo(tmp_path, monkeypatch):
    """A fake repo with three stored delivery notes in a temp knowledge dir."""
    monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
    url = "https://github.com/acme/widgets"
    for doc in [
        KnowledgeDocument(id="delivery_login", type="delivery_note", name="Delivered: login",
                          summary="Added password login sessions and hashing.",
                          content={"files": ["auth/session.py"]}),
        KnowledgeDocument(id="delivery_export", type="delivery_note", name="Delivered: CSV export",
                          summary="Exports spline reports as CSV files.",
                          content={"files": ["export/csv.py"]}),
        KnowledgeDocument(id="lessons_auth", type="lesson", name="Lessons: auth",
                          summary="Durable auth lessons.",
                          content={"lessons": ["sessions expire in 1h"]}),
    ]:
        store.save(url, doc)
    return url


class TestNoteRetrieval:
    def test_retrieves_and_ranks_notes(self, kb_repo):
        hits = retriever.notes(kb_repo, "csv export report")
        assert hits, "note ranking must retrieve, not silently return []"
        assert hits[0][0].id == "delivery_export"
        assert all(score > 0 for _doc, score in hits)

    def test_scope_context_surfaces_relevant_note(self, kb_repo):
        ctx = retriever.scope_context(kb_repo, "password login sessions")
        assert "login" in ctx.lower()

    def test_unrelated_query_returns_nothing(self, kb_repo):
        assert retriever.notes(kb_repo, "zzzq qqzz xyzzy") == []


# --- providers.can_chat -------------------------------------------------------
class TestCanChat:
    def test_openai_kind_needs_its_own_key(self, monkeypatch):
        monkeypatch.setattr(settings, "groq_api_key", "")
        assert not providers.can_chat("groq")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-x")
        assert providers.can_chat("groq")

    def test_does_not_depend_on_openai_key(self, monkeypatch):
        # A Groq-only install must count as chat-ready even with no OPENAI key.
        monkeypatch.setattr(settings, "openai_api_key", "")
        monkeypatch.setattr(settings, "groq_api_key", "gsk-x")
        assert providers.can_chat("groq")

    def test_cli_kind_follows_backend_availability(self, monkeypatch):
        from app.services import agent_backends

        monkeypatch.setattr(agent_backends, "is_available", lambda _b: True)
        assert providers.can_chat("claude-cli")
        monkeypatch.setattr(agent_backends, "is_available", lambda _b: False)
        assert not providers.can_chat("claude-cli")


# --- Ticket key allocation ----------------------------------------------------
class TestNextKey:
    def test_no_collision_after_deletion(self, db):
        from app.core.scoping import next_key as _next_key

        repo = Repo(name="w", org="acme", git_url="https://github.com/acme/w",
                    key_prefix="W")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        for n in (101, 102, 103):
            db.add(Task(key=f"W-{n}", repo_id=repo.id, title=f"t{n}"))
        db.commit()
        # Delete a middle ticket — count drops but the max survives.
        victim = db.exec(__import__("sqlmodel").select(Task).where(Task.key == "W-102")).first()
        db.delete(victim)
        db.commit()
        assert _next_key(db, repo) == "W-104"

    def test_first_key(self, db):
        from app.core.scoping import next_key as _next_key

        repo = Repo(name="w2", org="acme", git_url="https://github.com/acme/w2",
                    key_prefix="W2")
        db.add(repo)
        db.commit()
        db.refresh(repo)
        assert _next_key(db, repo) == "W2-101"


# --- Duplicate ingest + CORS --------------------------------------------------
class TestIngestGuard:
    def test_duplicate_url_is_409(self, admin_client, monkeypatch):
        from app.services import background

        monkeypatch.setattr(background, "submit", lambda *a, **k: None)
        r1 = admin_client.post("/repos/ingest",
                               json={"git_url": "https://github.com/acme/dup"})
        assert r1.status_code == 201
        r2 = admin_client.post("/repos/ingest",
                               json={"git_url": "https://github.com/acme/dup"})
        assert r2.status_code == 409


def test_no_cross_origin_credential_echo(client):
    """The API must never echo an arbitrary Origin back as allowed — that plus
    allow-credentials is what let any website make cookie-authed calls."""
    r = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

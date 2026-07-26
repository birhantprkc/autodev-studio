"""Pluggable embeddings (semantic | api | tfidf) and one-click CLI install."""

import pytest
from app.config import settings
from app.services import agent_backends, local_rag


@pytest.fixture(autouse=True)
def _restore_embedding_settings():
    saved = (settings.rag_embeddings, settings.embedding_model,
             settings.embedding_api_base_url, settings.embedding_api_key,
             settings.embedding_dim)
    yield
    (settings.rag_embeddings, settings.embedding_model,
     settings.embedding_api_base_url, settings.embedding_api_key,
     settings.embedding_dim) = saved


class _FakeResp:
    def __init__(self, vectors):
        self._v = vectors

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"index": i, "embedding": v} for i, v in enumerate(self._v)]}


# --- embeddings ---------------------------------------------------------------

def test_api_mode_needs_base_url():
    import importlib.util

    settings.rag_embeddings = "api"
    settings.embedding_api_base_url = ""
    assert local_rag.semantic_available() is False
    # With a base url, api-mode availability ALSO depends on qdrant being present
    # (it's the vector store). Assert it tracks that — which passes both locally
    # (qdrant installed) and in CI (core-only, no semantic extras) and still
    # proves the availability cache re-keys when settings change.
    settings.embedding_api_base_url = "http://localhost:11434/v1"
    has_qdrant = importlib.util.find_spec("qdrant_client") is not None
    assert local_rag.semantic_available() is has_qdrant


def test_api_embed_posts_openai_shape_and_adopts_dim(monkeypatch):
    settings.rag_embeddings = "api"
    settings.embedding_api_base_url = "https://example.test/v1"
    settings.embedding_api_key = "sk-test"
    settings.embedding_model = "text-embedding-3-small"
    settings.embedding_dim = 384
    calls = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["url"] = url
        calls["auth"] = headers.get("authorization")
        calls["model"] = json["model"]
        return _FakeResp([[0.1] * 1536 for _ in json["input"]])

    monkeypatch.setattr("httpx.post", fake_post)
    vecs = local_rag.embed_texts(["a", "b"])
    assert calls["url"] == "https://example.test/v1/embeddings"
    assert calls["auth"] == "Bearer sk-test"
    assert calls["model"] == "text-embedding-3-small"
    assert len(vecs) == 2 and len(vecs[0]) == 1536
    assert settings.embedding_dim == 1536  # adopted so new collections match


def test_api_embed_no_key_sends_no_auth_header(monkeypatch):
    settings.rag_embeddings = "api"
    settings.embedding_api_base_url = "http://localhost:11434/v1"
    settings.embedding_api_key = ""
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["auth"] = headers.get("authorization")
        return _FakeResp([[0.5] * 8])

    monkeypatch.setattr("httpx.post", fake_post)
    assert local_rag.embed_text("hello") == [0.5] * 8
    assert seen["auth"] is None  # local Ollama needs no bearer token


def test_probe_tfidf_always_ok():
    settings.rag_embeddings = "tfidf"
    r = local_rag.embedding_probe()
    assert r["ok"] is True


def test_probe_api_without_url_explains():
    settings.rag_embeddings = "api"
    settings.embedding_api_base_url = ""
    r = local_rag.embedding_probe()
    assert r["ok"] is False and "base URL" in r["detail"]


# --- one-click CLI install -----------------------------------------------------

def test_install_unknown_backend_fails_soft():
    r = agent_backends.install("no-such-tool")
    assert r["ok"] is False and "unknown" in r["output"]


def test_install_not_installable():
    r = agent_backends.install("antigravity")  # no headless mode → no installer
    assert r["ok"] is False and "no auto-install" in r["output"]


def test_install_missing_prerequisite(monkeypatch):
    b = agent_backends.BACKENDS["codex"]
    monkeypatch.setattr(type(b), "install_requires", "definitely-missing-prereq")
    r = agent_backends.install("codex")
    assert r["ok"] is False and "definitely-missing-prereq" in r["output"]


def test_install_runs_cmd_and_redetects(monkeypatch):
    b = agent_backends.BACKENDS["codex"]
    monkeypatch.setattr(type(b), "install_cmd", ("true",))  # trivially succeeds
    monkeypatch.setattr(type(b), "install_requires", "")
    # After "installing", point the path at a real binary so re-detect succeeds.
    monkeypatch.setattr("app.config.settings.codex_cli_path", "ls")
    b.reset_detection()
    r = agent_backends.install("codex")
    assert r["ok"] is True and r["detect"]["available"] is True
    b.reset_detection()


def test_refresh_reprobes_all():
    avail = agent_backends.refresh()
    assert set(avail) == set(agent_backends.BACKENDS)
    for det in avail.values():
        assert "installable" in det

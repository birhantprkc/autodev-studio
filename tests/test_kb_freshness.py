"""KB freshness + survival guarantees.

The invariants the pipeline's compounding knowledge depends on:

  1. Cross-run artifacts (delivery notes, distilled lessons) live in the JSON
     store and are the ONLY thing the store persists — a graph reindex never
     touches them, so history survives a rebuild by construction.
  2. Freshness brings both localization layers to origin/HEAD deterministically:
     the symbol map syncs per changed file, and the code graph reindexes when
     its SHA watermark drifts.

Plus: the symbol map's incremental per-file update must work for every language
the analyzer supports, not just Python — otherwise non-Python repos silently go
stale at the localization fallback tier.
"""

from __future__ import annotations

from app.config import settings
from app.services.knowledge import freshness, store, symbol_map
from app.services.knowledge.facts import KnowledgeDocument

REPO = "https://github.com/example/freshness-test"


def _doc(doc_id: str, doc_type: str, name: str = "") -> KnowledgeDocument:
    return KnowledgeDocument(id=doc_id, type=doc_type, name=name or doc_id,
                             summary=f"summary of {doc_id}")


class TestStoreKeepsOnlyNotes:
    def test_notes_persist_and_reload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        store.save(REPO, _doc("delivery_scope_1", "delivery_note"))
        store.save(REPO, _doc("lessons_core", "lesson"))
        ids = {e["id"] for e in store.load_index(REPO)}
        assert ids == {"delivery_scope_1", "lessons_core"}
        assert store.load(REPO, "delivery_scope_1") is not None
        assert store.load(REPO, "lessons_core") is not None


class TestFreshnessSyncsBothLayers:
    def test_reindexes_graph_and_syncs_symbol_map_on_drift(self, monkeypatch, tmp_path):
        """When origin/HEAD has moved past both watermarks, freshness rebuilds
        the symbol map and reindexes the code graph — both deterministic."""
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        monkeypatch.setattr(settings, "kb_auto_refresh", True)
        (tmp_path / ".git").mkdir()

        monkeypatch.setattr(freshness.git_ops, "workdir", lambda url: tmp_path)
        monkeypatch.setattr(freshness.git_ops, "default_branch", lambda p: "main")
        monkeypatch.setattr(freshness.git_ops, "rev_parse", lambda p, ref=None: "newsha999")

        # No symbol map yet → build; graph available with a stale watermark → reindex.
        built = {}

        def _build(url, sha):
            built["sha"] = sha
            return _FakeMap(sha)

        monkeypatch.setattr(freshness.symbol_map, "load", lambda url: None)
        monkeypatch.setattr(freshness.symbol_map, "build", _build)

        reindexed = {}

        def _reindex(url, sha):
            reindexed["sha"] = sha
            return True

        monkeypatch.setattr(freshness.graph, "available", lambda: True)
        monkeypatch.setattr(freshness.graph, "indexed_sha", lambda url: "oldsha000")
        monkeypatch.setattr(freshness.graph, "ensure_indexed", _reindex)
        monkeypatch.setattr(freshness.write_back, "reconcile_unmerged",
                            lambda url, path, on_event=None: 0)

        out = freshness.refresh_if_stale(REPO)
        assert built["sha"] == "newsha999"
        assert reindexed["sha"] == "newsha999"
        assert "graph_reindexed" in out["action"]
        assert "symbol_map_built" in out["action"]

    def test_graph_skipped_when_watermark_current(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        monkeypatch.setattr(settings, "kb_auto_refresh", True)
        (tmp_path / ".git").mkdir()
        monkeypatch.setattr(freshness.git_ops, "workdir", lambda url: tmp_path)
        monkeypatch.setattr(freshness.git_ops, "default_branch", lambda p: "main")
        monkeypatch.setattr(freshness.git_ops, "rev_parse", lambda p, ref=None: "sha_same")
        monkeypatch.setattr(freshness.symbol_map, "load", lambda url: _FakeMap("sha_same"))
        monkeypatch.setattr(freshness.graph, "available", lambda: True)
        monkeypatch.setattr(freshness.graph, "indexed_sha", lambda url: "sha_same")

        called = {"reindex": False}

        def _no(url, sha):
            called["reindex"] = True
            return True

        monkeypatch.setattr(freshness.graph, "ensure_indexed", _no)
        monkeypatch.setattr(freshness.write_back, "reconcile_unmerged",
                            lambda url, path, on_event=None: 0)
        out = freshness.refresh_if_stale(REPO)
        assert called["reindex"] is False  # watermark current → no reindex
        assert out["action"] == "fresh"

    def test_disabled_short_circuits(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "knowledge_dir", str(tmp_path))
        monkeypatch.setattr(settings, "kb_auto_refresh", False)
        assert freshness.refresh_if_stale(REPO) == {"action": "disabled"}


class _FakeMap:
    def __init__(self, sha):
        self.sha = sha
        self.files = {}

    def symbol_count(self):
        return 0


class TestSymbolMapMultiLanguage:
    def test_analyze_one_indexes_non_python(self, tmp_path):
        (tmp_path / "server.go").write_text(
            "package main\n\ntype Server struct{}\n\nfunc NewServer() *Server { return nil }\n")
        (tmp_path / "app.js").write_text("export function render() {}\n")
        (tmp_path / "notes.txt").write_text("not code\n")

        go_info = symbol_map._analyze_one(tmp_path, "server.go")
        assert go_info["language"] == "Go"
        assert {s["n"] for s in go_info["symbols"]} == {"Server", "NewServer"}

        js_info = symbol_map._analyze_one(tmp_path, "app.js")
        assert js_info["language"] == "JavaScript"
        assert [s["n"] for s in js_info["symbols"]] == ["render"]

        assert symbol_map._analyze_one(tmp_path, "notes.txt") is None

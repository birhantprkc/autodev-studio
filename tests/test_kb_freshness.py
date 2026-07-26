"""KB freshness + survival guarantees.

The two invariants the pipeline's compounding knowledge depends on:

  1. Cross-run artifacts (delivery notes, distilled lessons, symbol map,
     freshness meta) SURVIVE a full LLM-view rebuild — rebuilds regenerate
     interpretation, never history.
  2. Surviving on disk is not enough: they must also survive in RETRIEVAL.
     `indexer.index()` drops+recreates domain collections, and lessons share
     the `modules` collection with regenerated module docs — so the pipeline
     must re-upsert preserved docs after a rebuild.

Plus: the symbol map's incremental per-file update must work for every
language the analyzer supports, not just Python — otherwise non-Python repos
silently go stale at the localization layer.
"""

from __future__ import annotations

from app.services.knowledge import pipeline, store, symbol_map
from app.services.knowledge.facts import KnowledgeDocument

REPO = "https://github.com/example/freshness-test"


def _doc(doc_id: str, doc_type: str, name: str = "") -> KnowledgeDocument:
    return KnowledgeDocument(id=doc_id, type=doc_type, name=name or doc_id,
                             summary=f"summary of {doc_id}")


class TestResetPreservesCrossRunKnowledge:
    def _seed(self):
        store.reset(REPO)
        for e in store.load_index(REPO):
            store.remove(REPO, e["id"])
        store.save(REPO, _doc("mod_core", "module"))
        store.save(REPO, _doc("delivery_scope_1", "delivery_note"))
        store.save(REPO, _doc("lessons_core", "lesson"))

    def test_reset_keeps_deliveries_and_lessons(self):
        self._seed()
        store.reset(REPO)
        ids = {e["id"] for e in store.load_index(REPO)}
        assert "delivery_scope_1" in ids
        assert "lessons_core" in ids
        assert "mod_core" not in ids
        # the underlying JSON survived too, not just the index rows
        assert store.load(REPO, "delivery_scope_1") is not None
        assert store.load(REPO, "lessons_core") is not None
        assert store.load(REPO, "mod_core") is None

    def test_save_all_after_reset_merges_preserved(self):
        self._seed()
        store.reset(REPO)
        store.save_all(REPO, [_doc("mod_core", "module"), _doc("arch", "architecture")])
        ids = {e["id"] for e in store.load_index(REPO)}
        assert ids == {"mod_core", "arch", "delivery_scope_1", "lessons_core"}


class TestRebuildReindexesPreservedDocs:
    def test_generate_upserts_lessons_and_deliveries(self, monkeypatch, tmp_path):
        """After index() rebuilt the domain collections, the preserved
        cross-run docs must be re-upserted or lessons vanish from retrieval."""
        store.reset(REPO)
        for e in store.load_index(REPO):
            store.remove(REPO, e["id"])
        store.save(REPO, _doc("delivery_scope_9", "delivery_note"))
        store.save(REPO, _doc("lessons_core", "lesson"))

        fresh = [_doc("mod_core", "module"), _doc("repository", "repository")]
        indexed: list[list[str]] = []
        upserted: list[list[str]] = []

        class FakeFacts:
            files = [object()]

        monkeypatch.setattr(pipeline, "enabled", lambda: True)
        monkeypatch.setattr(pipeline.analyzer, "analyze_repo", lambda url: FakeFacts())
        monkeypatch.setattr(pipeline.generator, "generate_knowledge",
                            lambda facts, max_modules, progress: (
                                fresh, {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}))
        monkeypatch.setattr(pipeline.indexer, "index",
                            lambda url, docs: indexed.append([d.id for d in docs]))
        monkeypatch.setattr(pipeline.indexer, "upsert",
                            lambda url, docs: upserted.append(sorted(d.id for d in docs)))
        monkeypatch.setattr(pipeline.git_ops, "workdir", lambda url: tmp_path)
        monkeypatch.setattr(pipeline.git_ops, "rev_parse", lambda p: "abc123def")
        monkeypatch.setattr(pipeline.symbol_map, "build", lambda url, sha: None)
        monkeypatch.setattr(pipeline.retriever, "overview", lambda url: "")

        result = pipeline.generate(REPO)
        assert result.generated, result.error
        assert indexed == [["mod_core", "repository"]]
        assert upserted == [["delivery_scope_9", "lessons_core"]]


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

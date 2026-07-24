"""Knowledge-base helpers: repo slugging, collection naming, deterministic point
IDs, and the document → embedding-text rendering.
"""

from app.services import git_ops
from app.services.knowledge import indexer
from app.services.knowledge.facts import KnowledgeDocument


class TestSlug:
    def test_https_url(self):
        assert git_ops.slug("https://github.com/pallets/click") == "pallets__click"

    def test_dot_git_and_trailing_slash_stripped(self):
        assert git_ops.slug("https://github.com/pallets/click.git/") == "pallets__click"

    def test_ssh_url(self):
        assert git_ops.slug("git@github.com:pallets/click.git") == "pallets__click"


class TestCollectionAndPointId:
    def test_collection_name_is_domain_scoped(self):
        name = indexer.collection("https://github.com/pallets/click", "modules")
        assert name == "autodev_kn_pallets__click_modules"

    def test_point_id_is_stable(self):
        url = "https://github.com/pallets/click"
        a = indexer._point_id(url, "doc-1")
        b = indexer._point_id(url, "doc-1")
        assert a == b  # uuid5 → deterministic for the same (repo, doc)

    def test_point_id_differs_per_doc(self):
        url = "https://github.com/pallets/click"
        assert indexer._point_id(url, "doc-1") != indexer._point_id(url, "doc-2")


class TestBuildRetrievalText:
    def _doc(self, **kw):
        base = {"id": "d1", "type": "module", "name": "cli", "summary": "Command line parser"}
        base.update(kw)
        return KnowledgeDocument(**base)

    def test_includes_type_name_and_summary(self):
        text = indexer.build_retrieval_text(self._doc())
        assert "Module: cli" in text
        assert "Summary: Command line parser" in text

    def test_renders_tags_and_content_lists(self):
        doc = self._doc(
            tags=["parsing", "cli"],
            content={"purpose": "parse argv", "symbols": ["Command", "Option", "Group"]},
        )
        text = indexer.build_retrieval_text(doc)
        assert "Tags: parsing, cli" in text
        assert "Purpose: parse argv" in text
        assert "Command" in text and "Option" in text

    def test_truncates_long_lists(self):
        doc = self._doc(content={"files": [f"f{i}.py" for i in range(100)]})
        text = indexer.build_retrieval_text(doc)
        # Only the first 30 items are embedded (cost guard).
        assert "f0.py" in text
        assert "f29.py" in text
        assert "f30.py" not in text

    def test_roundtrips_through_dict(self):
        doc = self._doc(tags=["a"], related=["d2"], content={"purpose": "x"})
        restored = KnowledgeDocument.from_dict(doc.to_dict())
        assert restored == doc

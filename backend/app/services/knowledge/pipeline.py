"""End-to-end knowledge build for one repository.

    analyze (static facts) → generate (LLM views) → store (JSON) → index (Qdrant)

Called from ingest on a background thread, after the chunk-level RAG index is
built. Reports progress through a callback so the UI's `kb_step` reflects each
phase. Safe to skip: if there's no LLM key or the semantic stack is missing, the
platform still works on chunk RAG alone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC

from ...config import settings
from .. import git_ops, local_rag
from . import analyzer, generator, indexer, retriever, store, symbol_map

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeResult:
    generated: bool = False
    doc_count: int = 0
    overview: str = ""
    counts: dict = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0
    error: str | None = None


def enabled() -> bool:
    """Structured knowledge needs an LLM (Groq/OpenAI) and the semantic stack."""
    return bool(settings.generate_knowledge and settings.openai_api_key
                and local_rag.semantic_available())


def generate(repo_url: str, progress=None) -> KnowledgeResult:
    """Build (or rebuild) the structured knowledge for a repo. Never raises —
    returns a KnowledgeResult with `error` set on failure."""
    if not enabled():
        reason = ("knowledge generation disabled" if not settings.generate_knowledge
                  else "no LLM key" if not settings.openai_api_key
                  else "semantic stack unavailable")
        logger.info("knowledge.pipeline: skipping (%s)", reason)
        return KnowledgeResult(error=reason)

    try:
        if progress:
            progress("Analyzing repository structure…", 52)
        facts = analyzer.analyze_repo(repo_url)
        if not facts.files:
            return KnowledgeResult(error="no analyzable files")

        docs, cost = generator.generate_knowledge(
            facts, max_modules=settings.knowledge_max_modules, progress=progress
        )
        if not docs:
            return KnowledgeResult(error="no knowledge documents generated",
                                   tokens_in=cost["tokens_in"], tokens_out=cost["tokens_out"],
                                   cost=cost["cost"])

        if progress:
            progress("Storing knowledge documents…", 97)
        store.reset(repo_url)
        store.save_all(repo_url, docs)

        if progress:
            progress("Embedding knowledge views…", 98)
        indexer.index(repo_url, docs)
        # index() drops+recreates each domain collection touched by the new
        # docs — but lessons live in the `modules` collection alongside module
        # views, so a rebuild just wiped their vectors. Re-upsert every
        # preserved cross-run doc (lessons + delivery notes; store.reset kept
        # their JSON) so accumulated knowledge survives rebuilds in RETRIEVAL,
        # not just on disk. Also self-heals vectors lost to earlier rebuilds.
        preserved = [d for d in store.load_all(repo_url)
                     if d.type in ("delivery_note", "lesson")]
        if preserved:
            indexer.upsert(repo_url, preserved)

        # Localization layer + freshness watermark: record which commit this
        # knowledge was built from, and persist the line-numbered symbol map
        # (free) that PM grounding and Dev's verified-locations pins consume.
        sha = git_ops.rev_parse(str(git_ops.workdir(repo_url)))
        symbol_map.build(repo_url, sha)
        from datetime import datetime

        from . import freshness
        freshness.write_meta(repo_url, views_sha=sha,
                             last_full_rebuild_at=datetime.now(UTC).isoformat(),
                             full_rebuild_pending=False)

        counts = store.counts_by_domain(repo_url)
        result = KnowledgeResult(
            generated=True, doc_count=len(docs), overview=retriever.overview(repo_url),
            counts=counts, tokens_in=cost["tokens_in"], tokens_out=cost["tokens_out"],
            cost=cost["cost"],
        )
        logger.info("knowledge.pipeline: %s — %d docs, $%.4f", repo_url, len(docs), cost["cost"])
        return result
    except Exception as exc:  # noqa: BLE001
        logger.exception("knowledge.pipeline failed for %s", repo_url)
        return KnowledgeResult(error=str(exc)[:300])

"""Renders knowledge documents into embeddable text and indexes them per-domain.

We never embed raw JSON. Each knowledge document is rendered into a short,
readable text block first (the "retrieval document"), then embedded (reusing
`local_rag`'s fastembed model) and upserted into a per-repo, per-domain Qdrant
collection: `autodev_kn_<repo-slug>_<domain>`. Repos never share vectors.
"""

from __future__ import annotations

import logging
import uuid

from ...config import settings
from .. import git_ops, local_rag
from .domains import DOMAINS, domain_of
from .facts import KnowledgeDocument

logger = logging.getLogger(__name__)

_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000ad")


def collection(repo_url: str, domain: str) -> str:
    return f"autodev_kn_{git_ops.slug(repo_url)}_{domain}"


def _point_id(repo_url: str, doc_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"{git_ops.slug(repo_url)}:{doc_id}"))


def build_retrieval_text(doc: KnowledgeDocument) -> str:
    """Render a knowledge document into the text we embed."""
    lines = [f"{doc.type.replace('_', ' ').title()}: {doc.name}", f"Summary: {doc.summary}"]
    if doc.tags:
        lines.append(f"Tags: {', '.join(doc.tags)}")
    content = doc.content
    for key in ("purpose", "description", "style", "kind"):
        if content.get(key):
            lines.append(f"{key.title()}: {content[key]}")
    for key in ("modules", "dependencies", "symbols", "endpoints", "steps", "layers",
                "files", "gotchas", "wiring"):
        if content.get(key):
            lines.append(f"{key.title()}: {', '.join(map(str, content[key][:30]))}")
    if doc.related:
        lines.append(f"Related: {', '.join(doc.related)}")
    return "\n".join(lines)


def reset(repo_url: str) -> None:
    """Drop this repo's domain collections (re-analysis is a clean slate)."""
    if not local_rag.semantic_available():
        return
    client = local_rag.qdrant_client()
    for domain in DOMAINS:
        coll = collection(repo_url, domain)
        try:
            if client.collection_exists(coll):
                client.delete_collection(coll)
        except Exception:  # noqa: BLE001
            pass


def index(repo_url: str, docs: list[KnowledgeDocument]) -> bool:
    """Embed and upsert docs into their per-domain collections. Returns False if
    the semantic stack isn't available (knowledge is still stored as JSON)."""
    if not docs or not local_rag.semantic_available():
        return False
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = local_rag.qdrant_client()
    vectors = local_rag.embed_texts([build_retrieval_text(d) for d in docs])

    by_domain: dict[str, list] = {}
    for doc, vec in zip(docs, vectors, strict=False):
        by_domain.setdefault(domain_of(doc.type), []).append((doc, vec))

    for domain, items in by_domain.items():
        coll = collection(repo_url, domain)
        if client.collection_exists(coll):
            client.delete_collection(coll)
        client.create_collection(
            collection_name=coll,
            vectors_config=VectorParams(size=settings.embedding_dim, distance=Distance.COSINE),
        )
        points = [
            PointStruct(
                id=_point_id(repo_url, doc.id), vector=vec,
                payload={"id": doc.id, "type": doc.type, "name": doc.name},
            )
            for doc, vec in items
        ]
        client.upsert(collection_name=coll, points=points)
        logger.info("knowledge.indexed %s/%s — %d docs", repo_url, domain, len(points))
    return True


def upsert(repo_url: str, docs: list[KnowledgeDocument]) -> bool:
    """Embed and upsert docs WITHOUT dropping their collections — for
    incremental refreshes and delivery-note write-back. Creates a domain's
    collection on first use. Never raises: the JSON store is the source of
    truth, so a temporarily unreachable index (e.g. embedded Qdrant locked by
    another process) just means retrieval lags until the next full index."""
    try:
        return _upsert(repo_url, docs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge.upsert skipped (%s)", str(exc)[:160])
        return False


def _upsert(repo_url: str, docs: list[KnowledgeDocument]) -> bool:
    if not docs or not local_rag.semantic_available():
        return False
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = local_rag.qdrant_client()
    vectors = local_rag.embed_texts([build_retrieval_text(d) for d in docs])
    by_domain: dict[str, list] = {}
    for doc, vec in zip(docs, vectors, strict=False):
        by_domain.setdefault(domain_of(doc.type), []).append((doc, vec))

    for domain, items in by_domain.items():
        coll = collection(repo_url, domain)
        if not client.collection_exists(coll):
            client.create_collection(
                collection_name=coll,
                vectors_config=VectorParams(size=settings.embedding_dim, distance=Distance.COSINE),
            )
        client.upsert(collection_name=coll, points=[
            PointStruct(id=_point_id(repo_url, doc.id), vector=vec,
                        payload={"id": doc.id, "type": doc.type, "name": doc.name})
            for doc, vec in items
        ])
        logger.info("knowledge.upserted %s/%s — %d docs", repo_url, domain, len(items))
    return True


def delete(repo_url: str, docs: list[KnowledgeDocument]) -> None:
    """Remove docs' points from their domain collections (best-effort)."""
    if not docs or not local_rag.semantic_available():
        return
    client = local_rag.qdrant_client()
    by_domain: dict[str, list[str]] = {}
    for doc in docs:
        by_domain.setdefault(domain_of(doc.type), []).append(_point_id(repo_url, doc.id))
    for domain, ids in by_domain.items():
        coll = collection(repo_url, domain)
        try:
            if client.collection_exists(coll):
                client.delete(collection_name=coll, points_selector=ids)
        except Exception:  # noqa: BLE001
            pass

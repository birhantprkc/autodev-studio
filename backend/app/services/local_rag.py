"""Shared local embedding primitives — fastembed + an embedded Qdrant client.

This module deliberately does NOT embed raw source code. Embedding whole repos of
code chunks was expensive (multi-GB RAM, minutes of CPU) for little value; the
agents are grounded in the *structured knowledge* subsystem instead
(services/knowledge/: AST facts → LLM-written knowledge docs → embedded docs).

What lives here now is only the small, shared machinery that the knowledge
indexer/retriever reuse: one fastembed model (bge-small) and one embedded Qdrant
client for the process (it locks the storage path, so there must be exactly one).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend availability: fastembed + Qdrant (semantic embeddings).
# ---------------------------------------------------------------------------
def _semantic_available() -> bool:
    """Resolve once whether the semantic stack (fastembed + qdrant) can load."""
    cached = getattr(_semantic_available, "_cache", None)
    if cached is not None:
        return cached
    ok = False
    if settings.rag_embeddings == "semantic":
        try:
            import fastembed  # noqa: F401
            import qdrant_client  # noqa: F401
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.info("local_rag: semantic stack unavailable (%s)", exc)
    _semantic_available._cache = ok
    return ok


def _embedder():
    """Lazily construct the fastembed model once."""
    model = getattr(_embedder, "_cache", None)
    if model is None:
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=settings.embedding_model)
        _embedder._cache = model
        logger.info("local_rag: fastembed model loaded (%s)", settings.embedding_model)
    return model


def _qdrant():
    """Single embedded Qdrant client for the process (it locks the storage path)."""
    client = getattr(_qdrant, "_cache", None)
    if client is None:
        from qdrant_client import QdrantClient

        path = Path(settings.qdrant_path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        client = QdrantClient(path=str(path))
        _qdrant._cache = client
        logger.info("local_rag: embedded Qdrant at %s", path)
    return client


# ---------------------------------------------------------------------------
# Public helpers reused by the structured-knowledge subsystem.
# ---------------------------------------------------------------------------
def semantic_available() -> bool:
    return _semantic_available()


def embed_texts(texts: list[str]) -> list[list[float]]:
    return [list(v) for v in _embedder().embed(texts)]


def embed_text(text: str) -> list[float]:
    return list(next(_embedder().embed([text])))


def qdrant_client():
    return _qdrant()


def close() -> None:
    """Close the embedded Qdrant client cleanly (called on app shutdown)."""
    client = getattr(_qdrant, "_cache", None)
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        _qdrant._cache = None

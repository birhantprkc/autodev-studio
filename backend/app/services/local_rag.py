"""Shared embedding primitives — pluggable embedder + an embedded Qdrant client.

This module deliberately does NOT embed raw source code. Embedding whole repos of
code chunks was expensive (multi-GB RAM, minutes of CPU) for little value; the
agents are grounded in the *structured knowledge* subsystem instead
(services/knowledge/: AST facts → LLM-written knowledge docs → embedded docs).

What lives here is the small, shared machinery the knowledge indexer/retriever
reuse: ONE embedder for the process and one embedded Qdrant client (it locks the
storage path, so there must be exactly one). The embedder is pluggable via
``settings.rag_embeddings``:

  * ``semantic`` — local fastembed (default; free, no key, ~100MB model)
  * ``api``      — any OpenAI-compatible ``/embeddings`` endpoint the operator
                   configures (OpenAI, Gemini, Voyage, or a local Ollama/LM
                   Studio server). Vectors still live in embedded Qdrant.
  * ``tfidf``    — no dense vectors at all (the knowledge store's pure-Python
                   fallback path).

Caches are keyed on the settings that produced them, so flipping the embedding
config in the Settings UI takes effect immediately — no restart. Switching
embedders changes the vector space: existing repos must be re-indexed
(Repos → Reindex) before retrieval works again.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


def _mode_key() -> tuple:
    """The settings tuple the availability/embedder caches are valid for."""
    return (settings.rag_embeddings, settings.embedding_model,
            settings.embedding_api_base_url, bool(settings.embedding_api_key))


# ---------------------------------------------------------------------------
# Backend availability.
# ---------------------------------------------------------------------------
def _semantic_available() -> bool:
    """Whether dense retrieval can run with the CURRENT embedding settings.
    Re-resolved automatically when the operator changes them (cache is keyed
    on the settings)."""
    cached = getattr(_semantic_available, "_cache", None)
    key = _mode_key()
    if cached is not None and cached[0] == key:
        return cached[1]
    ok = False
    if settings.rag_embeddings == "semantic":
        try:
            import fastembed  # noqa: F401
            import qdrant_client  # noqa: F401
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.info("local_rag: semantic stack unavailable (%s)", exc)
    elif settings.rag_embeddings == "api":
        # Storage still needs qdrant; the embedder just needs a base URL.
        if not settings.embedding_api_base_url:
            logger.info("local_rag: api embeddings need embedding_api_base_url set")
        else:
            try:
                import qdrant_client  # noqa: F401
                ok = True
            except Exception as exc:  # noqa: BLE001
                logger.info("local_rag: qdrant unavailable for api embeddings (%s)", exc)
    _semantic_available._cache = (key, ok)
    return ok


def _embedder():
    """Lazily construct the fastembed model once (per configured model)."""
    cached = getattr(_embedder, "_cache", None)
    if cached is None or cached[0] != settings.embedding_model:
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=settings.embedding_model)
        _embedder._cache = (settings.embedding_model, model)
        logger.info("local_rag: fastembed model loaded (%s)", settings.embedding_model)
    return _embedder._cache[1]


def _api_embed(texts: list[str]) -> list[list[float]]:
    """Embed through the operator's OpenAI-compatible endpoint, in batches.
    On the first call the returned dimension is adopted as embedding_dim, so
    collections created afterwards match whatever model they chose."""
    import httpx

    base = settings.embedding_api_base_url.rstrip("/")
    headers = {"content-type": "application/json"}
    if settings.embedding_api_key:
        headers["authorization"] = f"Bearer {settings.embedding_api_key}"
    out: list[list[float]] = []
    for i in range(0, len(texts), 64):
        batch = texts[i:i + 64]
        r = httpx.post(f"{base}/embeddings", headers=headers,
                       json={"model": settings.embedding_model, "input": batch},
                       timeout=120)
        r.raise_for_status()
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        out.extend([d["embedding"] for d in data])
    if out and len(out[0]) != settings.embedding_dim:
        logger.info("local_rag: api embedding dim %d adopted (was %d)",
                    len(out[0]), settings.embedding_dim)
        settings.embedding_dim = len(out[0])
    return out


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
    if settings.rag_embeddings == "api":
        return _api_embed(texts)
    return [list(v) for v in _embedder().embed(texts)]


def embed_text(text: str) -> list[float]:
    if settings.rag_embeddings == "api":
        return _api_embed([text])[0]
    return list(next(_embedder().embed([text])))


def embedding_probe() -> dict:
    """One tiny embed with the current settings — the Settings screen's 'Test
    embeddings' button. Returns {ok, detail} and never raises."""
    mode = settings.rag_embeddings
    if mode == "tfidf":
        return {"ok": True, "detail": "tfidf — pure-Python, nothing to test"}
    if not _semantic_available():
        detail = ("set the embeddings base URL first" if mode == "api" and
                  not settings.embedding_api_base_url
                  else "semantic stack (fastembed + qdrant) not importable")
        return {"ok": False, "detail": detail}
    try:
        v = embed_text("embedding connectivity probe")
        return {"ok": True,
                "detail": f"{mode} · {settings.embedding_model} · {len(v)} dims"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": str(exc)[:300]}


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

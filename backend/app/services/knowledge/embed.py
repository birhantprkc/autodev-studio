"""Local dense-embedding channel — real semantic recall over the graph's nodes.

The code graph's own `semantic_query` uses *static token embeddings* (a frozen
word-vector table), which measured as near-useless (it returned Makefiles for
"bold coloured text"). This module replaces it with a real contextual embedding
model run locally via fastembed (ONNX, CPU, no Docker, no API key), stored in an
embedded Qdrant collection per repo and fused with the graph's BM25 via RRF.

Design (hybrid recipe borrowed from SocratiCode, adapted to our graph):
  * We embed the GRAPH'S NODES (functions/methods/classes). The graph already
    did AST-accurate chunking, so we reuse its symbol inventory instead of
    re-chunking files: one vector per symbol, keyed by qualified name, and every
    hit maps straight back to a real file:line plus its callers.
  * Doc text is `search_document: <file>\n<label> <name><sig>\n<head of body>`;
    query text is `search_query: <q>`. Those task prefixes are REQUIRED by the
    nomic family — omitting them measurably degrades retrieval.

Memory discipline (this runs on small dev boxes, and ONNX is greedy):
  ONNX Runtime's CPU arena grows with SEQUENCE LENGTH and never gives it back —
  measured 1.2 GB and climbing with 2 KB inputs, versus a flat 459 MB plateau
  with ~600-char inputs at batch 8, single-threaded. So we:
    1. cap doc text (`_DOC_CHARS`) — a symbol's file, signature, docstring and
       body head carry nearly all of its semantic signal anyway,
    2. embed in small batches, single-threaded,
    3. run the whole BUILD in a subprocess, so every byte is returned to the OS
       when it exits (the server process never holds the model during a build),
    4. refuse to load the model when free RAM is under `_MIN_FREE_MB`, degrading
       to BM25-only instead of inviting the OOM killer.
Nothing here is a quality compromise: the model and its vectors are unchanged.

Everything is optional and fail-open: without the [semantic] extra, with the
feature off, or under memory pressure, `search()` returns [] and retrieval falls
back to the graph's BM25 channel.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from ...config import settings
from .. import git_ops
from . import graph

logger = logging.getLogger(__name__)

_DIM = 768          # nomic-embed-text / jina-code family
_DOC_CHARS = 700    # per-node doc text cap — keeps the ONNX arena flat
_BODY_LINES = 12    # source lines past the definition folded into the doc text
_BATCH = 8          # small embed batches: bounded ONNX arena
_UPSERT_EVERY = 200  # but flush to Qdrant in big chunks — local writes are the
                     # slow part, and points are tiny in RAM (768 floats each)
_MIN_FREE_MB = 900  # refuse to load the model below this much available RAM
# Threads are the other arena multiplier: each intra-op thread carries its own.
# Scale with the RAM actually available rather than hard-coding the worst case —
# a roomy machine should not be throttled to a dev-box budget.
_THREAD_TIERS = ((3600, 4), (2400, 2))  # (min free MB, threads); else 1
_BUILD_TIMEOUT = 3600
_UUID_NS = uuid.UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")

_model = None       # cached fastembed TextEmbedding (query path only)
_model_name = ""
_client = None      # cached embedded Qdrant client


# --- Availability + memory guard ---------------------------------------------

def enabled() -> bool:
    return bool(settings.local_embeddings)


def available() -> bool:
    """Feature on AND the optional stack importable."""
    if not enabled():
        return False
    try:
        import fastembed  # noqa: F401
        import qdrant_client  # noqa: F401
    except ImportError:
        return False
    return True


def free_mb() -> int:
    """Available (not merely free) RAM in MB; -1 when unknown."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def _enough_ram() -> bool:
    mb = free_mb()
    if mb < 0:
        return True  # unknown platform — don't block
    if mb < _MIN_FREE_MB:
        logger.warning("knowledge.embed: only %d MB RAM available (< %d) — "
                       "skipping the dense channel for now (BM25 still active)",
                       mb, _MIN_FREE_MB)
        return False
    return True


# --- Model / store handles ----------------------------------------------------

def _model_id() -> str:
    return settings.embedding_model or "nomic-ai/nomic-embed-text-v1.5-Q"


def _threads() -> int:
    """How many ONNX threads this machine can afford right now."""
    mb = free_mb()
    if mb < 0:
        return 2
    for floor, n in _THREAD_TIERS:
        if mb >= floor:
            return n
    return 1


def _limit_threads(n: int = 1) -> None:
    """Cap the BLAS/OpenMP pools too — each thread carries its own arena."""
    for var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[var] = str(n)


@contextlib.contextmanager
def _quiet_progress():
    """Swallow fastembed's download progress bar.

    fastembed downloads the model with a bare ``tqdm`` and hardcodes
    ``show_progress=True`` — there is no flag to pass through ``TextEmbedding``,
    and tqdm 4.68 ignores ``TQDM_DISABLE``. tqdm redraws with ``\\r``, which
    collapses to a single line only on a TTY; under the CLI's Rich spinner every
    1 KB chunk becomes a NEW line instead, so a 90 MB download printed thousands
    of "Downloading bytes: ####" rows and buried the interface.

    Swapping the streams is safe for logging: ``logging.StreamHandler`` binds
    ``sys.stderr`` when it is constructed, so handlers made earlier keep writing
    to the real one and only tqdm's direct writes land in the buffer.
    """
    buf = io.StringIO()
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err


def _get_model():
    global _model, _model_name
    name = _model_id()
    if _model is None or _model_name != name:
        n = _threads()
        _limit_threads(n)
        from fastembed import TextEmbedding
        logger.info("knowledge.embed: loading embedding model %s (%d thread(s), "
                    "%d MB free) — first run downloads ~90 MB, once", name, n, free_mb())
        started = time.monotonic()
        with _quiet_progress() as noise:
            _model = TextEmbedding(name, threads=n)
        elapsed = time.monotonic() - started
        # Report the download as ONE line, and only when there actually was one:
        # a warm cache loads in well under a second and deserves no announcement.
        if "Downloading" in noise.getvalue() or elapsed > 5:
            logger.info("knowledge.embed: model %s ready in %.0fs", name, elapsed)
        _model_name = name
    return _model


def unload_model() -> None:
    """Drop the cached model and return its memory to the OS. Called after a
    build and available to callers that want the server slim between runs."""
    global _model, _model_name
    if _model is not None:
        _model = None
        _model_name = ""
        import gc
        gc.collect()


def _close_client() -> None:
    """Close the embedded store before interpreter teardown — qdrant-client's
    __del__ runs after sys.meta_path is torn down and raises a noisy ImportError
    otherwise (harmless, but it pollutes every log and test run)."""
    global _client
    client, _client = _client, None
    if client is not None:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _get_client():
    global _client
    if _client is None:
        import atexit

        from qdrant_client import QdrantClient
        path = Path(settings.qdrant_path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        _client = QdrantClient(path=str(path))
        atexit.register(_close_client)
    return _client


def _collection(repo_url: str) -> str:
    return "kn_emb_" + git_ops.slug(repo_url).replace("-", "_").replace(".", "_")


def _point_id(qualified_name: str) -> str:
    return str(uuid.uuid5(_UUID_NS, qualified_name))


# --- Node enumeration + doc-text assembly -------------------------------------

def _nodes(repo_url: str) -> list[dict]:
    """Every embeddable graph node (function/method/class) with its span."""
    rows = graph.cypher(
        repo_url,
        "MATCH (n) WHERE (n:Function OR n:Method OR n:Class) "
        "AND n.file_path IS NOT NULL "
        "RETURN n.name, n.qualified_name, n.file_path, n.start_line, n.end_line, "
        "labels(n), n.signature LIMIT 40000")
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6 or not r[1] or not r[2]:
            continue
        out.append({
            "name": r[0], "qn": r[1], "file": r[2],
            "start": graph._int(r[3]), "end": graph._int(r[4]),
            "label": graph._label(r[5]), "sig": (r[6] or "") if len(r) > 6 else "",
        })
    return out


def _doc_text(node: dict, body: str) -> str:
    head = f"{node['label']} {node['name']}{node['sig']}"
    parts = [f"search_document: {node['file']}", head]
    if body:
        parts.append(body)
    return "\n".join(parts)[:_DOC_CHARS]


def _read_bodies(repo_url: str, nodes: list[dict]) -> dict[str, str]:
    """First `_BODY_LINES` of each node, one file read per file."""
    path = git_ops.workdir(repo_url)
    by_file: dict[str, list[dict]] = {}
    for n in nodes:
        by_file.setdefault(n["file"], []).append(n)
    bodies: dict[str, str] = {}
    for rel, ns in by_file.items():
        try:
            lines = (path / rel).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for n in ns:
            s = (n["start"] or 1) - 1
            if 0 <= s < len(lines):
                bodies[n["qn"]] = "\n".join(lines[s:s + _BODY_LINES]).strip()[:_DOC_CHARS]
    return bodies


# --- Build (runs in a subprocess) ---------------------------------------------

def build(repo_url: str) -> int:
    """(Re)build the dense index for a repo, in a SUBPROCESS so the model's
    memory is fully reclaimed when it finishes. Returns the vector count
    (0 on failure/unavailable). Never raises."""
    if not available() or not graph.available():
        return 0
    if not _enough_ram():
        return 0
    env = {**os.environ, "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "OPENBLAS_NUM_THREADS": "1"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "app.services.knowledge.embed", repo_url],
            capture_output=True, text=True, errors="replace",
            timeout=_BUILD_TIMEOUT, env=env,
            cwd=str(Path(__file__).resolve().parents[3]),  # backend/
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("knowledge.embed: build subprocess failed: %s", exc)
        return 0
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("VECTORS="):
            n = int(line.split("=", 1)[1] or 0)
            logger.info("knowledge.embed: indexed %d node vectors for %s", n, repo_url)
            return n
    logger.warning("knowledge.embed: build produced no result (%s)",
                   (proc.stderr or "")[-200:])
    return 0


def build_inprocess(repo_url: str, progress=None) -> int:
    """The actual build. Runs in the subprocess spawned by `build()`."""
    from qdrant_client import models as qm

    nodes = _nodes(repo_url)
    if not nodes:
        return 0
    bodies = _read_bodies(repo_url, nodes)
    client = _get_client()
    coll = _collection(repo_url)
    if client.collection_exists(coll):
        client.delete_collection(coll)
    client.create_collection(
        coll,
        vectors_config=qm.VectorParams(size=_DIM, distance=qm.Distance.COSINE,
                                       on_disk=True),
        on_disk_payload=True)

    model = _get_model()
    total = 0
    pending: list = []

    def flush() -> None:
        if pending:
            client.upsert(coll, points=pending)
            pending.clear()

    for i in range(0, len(nodes), _BATCH):
        batch = nodes[i:i + _BATCH]
        texts = [_doc_text(n, bodies.get(n["qn"], "")) for n in batch]
        vecs = [v.tolist() for v in model.embed(texts, batch_size=_BATCH)]
        pending.extend(
            qm.PointStruct(
                id=_point_id(n["qn"]), vector=v,
                payload={"name": n["name"], "qn": n["qn"], "file": n["file"],
                         "line": n["start"], "label": n["label"]})
            for n, v in zip(batch, vecs, strict=False))
        total += len(batch)
        if len(pending) >= _UPSERT_EVERY:
            flush()
            if progress:
                progress(total, len(nodes))
    flush()
    return total


# --- Search (in-process, guarded) ---------------------------------------------

def search(repo_url: str, query: str, *, limit: int = 10) -> list[dict]:
    """Dense semantic search over the node vectors. Returns node hits
    [{name, file_path, start_line, label, qualified_name, score}] or []."""
    if not available() or not query.strip():
        return []
    try:
        client = _get_client()
        coll = _collection(repo_url)
        if not client.collection_exists(coll):
            return []
        if not _enough_ram() and _model is None:
            return []  # don't load the model into a machine that's already tight
        vector = next(iter(_get_model().embed([f"search_query: {query}"]))).tolist()
        # query_points(), not the removed .search() — qdrant-client >=1.10.
        hits = client.query_points(collection_name=coll, query=vector,
                                   limit=limit, with_payload=True).points
    except Exception as exc:  # noqa: BLE001
        # WARNING, not debug: a silently-empty dense channel is indistinguishable
        # from "no matches" and cost us a whole A/B round once.
        logger.warning("knowledge.embed: search failed for %s: %s", repo_url, exc)
        return []
    out = []
    for h in hits:
        p = h.payload or {}
        out.append({"name": p.get("name"), "file_path": p.get("file"),
                    "start_line": p.get("line"), "label": p.get("label"),
                    "qualified_name": p.get("qn"), "score": float(h.score)})
    return out


def delete(repo_url: str) -> None:
    if not available():
        return
    try:
        _get_client().delete_collection(_collection(repo_url))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":  # subprocess entrypoint (see build())
    _limit_threads()
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        count = build_inprocess(url)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR={exc}", file=sys.stderr)
        count = 0
    print(f"VECTORS={count}")

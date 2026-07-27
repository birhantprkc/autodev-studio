"""Domain-aware retrieval over the structured knowledge.

Given a query, embed it, search each requested domain's Qdrant collection, merge
by best score, then do a single capped hop through each hit's `related` links
(expanded docs are score-penalised). Returns a compact, readable digest the PM
agent can reason over — architecture + the most relevant modules/features/files.

Everything degrades gracefully: without the semantic stack (tfidf mode, or
fastembed/qdrant not installed) retrieval falls back to a pure-Python TF-IDF
ranking over the same stored docs — lower fidelity, same shape. With no
knowledge at all, the retrieval functions return "" and callers degrade.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter

from ...config import settings
from .. import git_ops, local_rag
from . import indexer, store, symbol_map
from .domains import DOMAIN_LABELS, DOMAINS
from .facts import KnowledgeDocument

logger = logging.getLogger(__name__)

_EXPANSION_PENALTY = 0.6
_MAX_EXPANSIONS = 4

# --- Deterministic code-hits channel -----------------------------------------
# Flags (--timeout), env-var-ish UPPER_SNAKE, and identifier-ish words.
_QUERY_TOKEN_RE = re.compile(
    r"--[A-Za-z][A-Za-z0-9-]*|\b[A-Z][A-Z0-9_]{2,}\b|\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
# Only pure filler is stoplisted — generic-but-meaningful words ("output",
# "session") are handled dynamically by the too-many-files guard instead.
_QUERY_STOP = frozenset(
    ["the", "and", "for", "with", "when", "that", "this", "from", "into", "should", "must", "make", "more", "some", "very", "add", "adds", "adding", "new", "fix", "fixes", "fixing", "bug", "feature", "support", "supports", "use", "using", "set", "sets", "allow", "allows", "enable", "enables", "disable", "disables", "implement", "implements", "create", "creates", "update", "updates", "remove", "removes", "after", "before", "where", "which", "what", "user", "users", "all", "any", "able", "want", "need", "needs", "like", "just", "also", "environment", "variable", "variables", "option", "options", "flag", "flags"])
_NOISE_EXT = (".md", ".rst", ".txt", ".yml", ".yaml", ".lock", ".cfg", ".toml", ".ini")


def code_hits(repo_url: str, query: str, max_tokens: int = 5) -> str:
    """Deterministic localization channel alongside semantic retrieval: exact
    symbol-map/git-grep hits for identifier-flavored query terms ('--timeout',
    'NO_COLOR', 'ColorFormatter'), which small embedding models rank poorly.
    Grep is pinned to origin's default branch — the shared clone may sit on a
    stale agent branch at scoping time, and unmerged code must not look real.
    Explicitly reports deliberate-looking terms found NOWHERE, so the PM knows
    a thing is new instead of assuming it exists. No LLM, ~ms per query."""
    path = git_ops.workdir(repo_url)
    if not (path / ".git").exists():
        return ""
    try:
        ref = f"origin/{git_ops.default_branch(str(path))}"
    except Exception:  # noqa: BLE001
        return ""
    smap = symbol_map.load(repo_url)
    blocks: list[str] = []
    seen: set[str] = set()
    for m in _QUERY_TOKEN_RE.finditer(query):
        tok = m.group(0)
        name = tok.lstrip("-")
        low = name.lower()
        if low in _QUERY_STOP or low in seen:
            continue
        seen.add(low)
        # A term that LOOKS like a code artifact (flag, ENV_VAR, snake/CamelCase)
        # deserves a "not found" report; plain words just get skipped quietly.
        deliberate = (tok.startswith("--") or name.isupper() or "_" in name
                      or (name[:1].isupper() and any(c.islower() for c in name)))
        entry: list[str] = []
        defs = smap.lookup(name) if smap else []
        if defs:
            entry.append("defined at " + "; ".join(
                f"{f}:{s['l']} ({s['k']})" for f, s in defs[:2]))
        else:
            pat = re.escape(tok if tok.startswith("--") else name)
            files = git_ops.grep_files(str(path), pat, max_files=31,
                                       ignore_case=not deliberate, ref=ref)
            if not files:
                if deliberate:
                    entry.append("NOT FOUND anywhere in the repo — do not assume "
                                 "it exists; treat as new")
            elif len(files) > 30:
                continue  # too common to be a useful pin
            else:
                rows = git_ops.grep_lines(str(path), pat, max_lines=8,
                                          ignore_case=not deliberate, ref=ref)
                good = [r for r in rows.splitlines()
                        if ":" in r and not r.split(":", 1)[0].endswith(_NOISE_EXT)][:3]
                entry.extend(g[:140] for g in good)
                if len(files) > 3:
                    entry.append(f"… appears in {len(files)} files total")
        if entry:
            blocks.append(f"  {tok}:\n" + "\n".join(f"    {e}" for e in entry))
        if len(blocks) >= max_tokens:
            break
    if not blocks:
        return ""
    return ("Exact code hits for the request's terms (symbol map + git grep against "
            f"{ref} — deterministic, real locations, NOT inferred):\n"
            + "\n".join(blocks))[:1600]


def available(repo_url: str) -> bool:
    return store.has_knowledge(repo_url)


def _search_domain(repo_url: str, domain: str, vector: list[float], k: int) -> list[tuple[str, float]]:
    client = local_rag.qdrant_client()
    coll = indexer.collection(repo_url, domain)
    try:
        if not client.collection_exists(coll):
            return []
        hits = client.query_points(collection_name=coll, query=vector, limit=k, with_payload=True).points
    except Exception as exc:  # noqa: BLE001
        logger.debug("knowledge.retriever: search failed on %s: %s", coll, exc)
        return []
    return [(h.payload.get("id", ""), float(h.score)) for h in hits if h.payload]


_TFIDF_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")


def _tfidf_scores(repo_url: str, query: str, domains: list[str],
                  per_domain_k: int) -> dict[str, tuple[KnowledgeDocument, float]]:
    """Pure-Python TF-IDF + cosine over the stored docs — the retrieval path for
    tfidf mode (and when fastembed/qdrant aren't installed). Same doc corpus and
    result shape as the dense path, so callers can't tell the tiers apart.
    ~100-200 docs per repo, so scoring in-process per query is cheap."""
    from .domains import domain_of

    wanted = set(domains)
    docs = [d for d in store.load_all(repo_url) if domain_of(d.type) in wanted]
    if not docs:
        return {}

    def toks(text: str) -> list[str]:
        return _TFIDF_TOKEN_RE.findall(text.lower())

    corpus = [toks(indexer.build_retrieval_text(d)) for d in docs]
    df: Counter = Counter()
    for c in corpus:
        df.update(set(c))
    n = len(corpus)

    def vec(tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        return {t: (1 + math.log(c)) * math.log(1 + n / (df.get(t) or n))
                for t, c in tf.items()}

    def cos(a: dict[str, float], b: dict[str, float]) -> float:
        num = sum(a[t] * b[t] for t in a.keys() & b.keys())
        den = (math.sqrt(sum(v * v for v in a.values()))
               * math.sqrt(sum(v * v for v in b.values())))
        return num / den if den else 0.0

    q = vec(toks(query))
    per_domain: dict[str, list[tuple[KnowledgeDocument, float]]] = {}
    for doc, c in zip(docs, corpus, strict=False):
        score = cos(q, vec(c))
        if score > 0:
            per_domain.setdefault(domain_of(doc.type), []).append((doc, score))
    results: dict[str, tuple[KnowledgeDocument, float]] = {}
    for hits in per_domain.values():  # per-domain cap mirrors the dense path
        hits.sort(key=lambda x: -x[1])
        for doc, score in hits[:per_domain_k]:
            results[doc.id] = (doc, score)
    return results


def retrieve(repo_url: str, query: str, *, domains: list[str] | None = None,
             per_domain_k: int = 3) -> list[tuple[KnowledgeDocument, float]]:
    """Return scored knowledge documents for `query`, best-score-first.
    Dense (Qdrant) when the semantic stack is up; TF-IDF fallback otherwise."""
    if not query.strip() or not available(repo_url):
        return []
    domains = domains or DOMAINS

    if local_rag.semantic_available():
        vector = local_rag.embed_text(query)
        scores: dict[str, float] = {}
        for domain in domains:
            for doc_id, score in _search_domain(repo_url, domain, vector, per_domain_k):
                scores[doc_id] = max(scores.get(doc_id, 0.0), score)
        results: dict[str, tuple[KnowledgeDocument, float]] = {}
        for doc_id, score in scores.items():
            doc = store.load(repo_url, doc_id)
            if doc is not None:
                results[doc_id] = (doc, score)
    else:
        results = _tfidf_scores(repo_url, query, domains, per_domain_k)

    # One-hop related expansion (capped, penalised).
    expansions = 0
    for doc, score in list(results.values()):
        for rel_id in doc.related:
            if expansions >= _MAX_EXPANSIONS:
                break
            if rel_id in results:
                continue
            rel = store.load(repo_url, rel_id)
            if rel is None:
                continue
            results[rel_id] = (rel, score * _EXPANSION_PENALTY)
            expansions += 1

    return sorted(results.values(), key=lambda x: x[1], reverse=True)


def _render_doc(doc: KnowledgeDocument, score: float | None = None) -> str:
    head = f"[{doc.type}] {doc.name}"
    if score is not None:
        head += f" (score {score:.2f})"
    # Inferred doc types (business rules et al) must not read with the same
    # authority as AST-verified facts — surface non-HIGH confidence inline.
    conf = (doc.confidence or "").upper()
    if conf and conf != "HIGH":
        head += f" (confidence: {conf} — inferred, verify before relying on it)"
    lines = [head]
    if doc.summary:
        lines.append(f"  {doc.summary}")
    files = doc.content.get("files") or []
    if files:
        lines.append(f"  files: {', '.join(files[:8])}")
    # Delivery notes / lesson docs carry validated cross-run learnings — always
    # show them.
    for key in ("wiring", "gotchas", "lessons"):
        vals = doc.content.get(key) or []
        if vals:
            lines.append(f"  {key}: " + " | ".join(str(v)[:200] for v in vals[:4]))
    return "\n".join(lines)


def scope_context(repo_url: str, query: str, *, budget_docs: int = 8) -> str:
    """A compact, query-scoped digest for the PM agent (scoping + ticket drafting)."""
    hits = retrieve(repo_url, query, per_domain_k=3)
    if not hits:
        return ""
    lines = ["Structured repository knowledge (most relevant views first):"]
    for doc, score in hits[:budget_docs]:
        lines.append(_render_doc(doc, score))
    return "\n".join(lines)


def answer(repo_url: str, messages: list[dict], *, timeout: int = 120) -> str:
    """Answer a repo question grounded in the structured knowledge docs (no raw
    code). Retrieves the most relevant views for the latest user turn, then (if an
    LLM key is set) synthesizes a concise grounded reply citing file paths; else
    returns the ranked view digest. Mirrors the old chunk-RAG answer()."""
    query = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    if not query.strip():
        return ""
    hits = retrieve(repo_url, query, per_domain_k=3)
    if not hits:
        return ""

    context_parts: list[str] = []
    for doc, _ in hits[:8]:
        block = _render_doc(doc)
        desc = doc.content.get("description") or doc.content.get("purpose")
        if desc:
            block += f"\n  {str(desc)[:400]}"
        context_parts.append(block)
    context = "\n\n".join(context_parts)

    from .. import llm, providers

    if providers.can_chat(settings.knowledge_provider):
        system = (
            "You are a code knowledge-base assistant. Answer the question using ONLY the "
            "structured repository knowledge provided (architecture, modules, features, "
            "files). Be concise and concrete. Cite the exact file paths you rely on in "
            "`backticks`. If the knowledge is insufficient, say so."
        )
        user = f"Repository knowledge:\n{context[:9000]}\n\nQuestion: {query}"
        r = llm.chat(system, user, provider=settings.knowledge_provider,
                     model=settings.knowledge_model, timeout=timeout)
        if r.get("text"):
            return r["text"]
    return f"Most relevant repository knowledge for “{query.strip()[:120]}”:\n\n{context[:4000]}"


def overview(repo_url: str) -> str:
    """A natural-language architecture overview built from the stored views.
    Used at ingest time as the repo's `kb_overview`."""
    repo = store.load(repo_url, "repository")
    arch = store.load(repo_url, "architecture")
    if not repo and not arch:
        return ""
    parts: list[str] = []
    if repo and repo.summary:
        parts.append(repo.summary)
    if repo and repo.content.get("purpose"):
        parts.append(str(repo.content["purpose"]))
    if arch and arch.summary:
        style = arch.content.get("style")
        parts.append(f"Architecture: {arch.summary}" + (f" (style: {style})." if style else "."))
    features = [d for d in store.load_all(repo_url) if d.type == "feature"][:6]
    if features:
        parts.append("Key features: " + ", ".join(f.name for f in features) + ".")
    return " ".join(parts).strip()[:1200]


def views(repo_url: str) -> dict:
    """All stored documents grouped by domain — for the API / Analysis screen."""
    from .domains import domain_of

    docs = store.load_all(repo_url)
    grouped: dict[str, list[dict]] = {d: [] for d in DOMAINS}
    for doc in docs:
        grouped.setdefault(domain_of(doc.type), []).append({
            "id": doc.id, "type": doc.type, "name": doc.name,
            "summary": doc.summary, "tags": doc.tags,
            "content": doc.content, "confidence": doc.confidence,
        })
    return {
        "slug": git_ops.slug(repo_url),
        "total": len(docs),
        "labels": DOMAIN_LABELS,
        "order": DOMAINS,
        "domains": grouped,
    }

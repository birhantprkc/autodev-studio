# The knowledge base

Every agent is grounded in a knowledge base built from the target repository. This
is the piece that makes the pipeline cheaper and more accurate than sending a cold
agent to explore a repo from scratch — see the
[benchmarks](../benchmarks/kb-vs-claude-code.md) for the measured effect.

The knowledge base has two layers: a **retrieval index** (chunk-level RAG) and a set
of **structured views** (an interpreted map of the repo).

## Layer 1 — retrieval index (RAG)

On ingest, the repo is cloned and every git-tracked text file is chunked with a
line window (≈60 lines, ≈12 overlap). Chunks are vectorized and stored for
similarity search. There are two interchangeable backends:

- **Semantic (default).** [`fastembed`](https://github.com/qdrant/fastembed) with
  `BAAI/bge-small-en-v1.5` (384-dim, local, no API key) produces embeddings that are
  upserted into an **embedded Qdrant** collection per repo, searched by cosine
  distance. Qdrant runs in-process — no Docker, no server — and persists on disk, so
  a repo stays indexed across restarts. The model downloads once (~90 MB) on first
  ingest.
- **TF-IDF (automatic fallback).** If `fastembed`/`qdrant` aren't installed, the RAG
  falls back to a pure-Python TF-IDF index: smoothed `idf`, L2-normalized sparse
  vectors, cosine via sparse dot product. Zero extra dependencies, so retrieval
  always works. Select it explicitly with `RAG_EMBEDDINGS=tfidf`.

The index is queried in two shapes:

- **Q&A** (`answer`) — retrieve relevant chunks, synthesize an answer with the
  configured knowledge model, and cite the source files. This powers the Knowledge
  screen.
- **Precision retrieval** (`retrieve`) — a *use-case-scoped, token-budgeted* slice.
  Each use case (`task-breakdown`, `story-breakdown`, `architecture`, …) has its own
  token budget; ranked chunks are packed greedily until the budget is spent. Feeding
  the Dev agent the *right* files with exact paths — instead of a broad blob — is what
  makes it edit surgically and stops it hallucinating paths.

## Layer 2 — structured views

Chunk retrieval alone doesn't tell an agent how a repo is *organized*. So beyond the
index, each repo is statically analyzed with the standard-library `ast` module and
distilled into interpreted **views**:

- architecture
- modules
- features
- workflows
- entry points
- domain concepts
- business rules
- integrations

Each view is stored as a JSON document and embedded per domain for retrieval. The
important discipline here: **factual fields come from the code** (symbols, imports,
call graph, file lists via AST), and **only the interpretation comes from the LLM**.
That keeps the map grounded rather than hallucinated.

A large top-level package is split into per-subdirectory module views (for example
`httpie/cli`, `httpie/output`, …) so retrieval can localize inside it, rather than
returning one coarse doc per 60-file package. The split threshold and the per-repo
module cap are configurable (`KB_MODULE_SPLIT_FILES`, `KNOWLEDGE_MAX_MODULES`).

### Freshness

When a pipeline run starts, the knowledge base is refreshed if the repo's origin has
moved. A free AST **symbol map** (the localization layer) is always resynced; the
LLM prose views refresh incrementally per affected module, escalating to a full
rebuild only past a drift threshold (fraction of files changed, or an absolute
changed-file count — whichever hits first). This keeps a long-lived knowledge base in
sync without paying to rebuild it on every commit. See
[configuration.md](configuration.md) for the `KB_AUTO_REFRESH` /
`KB_FULL_REBUILD_*` knobs.

## Graceful degradation

The whole subsystem degrades rather than fails:

- No embedding stack → TF-IDF lexical retrieval.
- No LLM key → structured views are skipped; chunk retrieval still works.
- External backends (DeepWiki, Deep-Analysis) are optional and off by default.

## Where it lives in the code

| Concern | Module |
|---|---|
| Chunking, embeddings, Qdrant / TF-IDF | [`services/local_rag.py`](../backend/app/services/local_rag.py) |
| Static analysis → structured views | [`services/knowledge/`](../backend/app/services/knowledge/) |
| Indexing / collections / point IDs | [`services/knowledge/indexer.py`](../backend/app/services/knowledge/indexer.py) |
| Retrieval, scoped context, Q&A | [`services/knowledge/retriever.py`](../backend/app/services/knowledge/retriever.py) |
| Freshness / incremental refresh | [`services/knowledge/freshness.py`](../backend/app/services/knowledge/freshness.py) |
| Precision (use-case-scoped) retrieval | [`services/precision.py`](../backend/app/services/precision.py) |

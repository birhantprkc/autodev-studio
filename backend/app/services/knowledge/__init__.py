"""Structured, multi-view repository knowledge.

Where `local_rag` gives a *chunk-level* index (line-windowed embeddings for
precise code Q&A), this package builds *structured views* of a repository —
architecture, modules, features, workflows, entrypoints, domain concepts,
business rules and integrations — as validated JSON knowledge documents that are
embedded per-domain and fed to the PM agent when it scopes work.

Pipeline (all per-repo, keyed off the repo-url slug):

    clone → analyze (static facts) → generate (LLM views) → store (JSON on disk)
          → index (embed each view into its own Qdrant collection)

The design is modeled on the PM_agent knowledge base: the LLM only ever sees
extracted *facts*, and only the interpretive fields (summaries, tags,
relationships) come from the model — factual fields (files, symbols,
dependencies) are filled in code so they can't be hallucinated.
"""

from . import pipeline, retriever, store  # noqa: F401

"""Reads and writes knowledge JSON files — the files are the source of truth.

Layout under `settings.knowledge_dir/<repo-slug>/`:
    index.json
    structure/repository.json
    structure/architecture.json
    structure/modules/<id>.json
    functional/features/<id>.json
    functional/workflows/<id>.json
    functional/entrypoints/<id>.json
    functional/domain/<id>.json
    functional/business_rules/<id>.json
    functional/integrations/<id>.json
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ...config import settings
from .. import git_ops
from .facts import KnowledgeDocument

_SUBDIR = {
    "repository": "structure", "architecture": "structure",
    "module": "structure/modules", "feature": "functional/features",
    "workflow": "functional/workflows", "entrypoint": "functional/entrypoints",
    "domain": "functional/domain", "business_rule": "functional/business_rules",
    "integration": "functional/integrations", "delivery_note": "functional/deliveries",
    "lesson": "functional/lessons",
}
_SINGLETON = {"repository": "repository.json", "architecture": "architecture.json"}

# Survives `reset()`: knowledge accumulated ACROSS runs (delivery notes) and the
# deterministic localization layer (symbol map + freshness meta) must not be
# wiped by a full LLM-view rebuild — rebuilds regenerate interpretation, not
# history.
_KEEP_ON_RESET = ("functional/deliveries", "functional/lessons", "symbols.json",
                  "meta.json", "gaps.jsonl")
_KEEP_TYPES = ("delivery_note", "lesson")


def _base(repo_url: str) -> Path:
    return Path(settings.knowledge_dir).resolve() / git_ops.slug(repo_url)


def _relative_path(doc: KnowledgeDocument) -> str:
    subdir = _SUBDIR.get(doc.type, "structure")
    filename = _SINGLETON.get(doc.type, f"{doc.id}.json")
    return f"{subdir}/{filename}"


def reset(repo_url: str) -> None:
    """Clear generated knowledge, preserving cross-run artifacts (delivery
    notes, symbol map, freshness meta) and their index entries."""
    base = _base(repo_url)
    if not base.exists():
        return
    kept_entries = [e for e in load_index(repo_url)
                    if e.get("type") in _KEEP_TYPES and (base / e["path"]).exists()]
    for child in list(base.iterdir()):
        rel = child.name
        if rel in [k.split("/")[0] for k in _KEEP_ON_RESET if "/" not in k]:
            continue
        if child.is_dir():
            # Keep protected subtrees (e.g. functional/deliveries) inside dirs.
            keep_subs = [k.split("/", 1)[1] for k in _KEEP_ON_RESET
                         if "/" in k and k.split("/", 1)[0] == rel]
            if keep_subs:
                for sub in list(child.iterdir()):
                    if sub.name not in keep_subs:
                        shutil.rmtree(sub) if sub.is_dir() else sub.unlink()
                continue
            shutil.rmtree(child)
        else:
            child.unlink()
    _write_index(repo_url, kept_entries)


def _write_index(repo_url: str, entries: list[dict]) -> None:
    base = _base(repo_url)
    base.mkdir(parents=True, exist_ok=True)
    (base / "index.json").write_text(
        json.dumps({"documents": entries}, indent=2), encoding="utf-8"
    )


def save_all(repo_url: str, docs: list[KnowledgeDocument]) -> list[dict]:
    base = _base(repo_url)
    base.mkdir(parents=True, exist_ok=True)
    new_ids = {d.id for d in docs}
    # Existing preserved entries (delivery notes survive full rebuilds).
    entries = [e for e in load_index(repo_url)
               if e.get("type") in _KEEP_TYPES and e["id"] not in new_ids
               and (base / e["path"]).exists()]
    for doc in docs:
        rel = _relative_path(doc)
        full = base / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(json.dumps(doc.to_dict(), indent=2), encoding="utf-8")
        entries.append({"id": doc.id, "type": doc.type, "name": doc.name, "path": rel})
    _write_index(repo_url, entries)
    return entries


def save(repo_url: str, doc: KnowledgeDocument) -> None:
    """Upsert ONE document without touching the rest of the store."""
    base = _base(repo_url)
    rel = _relative_path(doc)
    full = base / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(json.dumps(doc.to_dict(), indent=2), encoding="utf-8")
    entries = [e for e in load_index(repo_url) if e["id"] != doc.id]
    entries.append({"id": doc.id, "type": doc.type, "name": doc.name, "path": rel})
    _write_index(repo_url, entries)


def remove(repo_url: str, doc_id: str) -> None:
    base = _base(repo_url)
    entries = load_index(repo_url)
    for e in entries:
        if e["id"] == doc_id:
            try:
                (base / e["path"]).unlink(missing_ok=True)
            except OSError:
                pass
    _write_index(repo_url, [e for e in entries if e["id"] != doc_id])


def load_index(repo_url: str) -> list[dict]:
    index_file = _base(repo_url) / "index.json"
    if not index_file.exists():
        return []
    try:
        return json.loads(index_file.read_text(encoding="utf-8")).get("documents", [])
    except (json.JSONDecodeError, OSError):
        return []


def load(repo_url: str, doc_id: str) -> KnowledgeDocument | None:
    for entry in load_index(repo_url):
        if entry["id"] == doc_id:
            try:
                raw = (_base(repo_url) / entry["path"]).read_text(encoding="utf-8")
            except OSError:
                return None
            return KnowledgeDocument.from_dict(json.loads(raw))
    return None


def load_all(repo_url: str) -> list[KnowledgeDocument]:
    docs = [load(repo_url, e["id"]) for e in load_index(repo_url)]
    return [d for d in docs if d is not None]


def has_knowledge(repo_url: str) -> bool:
    return bool(load_index(repo_url))


def counts_by_domain(repo_url: str) -> dict[str, int]:
    from .domains import domain_of

    counts: dict[str, int] = {}
    for entry in load_index(repo_url):
        d = domain_of(entry["type"])
        counts[d] = counts.get(d, 0) + 1
    return counts

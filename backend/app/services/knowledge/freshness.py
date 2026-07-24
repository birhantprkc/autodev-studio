"""Keeps the knowledge base current as the repo evolves — at the cheapest cost
that preserves correctness. Called at pipeline-run entry (right after the clone
is reset to origin's default branch, so the working tree == origin/HEAD).

Two layers, each refreshed from its own watermark:

  * **Symbol map** (localization layer, deterministic): re-analyzed for changed
    files only, every time it drifts. Free — so it is ALWAYS exactly current.
  * **LLM prose views** (PM layer, interpretive): tracked by `views_sha` in
    meta.json. Small drift → regenerate only the affected modules' docs (one
    cheap knowledge_model call each, capped) + deterministically sync the
    factual fields of the repository/architecture docs (no LLM). Large drift
    (fraction/count threshold) → full rebuild, rate-limited so a hot repo
    doesn't re-pay ingest cost daily; when rate-limited we still do the capped
    module refresh and flag `full_rebuild_pending` for the next allowed window.

Never raises — a knowledge refresh must not be able to break a pipeline run.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from ...config import settings
from .. import git_ops
from . import analyzer, facts_view, generator, indexer, store, symbol_map, write_back

logger = logging.getLogger(__name__)


def _meta_path(repo_url: str) -> Path:
    return Path(settings.knowledge_dir).resolve() / git_ops.slug(repo_url) / "meta.json"


def read_meta(repo_url: str) -> dict:
    try:
        return json.loads(_meta_path(repo_url).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_meta(repo_url: str, **fields) -> None:
    meta = read_meta(repo_url)
    meta.update(fields)
    fp = _meta_path(repo_url)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _hours_since(iso: str) -> float:
    try:
        then = datetime.fromisoformat(iso)
        return (datetime.now(UTC) - then).total_seconds() / 3600.0
    except (ValueError, TypeError):
        return 1e9


def _log(on_event, level: str, msg: str) -> None:
    logger.info("knowledge.freshness: %s", msg)
    if on_event:
        try:
            on_event(level, msg)
        except Exception:  # noqa: BLE001
            pass


def _sync_factual_content(repo_url: str, facts) -> list:
    """Refresh the deterministic fields of the repository/architecture docs
    from fresh facts, keeping the LLM prose. Returns the updated docs."""
    updated = []
    modules = sorted(facts_view.group_modules(facts))
    repo_doc = store.load(repo_url, "repository")
    if repo_doc is not None:
        repo_doc.content["modules"] = modules
        repo_doc.content["file_count"] = len(facts.files)
        repo_doc.content["languages"] = sorted({f.language for f in facts.files if f.language})
        store.save(repo_url, repo_doc)
        updated.append(repo_doc)
    arch_doc = store.load(repo_url, "architecture")
    if arch_doc is not None:
        arch_doc.content["module_dependencies"] = [
            f"{a} -> {b}" for a, b in facts_view.dependency_edges(facts)]
        store.save(repo_url, arch_doc)
        updated.append(arch_doc)
    return updated


def _refresh_modules(repo_url: str, changed: list[str], deleted: list[str],
                     on_event) -> tuple[list[str], float]:
    """Regenerate the knowledge docs of the modules touched by `changed` +
    `deleted` files (LLM, capped) and sync factual content of the global docs
    (free). Maps files to modules AFTER grouping, so split submodules
    (httpie/cli) refresh their own doc rather than the parent's. Returns
    (names, cost)."""
    facts = analyzer.analyze_repo(repo_url)  # pure AST, seconds — always fresh
    if not facts.files:
        return [], 0.0
    modules = facts_view.group_modules(facts)
    known = set(modules)
    affected = {facts_view.module_of(f, known) for f in [*changed, *deleted]}
    cost = generator._Cost()
    refreshed: list[str] = []
    to_upsert = _sync_factual_content(repo_url, facts)

    for module in sorted(affected)[:settings.kb_refresh_max_modules]:
        if module in modules:
            doc = generator._module(facts, module, modules[module], cost)
            doc.confidence = "HIGH"
            store.save(repo_url, doc)
            to_upsert.append(doc)
            refreshed.append(module)
        else:  # module directory no longer exists — retire its doc
            doc = store.load(repo_url, generator._slug(module))
            if doc is not None and doc.type == "module":
                indexer.delete(repo_url, [doc])
                store.remove(repo_url, doc.id)
                refreshed.append(f"{module} (removed)")
    if to_upsert:
        indexer.upsert(repo_url, to_upsert)
    _log(on_event, "info",
         f"KB refresh: {len(refreshed)} module view(s) updated ({', '.join(refreshed[:6])})"
         f" — ${cost.cost:.4f}")
    return refreshed, cost.cost


def _full_rebuild(repo_url: str, on_event) -> dict:
    from . import pipeline  # lazy: pipeline imports this module's siblings

    _log(on_event, "info", "KB refresh: large drift — full knowledge rebuild")
    result = pipeline.generate(repo_url)
    if result.generated:
        write_meta(repo_url, last_full_rebuild_at=_now_iso(), full_rebuild_pending=False)
        _log(on_event, "success",
             f"KB rebuilt: {result.doc_count} views (${result.cost:.4f})")
        return {"action": "full_rebuild", "docs": result.doc_count, "cost": result.cost}
    _log(on_event, "warn", f"KB full rebuild failed: {result.error}")
    return {"action": "full_rebuild_failed", "error": result.error}


def refresh_if_stale(repo_url: str, on_event=None) -> dict:
    """Bring the knowledge layers up to origin's default branch. Cheap when
    fresh (one rev-parse). Returns a summary dict; never raises."""
    try:
        return _refresh(repo_url, on_event)
    except Exception as exc:  # noqa: BLE001
        logger.exception("knowledge.freshness failed for %s", repo_url)
        return {"action": "error", "error": str(exc)[:200]}


def _refresh(repo_url: str, on_event) -> dict:
    if not settings.kb_auto_refresh:
        return {"action": "disabled"}
    path = git_ops.workdir(repo_url)
    if not (path / ".git").exists():
        return {"action": "no_clone"}
    head = git_ops.rev_parse(str(path), f"origin/{git_ops.default_branch(str(path))}")
    if not head:
        return {"action": "no_head"}

    # --- Layer 1: symbol map (free, always current) --------------------------
    smap = symbol_map.load(repo_url)
    if smap is None:
        smap = symbol_map.build(repo_url, head)
        _log(on_event, "info",
             f"KB: symbol map built — {len(smap.files)} files, {smap.symbol_count()} symbols")
    elif smap.sha != head:
        delta = git_ops.changed_files(str(path), smap.sha, head)
        if delta is None:
            smap = symbol_map.build(repo_url, head)
        else:
            smap = symbol_map.update(repo_url, delta[0], delta[1], head)
        _log(on_event, "info", f"KB: symbol map synced to {head[:9]}")

    # Delivery notes recorded before their branch merged carry a "do not assume
    # this exists" disclaimer; upgrade any whose work has since landed on the
    # default branch (deterministic, one merge-base check each — never raises).
    write_back.reconcile_unmerged(repo_url, str(path), on_event)

    # --- Layer 2: LLM prose views (watermarked, selective) -------------------
    if not store.has_knowledge(repo_url):
        return {"action": "symbols_only"}  # views come from ingest, not from here
    from . import pipeline

    meta = read_meta(repo_url)
    views_sha = meta.get("views_sha", "")
    if not views_sha:
        # Pre-existing KB from before watermarking: adopt the current head as
        # the baseline; staleness self-heals on the next real drift.
        write_meta(repo_url, views_sha=head)
        return {"action": "baseline"}
    if views_sha == head:
        return {"action": "fresh"}
    if not pipeline.enabled():
        return {"action": "views_stale_no_llm"}  # heal when a key is back

    rebuild_allowed = _hours_since(meta.get("last_full_rebuild_at", "")) \
        >= settings.kb_full_rebuild_min_hours
    delta = git_ops.changed_files(str(path), views_sha, head)
    if delta is None:  # old watermark unresolvable — can't scope the drift
        if rebuild_allowed:
            return _full_rebuild(repo_url, on_event)
        write_meta(repo_url, full_rebuild_pending=True)
        return {"action": "rebuild_deferred"}

    changed = [f for f in delta[0] if analyzer.analyzable(f)]
    deleted = [f for f in delta[1] if analyzer.analyzable(f)]
    if not changed and not deleted and not meta.get("full_rebuild_pending"):
        write_meta(repo_url, views_sha=head)  # docs/CI churn only
        return {"action": "fresh_views"}

    total = max(1, len(smap.files))
    drift = len(changed) + len(deleted)
    big = (drift / total >= settings.kb_full_rebuild_fraction
           or drift >= settings.kb_full_rebuild_files
           or meta.get("full_rebuild_pending", False))
    if big and rebuild_allowed:
        return _full_rebuild(repo_url, on_event)

    refreshed, cost = _refresh_modules(repo_url, changed, deleted, on_event)
    write_meta(repo_url, views_sha=head,
               full_rebuild_pending=big)  # too big but rate-limited → do it later
    return {"action": "incremental", "changed_files": drift,
            "modules_refreshed": refreshed, "cost": cost,
            "full_rebuild_pending": big}

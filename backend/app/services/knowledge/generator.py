"""Knowledge generators: extracted facts -> validated knowledge documents.

Each generator asks the LLM (Groq, JSON mode) ONLY for interpretive fields —
summaries, tags, relationships, descriptions. Factual fields (files, symbols,
dependencies, endpoints) are filled in code from the extracted facts, so the
model can't hallucinate them. Every call's token cost is accumulated so ingest
can report what the knowledge build cost.
"""

from __future__ import annotations

import json
import logging
import re
import time

from ...config import settings
from .. import llm
from . import facts_view
from .facts import ExtractedFacts, FileFacts, KnowledgeDocument

logger = logging.getLogger(__name__)

# Default confidence per document type (how much to trust the inference).
_CONFIDENCE_BY_TYPE = {
    "repository": "HIGH", "architecture": "HIGH", "module": "HIGH",
    "feature": "HIGH", "entrypoint": "HIGH", "integration": "MEDIUM",
    "domain": "MEDIUM", "business_rule": "LOW", "workflow": "MEDIUM",
}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s or "item"


def _load_json(text: str) -> dict:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        if text and "{" in text:
            try:
                return json.loads(text[text.find("{"): text.rfind("}") + 1])
            except (json.JSONDecodeError, ValueError):
                pass
    return {}


class _Cost:
    """Mutable accumulator threaded through a generation run."""

    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost = 0.0

    def add(self, r: dict) -> None:
        self.tokens_in += r.get("tokens_in", 0) or 0
        self.tokens_out += r.get("tokens_out", 0) or 0
        self.cost += r.get("cost", 0.0) or 0.0


def _gen(system: str, user: str, cost: _Cost, *, retries: int = 2) -> dict:
    """One JSON-mode LLM call with a small retry for Groq rate limits."""
    last = {}
    for attempt in range(retries + 1):
        r = llm.chat(system, user, provider=settings.knowledge_provider,
                     model=settings.knowledge_model, json_mode=True, timeout=180)
        cost.add(r)
        err = r.get("error") or ""
        if err and ("429" in err or "rate" in err.lower()) and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue
        last = _load_json(r.get("text", ""))
        if last or not err:
            return last
    return last


def _list(data: dict, *keys: str) -> list[dict]:
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict) and x.get("name")]
    return []


def _tags(item: dict) -> list[str]:
    v = item.get("tags")
    return [str(t) for t in v][:8] if isinstance(v, list) else []


def _mods(item: dict) -> list[str]:
    v = item.get("modules")
    return [str(m) for m in v] if isinstance(v, list) else []


# --- Individual view generators ---------------------------------------------
def _repository(facts: ExtractedFacts, cost: _Cost) -> KnowledgeDocument:
    system = (
        "You are a software analyst. You are given factual structure extracted from "
        "a repository. Describe the repository as a whole. Respond ONLY as JSON: "
        '{"summary": string, "purpose": string, "tags": [string], "related": [string]}. '
        "Be concise and base every statement on the facts provided."
    )
    data = _gen(system, facts_view.render_facts(facts), cost)
    modules = sorted(facts_view.group_modules(facts))
    languages = sorted({f.language for f in facts.files if f.language})
    return KnowledgeDocument(
        id="repository", type="repository", name="Repository Overview",
        summary=data.get("summary", ""), tags=_tags(data),
        related=[str(r) for r in data.get("related", [])] or modules,
        content={
            "purpose": data.get("purpose", ""),
            "languages": languages, "modules": modules, "file_count": len(facts.files),
        },
    )


def _architecture(facts: ExtractedFacts, cost: _Cost) -> KnowledgeDocument:
    system = (
        "You are a software architect. Given a repository's modules and how they "
        "depend on each other, describe the architecture. Respond ONLY as JSON: "
        '{"summary": string, "style": string, "layers": [string], "notes": [string], '
        '"tags": [string]}. Base it only on the facts (e.g. "layered", "modular '
        'monolith", "microservices").'
    )
    data = _gen(system, facts_view.render_facts(facts), cost)
    edges = [f"{a} -> {b}" for a, b in facts_view.dependency_edges(facts)]
    return KnowledgeDocument(
        id="architecture", type="architecture", name="Architecture",
        summary=data.get("summary", ""), tags=_tags(data),
        related=sorted(facts_view.group_modules(facts)),
        content={
            "style": data.get("style", ""),
            "layers": [str(x) for x in data.get("layers", [])],
            "notes": [str(x) for x in data.get("notes", [])],
            "module_dependencies": edges,
        },
    )


def _module(facts: ExtractedFacts, module: str, files: list[FileFacts], cost: _Cost) -> KnowledgeDocument:
    system = (
        "You are a software analyst. Given one module's files, classes and functions, "
        "write a concise summary of what the module does. Respond ONLY as JSON: "
        '{"summary": string, "tags": [string], "related": [string]}. '
        "Base it only on the facts."
    )
    symbols = sorted({s for f in files for s in (f.classes + f.functions)})
    location = files[0].path.rsplit("/", 1)[0] if "/" in files[0].path else "."
    deps = facts_view.module_dependencies(facts, module)
    detail = facts_view.render_facts(ExtractedFacts(root=facts.root, files=files))
    data = _gen(system, f"Module '{module}':\n\n{detail}", cost)
    return KnowledgeDocument(
        id=_slug(module), type="module", name=module.replace("_", " ").title(),
        summary=data.get("summary", ""), tags=_tags(data) or [module],
        related=[str(r) for r in data.get("related", [])] or deps,
        content={"location": location, "files": [f.path for f in files],
                 "symbols": symbols[:60], "dependencies": deps},
    )


def _list_view(facts: ExtractedFacts, cost: _Cost, *, doc_type: str, system: str,
               key: str, extra_facts: str = "") -> list[KnowledgeDocument]:
    """Shared driver for the multi-item views (features, workflows, …)."""
    user = facts_view.render_facts(facts)
    if extra_facts:
        user += f"\n\n{extra_facts}"
    data = _gen(system, user, cost)
    docs: list[KnowledgeDocument] = []
    for item in _list(data, key, "items"):
        content: dict = {"description": item.get("description", ""), "modules": _mods(item)}
        for f in ("kind", "steps", "endpoints"):
            if f in item and item[f]:
                content[f] = item[f] if isinstance(item[f], list) or f == "kind" else []
        docs.append(KnowledgeDocument(
            id=_slug(item["name"]), type=doc_type, name=str(item["name"]),
            summary=item.get("summary", ""), tags=_tags(item),
            related=[str(r) for r in item.get("related", [])] or [_slug(m) for m in _mods(item)],
            content=content,
        ))
    return docs


_FEATURE_SYS = (
    "You are a product analyst. From the repository's modules, classes, functions "
    "and endpoints, identify the user-facing features it supports. Respond ONLY as "
    'JSON: {"features": [{"name": string, "summary": string, "description": string, '
    '"modules": [string], "endpoints": [string], "tags": [string]}]}. Only infer '
    "features the facts support."
)
_WORKFLOW_SYS = (
    "You are a product analyst. From the repository's modules, endpoints and "
    "dependencies, identify the key end-to-end workflows (sequences of steps across "
    'modules). Respond ONLY as JSON: {"workflows": [{"name": string, "summary": '
    'string, "description": string, "steps": [string], "modules": [string], "tags": '
    '[string]}]}. Only infer workflows the facts support.'
)
_ENTRYPOINT_SYS = (
    "You are a software analyst. From the repository's endpoints, modules and "
    "functions, identify the application's entry points (HTTP routes, CLI commands, "
    'workers, scheduled jobs). Respond ONLY as JSON: {"entrypoints": [{"name": '
    'string, "kind": string, "summary": string, "description": string, "modules": '
    '[string], "endpoints": [string], "tags": [string]}]}. Only infer entry points '
    "the facts support."
)
_DOMAIN_SYS = (
    "You are a domain-modeling analyst. From the repository's classes, modules and "
    "functions, identify the core business domain concepts (entities) it models. "
    'Respond ONLY as JSON: {"concepts": [{"name": string, "summary": string, '
    '"description": string, "modules": [string], "tags": [string]}]}. Only infer '
    "concepts the facts support."
)
_RULE_SYS = (
    "You are a business analyst. From the repository's features, workflows, "
    "endpoints and modules, infer the business rules and constraints the system "
    'likely enforces. Respond ONLY as JSON: {"rules": [{"name": string, "summary": '
    'string, "description": string, "modules": [string], "tags": [string]}]}. Only '
    "infer rules the facts reasonably support; do not invent specific numbers."
)
_INTEGRATION_SYS = (
    "You are a software analyst. From the repository's imports and modules, identify "
    "the external systems it integrates with (databases, queues, caches, cloud "
    'services, third-party APIs). Respond ONLY as JSON: {"integrations": [{"name": '
    'string, "kind": string, "summary": string, "description": string, "modules": '
    '[string], "tags": [string]}]}. Base it on the facts, especially the imports.'
)


def _ensure_unique_ids(docs: list[KnowledgeDocument]) -> None:
    seen: set[str] = set()
    for doc in docs:
        if doc.id in seen:
            n = 2
            while f"{doc.id}_{n}" in seen:
                n += 1
            doc.id = f"{doc.id}_{n}"
        seen.add(doc.id)


def generate_knowledge(facts: ExtractedFacts, *, max_modules: int = 25,
                       progress=None) -> tuple[list[KnowledgeDocument], dict]:
    """Run every generator over `facts`; return (documents, cost dict).

    `progress(step, pct)` is called between phases so ingest can surface it.
    """
    cost = _Cost()
    docs: list[KnowledgeDocument] = []

    def step(msg: str, pct: int) -> None:
        logger.info("knowledge.generate: %s", msg)
        if progress:
            progress(msg, pct)

    step("Analyzing repository overview…", 55)
    docs.append(_repository(facts, cost))

    step("Mapping architecture…", 60)
    docs.append(_architecture(facts, cost))

    modules = facts_view.group_modules(facts)
    # Cost guard: describe the largest modules first, cap the count.
    ranked = sorted(modules.items(), key=lambda kv: -len(kv[1]))[:max_modules]
    for i, (module, files) in enumerate(ranked):
        step(f"Describing modules ({i + 1}/{len(ranked)})…", 62 + int(18 * i / max(1, len(ranked))))
        docs.append(_module(facts, module, files, cost))

    step("Identifying features…", 82)
    docs.extend(_list_view(facts, cost, doc_type="feature", system=_FEATURE_SYS, key="features"))

    step("Tracing workflows…", 86)
    docs.extend(_list_view(facts, cost, doc_type="workflow", system=_WORKFLOW_SYS, key="workflows"))

    step("Finding entry points…", 89)
    docs.extend(_list_view(facts, cost, doc_type="entrypoint", system=_ENTRYPOINT_SYS, key="entrypoints"))

    step("Modeling domain concepts…", 92)
    docs.extend(_list_view(facts, cost, doc_type="domain", system=_DOMAIN_SYS, key="concepts"))

    step("Inferring business rules…", 94)
    docs.extend(_list_view(facts, cost, doc_type="business_rule", system=_RULE_SYS, key="rules"))

    step("Detecting integrations…", 96)
    docs.extend(_list_view(
        facts, cost, doc_type="integration", system=_INTEGRATION_SYS, key="integrations",
        extra_facts=f"Imports:\n{facts_view.render_imports(facts)}",
    ))

    _ensure_unique_ids(docs)
    for doc in docs:
        doc.confidence = _CONFIDENCE_BY_TYPE.get(doc.type, "MEDIUM")

    return docs, {"tokens_in": cost.tokens_in, "tokens_out": cost.tokens_out,
                  "cost": round(cost.cost, 6)}

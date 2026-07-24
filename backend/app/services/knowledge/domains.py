"""Knowledge domains — the categories the retrieval index is split by.

A "domain" is one Qdrant collection (per repo). Every knowledge document maps to
exactly one domain via its `type`. Keeping the mapping in one place means the
generator, indexer and retriever all agree on the same names.
"""

# All retrievable domains, in a stable, human-meaningful order.
DOMAINS = [
    "repository",
    "architecture",
    "modules",
    "features",
    "workflows",
    "entrypoints",
    "domain",
    "business_rules",
    "integrations",
    "deliveries",
]

# The minimal baseline pulled first to give any query a repo-shaped anchor.
BOOTSTRAP_DOMAINS = ["repository", "architecture", "modules"]

# A document's `type` -> the domain (collection) it belongs to.
_DOC_TYPE_TO_DOMAIN = {
    "repository": "repository",
    "architecture": "architecture",
    "module": "modules",
    "feature": "features",
    "workflow": "workflows",
    "entrypoint": "entrypoints",
    "domain": "domain",
    "business_rule": "business_rules",
    "integration": "integrations",
    "delivery_note": "deliveries",
    # Consolidated per-module lessons live in the modules collection so a module
    # query surfaces its distilled cross-run learnings next to the module doc.
    "lesson": "modules",
}

# Human labels for the UI.
DOMAIN_LABELS = {
    "repository": "Repository",
    "architecture": "Architecture",
    "modules": "Modules",
    "features": "Features",
    "workflows": "Workflows",
    "entrypoints": "Entry points",
    "domain": "Domain concepts",
    "business_rules": "Business rules",
    "integrations": "Integrations",
    "deliveries": "Delivery notes",
}


def domain_of(doc_type: str) -> str:
    """Return the domain (collection) a document type belongs to."""
    return _DOC_TYPE_TO_DOMAIN.get(doc_type, "repository")

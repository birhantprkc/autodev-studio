"""Turn raw facts into the structured inputs the generators need.

Deterministic, code-only logic (no LLM): grouping files into modules, building
the module dependency graph, and rendering a compact text summary handed to the
model. Keeping derivation here means prompts stay free of business logic and the
model can't invent factual fields.
"""

from __future__ import annotations

from ...config import settings
from .facts import ExtractedFacts, FileFacts


def _parts(relative_path: str) -> list[str]:
    parts = relative_path.replace("\\", "/").split("/")
    if parts and parts[0] in ("src", "lib"):
        parts = parts[1:]
    return parts


def module_of(relative_path: str, known: set[str] | None = None) -> str:
    """Group a file into a module by its top-level package directory.

    src/orders/service.py -> 'orders'   (a leading 'src' is ignored)
    main.py               -> 'root'

    With `known` (the module names group_modules actually produced), a file
    inside a split submodule maps to it: httpie/cli/definition.py ->
    'httpie/cli' when 'httpie/cli' is a known module. Callers that need the
    file→module-doc mapping (freshness, retrieval) must pass `known`;
    without it this returns the coarse top-level name.
    """
    parts = _parts(relative_path)
    if len(parts) <= 1:
        return "root"
    if known and len(parts) > 2 and f"{parts[0]}/{parts[1]}" in known:
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def group_modules(facts: ExtractedFacts) -> dict[str, list[FileFacts]]:
    """Top-level grouping, with oversized packages split one directory deeper.

    A monolithic package (e.g. httpie/ holding 60+ analyzable files) otherwise
    collapses into ONE module doc — too coarse for retrieval to localize
    anything inside it (measured: 'add a --timeout flag' surfaced nothing
    about httpie/cli). Files sitting directly in the package root keep the
    package name, so the parent doc survives the split."""
    coarse: dict[str, list[FileFacts]] = {}
    for file in facts.files:
        coarse.setdefault(module_of(file.path), []).append(file)
    out: dict[str, list[FileFacts]] = {}
    for module, files in coarse.items():
        if module == "root" or len(files) <= settings.kb_module_split_files:
            out[module] = files
            continue
        sub: dict[str, list[FileFacts]] = {}
        for f in files:
            parts = _parts(f.path)
            name = f"{parts[0]}/{parts[1]}" if len(parts) > 2 else module
            sub.setdefault(name, []).append(f)
        if len(sub) > 1:  # actually has subpackages — split
            out.update(sub)
        else:
            out[module] = files
    return out


def dependency_edges(facts: ExtractedFacts) -> list[tuple[str, str]]:
    """Edge (A, B) means a file in module A imports something naming module B."""
    modules = group_modules(facts)
    names = set(modules)
    edges: set[tuple[str, str]] = set()
    for module, files in modules.items():
        for file in files:
            for imp in file.imports:
                for other in names:
                    # Split submodules are path-shaped (httpie/cli) but imports
                    # are dotted (httpie.cli.definition) — match either form.
                    if other != module and (other in imp or other.replace("/", ".") in imp):
                        edges.add((module, other))
    return sorted(edges)


def module_dependencies(facts: ExtractedFacts, module: str) -> list[str]:
    return sorted({b for a, b in dependency_edges(facts) if a == module})


def render_imports(facts: ExtractedFacts) -> str:
    """Unique import lines across the repository (signal for integrations)."""
    seen: list[str] = []
    for file in facts.files:
        for imp in file.imports:
            if imp not in seen:
                seen.append(imp)
    return "\n".join(seen[:400])


def render_facts(facts: ExtractedFacts, max_files_per_module: int = 40) -> str:
    """Compact, deterministic text summary of the whole repository."""
    modules = group_modules(facts)
    edges = dependency_edges(facts)
    deps_by_module: dict[str, list[str]] = {}
    for a, b in edges:
        deps_by_module.setdefault(a, []).append(b)

    lines = [f"Repository with {len(facts.files)} files in {len(modules)} modules.\n"]
    for module, files in sorted(modules.items()):
        lines.append(f"Module '{module}':")
        for file in files[:max_files_per_module]:
            lines.append(f"  file {file.path}")
            if file.classes:
                lines.append(f"    classes: {', '.join(file.classes[:30])}")
            if file.functions:
                lines.append(f"    functions: {', '.join(file.functions[:30])}")
            for ep in file.endpoints:
                lines.append(f"    endpoint: {ep.method} {ep.path}")
        if len(files) > max_files_per_module:
            lines.append(f"  … and {len(files) - max_files_per_module} more files")
        deps = sorted(set(deps_by_module.get(module, [])))
        if deps:
            lines.append(f"  depends on: {', '.join(deps)}")
        lines.append("")
    return "\n".join(lines)

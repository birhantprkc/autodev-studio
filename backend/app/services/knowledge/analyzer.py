"""Walks a cloned repository and extracts raw facts (never knowledge).

Python files are parsed with the stdlib `ast` (top-level classes, functions,
imports) plus a small regex pass for HTTP endpoints (FastAPI/Flask). Files in
other languages are still recorded (path + language) so the module/architecture
views can reason about the whole tree; only the symbol extraction is Python-aware
today. Output is `ExtractedFacts` — facts only.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

from .. import git_ops
from .facts import Endpoint, ExtractedFacts, FileFacts

logger = logging.getLogger(__name__)

# Directories we never descend into.
_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".next", ".turbo", "vendor", "target", "coverage", ".pytest_cache", ".qdrant",
}

# Extensions we record as files (symbol extraction is Python-only for now).
_LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".java": "Java",
    ".rb": "Ruby", ".rs": "Rust", ".php": "PHP", ".c": "C", ".cc": "C++",
    ".cpp": "C++", ".h": "C", ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift",
    ".scala": "Scala", ".vue": "Vue", ".svelte": "Svelte",
}
_MAX_FILE_BYTES = 300_000

# @app.get("/path"), @router.post('/path'), @app.route("/path", ...)
_ENDPOINT_RE = re.compile(
    r"""@\w+\.(get|post|put|patch|delete|route)\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_METHODS_RE = re.compile(r"methods\s*=\s*\[([^\]]*)\]", re.IGNORECASE)


def analyze_repo(repo_url: str) -> ExtractedFacts:
    """Extract facts from the already-cloned working copy for `repo_url`."""
    root = git_ops.workdir(repo_url)
    facts = ExtractedFacts(root=str(root))
    if not (root / ".git").exists():
        return facts

    for rel in _iter_files(repo_url, root):
        ext = Path(rel).suffix.lower()
        language = _LANG_BY_EXT.get(ext, "")
        fp = root / rel
        try:
            if fp.stat().st_size > _MAX_FILE_BYTES:
                continue
            source = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ext == ".py":
            facts.files.append(_analyze_python(rel, source))
        else:
            facts.files.append(FileFacts(path=rel, language=language or "Other"))

    logger.info("knowledge.analyzer: %s — %d files", repo_url, len(facts.files))
    return facts


def _iter_files(repo_url: str, root: Path) -> list[str]:
    try:
        tracked = git_ops.list_files(str(root), limit=5000)
    except Exception:  # noqa: BLE001
        tracked = []
    out: list[str] = []
    for rel in tracked:
        p = Path(rel)
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _LANG_BY_EXT:
            continue
        out.append(rel)
    return out


def _analyze_python(rel: str, source: str) -> FileFacts:
    classes: list[str] = []
    functions: list[str] = []
    imports: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return FileFacts(path=rel, language="Python")

    for node in tree.body:  # module-level only → top-level defs
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.Import):
            imports.append("import " + ", ".join(a.name for a in node.names))
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * (node.level or 0)) + (node.module or "")
            imports.append(f"from {mod} import " + ", ".join(a.name for a in node.names))

    return FileFacts(
        path=rel,
        language="Python",
        classes=classes,
        functions=functions,
        imports=imports,
        endpoints=_endpoints(rel, source),
    )


def extract_symbols(source: str) -> list[dict]:
    """Line-numbered symbol facts for one Python file — the raw material of the
    localization symbol map. Unlike `_analyze_python` (names only, for the LLM
    views), this keeps line numbers and descends into class bodies for methods:
    [{"n": name, "k": class|function|method, "l": line, "p": parent_class}].
    Returns [] for unparseable source."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    symbols: list[dict] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append({"n": node.name, "k": "class", "l": node.lineno})
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append({"n": child.name, "k": "method",
                                    "l": child.lineno, "p": node.name})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append({"n": node.name, "k": "function", "l": node.lineno})
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and not tgt.id.startswith("_"):
                    symbols.append({"n": tgt.id, "k": "const", "l": node.lineno})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if not node.target.id.startswith("_"):
                symbols.append({"n": node.target.id, "k": "const", "l": node.lineno})
    return symbols


def analyzable(rel: str) -> bool:
    """Whether a repo-relative path is one the analyzer would record at all."""
    p = Path(rel)
    if any(part in _SKIP_DIRS for part in p.parts):
        return False
    return p.suffix.lower() in _LANG_BY_EXT


def language_of(rel: str) -> str:
    return _LANG_BY_EXT.get(Path(rel).suffix.lower(), "Other")


def _endpoints(rel: str, source: str) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    for line in source.splitlines():
        m = _ENDPOINT_RE.search(line)
        if not m:
            continue
        verb, path = m.group(1).lower(), m.group(2)
        if verb == "route":  # Flask: methods=[...], default GET
            mm = _METHODS_RE.search(line)
            methods = re.findall(r"['\"](\w+)['\"]", mm.group(1)) if mm else ["GET"]
            for method in methods:
                endpoints.append(Endpoint(method=method.upper(), path=path, file=rel))
        else:
            endpoints.append(Endpoint(method=verb.upper(), path=path, file=rel))
    return endpoints

"""Language registry — every language-specific decision in one place.

The pipeline itself is language-agnostic (localize → edit → verify → iterate);
what varies by language is only:

  * symbol extraction  → `extract_symbols(rel, source)`  (feeds the symbol map)
  * import extraction  → `extract_imports(rel, source)`  (feeds the dep graph)
  * the edit-time parse gate → `syntax_error(rel, content)`
  * what counts as a test file → `is_test_file(rel)`
  * which test ecosystem a repo uses → `project_kind(root)` + `RUNNERS`

Design rule: FAIL OPEN. An unknown language still gets its files recorded,
edits applied ungated, and tests reported as "couldn't run" (None) rather than
FAIL — the QA agent already treats INCONCLUSIVE as not-a-pass, so degrading
gracefully never fabricates a green result.

Python uses the stdlib `ast` (exact); other languages use line-anchored regex
extractors with a naive brace/indent tracker for methods. Deliberately no
tree-sitter: the extra fidelity isn't worth the native-lib memory/dependency
cost for what is an index, not a compiler (this box has ~5.6 GiB RAM).
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Language identification
# ---------------------------------------------------------------------------

LANG_BY_EXT = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".go": "Go", ".java": "Java",
    ".rb": "Ruby", ".rs": "Rust", ".php": "PHP", ".c": "C", ".cc": "C++",
    ".cpp": "C++", ".h": "C", ".cs": "C#", ".kt": "Kotlin", ".swift": "Swift",
    ".scala": "Scala", ".vue": "Vue", ".svelte": "Svelte",
}

# Directories no analyzer pass ever descends into.
SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".next", ".turbo", "vendor", "target", "coverage", ".pytest_cache", ".qdrant",
    ".agent-venv",
}


def language_of(rel: str) -> str:
    return LANG_BY_EXT.get(Path(rel).suffix.lower(), "Other")


def _ext(rel: str) -> str:
    return Path(rel).suffix.lower()


# ---------------------------------------------------------------------------
# Test-file detection (path-shaped only — cheap, used in hot loops)
# ---------------------------------------------------------------------------

_TEST_PATH_RES = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.(py|go)$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
    re.compile(r"(^|/)src/test/"),            # Maven/Gradle layout
    re.compile(r"Test\.java$"),
    re.compile(r"_spec\.rb$"),
)


def is_test_file(rel: str) -> bool:
    p = rel.replace("\\", "/")
    return any(r.search(p) for r in _TEST_PATH_RES)


# ---------------------------------------------------------------------------
# Symbol extraction → [{"n": name, "k": class|function|method|const, "l": line,
#                       "p": parent}] — the raw material of the symbol map.
# ---------------------------------------------------------------------------

_MAX_SYMBOLS = 400  # per file — bounds symbols.json on generated/minified files


def extract_symbols(rel: str, source: str) -> list[dict]:
    """Line-numbered symbol facts for one file; [] when the language has no
    extractor or the source doesn't parse. Dispatches on extension."""
    fn = _SYMBOL_EXTRACTORS.get(_ext(rel))
    if fn is None:
        return []
    try:
        return fn(source)[:_MAX_SYMBOLS]
    except Exception:  # noqa: BLE001 — an index must never take the pipeline down
        return []


def _python_symbols(source: str) -> list[dict]:
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


def _brace_delta(line: str) -> int:
    """Net {…} depth change of a line, ignoring braces inside quotes and after
    line comments. Naive (no multi-line strings) — good enough for an index."""
    depth = 0
    quote: str | None = None
    prev = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                prev = ""
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "/" and prev == "/":
            break
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        prev = ch
        i += 1
    return depth


_ID = r"[A-Za-z_$][\w$]*"
_JS_CLASS = re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+({_ID})")
_JS_FUNC = re.compile(rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*({_ID})")
_JS_ARROW = re.compile(
    rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_ID})\s*(?::[^=\n]+)?=\s*"
    rf"(?:async\s+)?(?:\([^)]*\)|{_ID})\s*=>")
_JS_CONST = re.compile(rf"^\s*export\s+(?:const|let|var)\s+({_ID})"
                       r"|^(?:const|let|var)\s+([A-Z_][A-Z0-9_]*)\s*=")
_TS_TYPE = re.compile(rf"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|enum)\s+({_ID})"
                      rf"|^\s*(?:export\s+)?type\s+({_ID})\s*=")
_JS_METHOD = re.compile(
    rf"^\s+(?:(?:public|private|protected|static|readonly|override|async|get|set)\s+|\*\s*)*"
    rf"({_ID})\s*(?:<[^>\n]*>)?\([^;]*$|"
    rf"^\s+(?:(?:public|private|protected|static|readonly|override|async|get|set)\s+|\*\s*)*"
    rf"({_ID})\s*(?:<[^>\n]*>)?\([^;{{]*\)\s*(?::[^;{{\n]+)?\s*\{{")
_JS_NOT_METHOD = {"if", "for", "while", "switch", "catch", "return", "function",
                  "else", "do", "try", "new", "await", "typeof", "super", "import"}


def _js_symbols(source: str) -> list[dict]:
    symbols: list[dict] = []
    parent: str | None = None
    parent_close = 0  # depth at which the current class body closes
    depth = 0
    for i, line in enumerate(source.splitlines(), start=1):
        if m := _JS_CLASS.match(line):
            symbols.append({"n": m.group(1), "k": "class", "l": i})
            parent, parent_close = m.group(1), depth
        elif m := _JS_FUNC.match(line):
            symbols.append({"n": m.group(1), "k": "function", "l": i})
        elif m := _JS_ARROW.match(line):
            if parent is None or depth == 0:
                symbols.append({"n": m.group(1), "k": "function", "l": i})
        elif m := _TS_TYPE.match(line):
            symbols.append({"n": m.group(1) or m.group(2), "k": "class", "l": i})
        elif m := _JS_CONST.match(line):
            name = m.group(1) or m.group(2)
            if depth == 0:
                symbols.append({"n": name, "k": "const", "l": i})
        elif parent and depth == parent_close + 1 and (m := _JS_METHOD.match(line)):
            name = m.group(1) or m.group(2)
            if name and name not in _JS_NOT_METHOD:
                symbols.append({"n": name, "k": "method", "l": i, "p": parent})
        depth += _brace_delta(line)
        if parent is not None and depth <= parent_close:
            parent = None
    return symbols


_GO_FUNC = re.compile(r"^func\s+(?:\(\s*\w+\s+\*?(\w+)\s*\)\s+)?([A-Za-z_]\w*)")
_GO_TYPE = re.compile(r"^type\s+([A-Za-z_]\w*)\b")
_GO_VAR = re.compile(r"^(?:var|const)\s+([A-Za-z_]\w*)\s")


def _go_symbols(source: str) -> list[dict]:
    symbols: list[dict] = []
    for i, line in enumerate(source.splitlines(), start=1):
        if m := _GO_FUNC.match(line):
            recv, name = m.group(1), m.group(2)
            if recv:
                symbols.append({"n": name, "k": "method", "l": i, "p": recv})
            else:
                symbols.append({"n": name, "k": "function", "l": i})
        elif m := _GO_TYPE.match(line):
            symbols.append({"n": m.group(1), "k": "class", "l": i})
        elif m := _GO_VAR.match(line):
            symbols.append({"n": m.group(1), "k": "const", "l": i})
    return symbols


_RS_VIS = r"(?:pub(?:\([^)]*\))?\s+)?"
_RS_IMPL = re.compile(r"^impl(?:<[^>]*>)?\s+(?:[\w:<>, ]+\s+for\s+)?([\w]+)")
_RS_FN = re.compile(rf"^(\s*){_RS_VIS}(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_]\w*)")
_RS_TYPE = re.compile(rf"^{_RS_VIS}(?:struct|enum|trait|union)\s+([A-Za-z_]\w*)")
_RS_CONST = re.compile(rf"^{_RS_VIS}(?:const|static)\s+([A-Za-z_]\w*)")


def _rust_symbols(source: str) -> list[dict]:
    symbols: list[dict] = []
    parent: str | None = None
    parent_close = 0
    depth = 0
    for i, line in enumerate(source.splitlines(), start=1):
        if m := _RS_IMPL.match(line):
            parent, parent_close = m.group(1), depth
        elif m := _RS_TYPE.match(line):
            symbols.append({"n": m.group(1), "k": "class", "l": i})
        elif m := _RS_CONST.match(line):
            symbols.append({"n": m.group(1), "k": "const", "l": i})
        elif m := _RS_FN.match(line):
            if parent and m.group(1):
                symbols.append({"n": m.group(2), "k": "method", "l": i, "p": parent})
            else:
                symbols.append({"n": m.group(2), "k": "function", "l": i})
        depth += _brace_delta(line)
        if parent is not None and depth <= parent_close:
            parent = None
    return symbols


_JAVA_TYPE = re.compile(
    r"^\s*(?:(?:public|protected|private|final|abstract|static|sealed)\s+)*"
    r"(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")
_JAVA_METHOD = re.compile(
    r"^\s+(?:(?:public|protected|private|static|final|synchronized|abstract|native|default)\s+)+"
    r"[\w<>\[\],.?\s]+?\s+(\w+)\s*\([^;]*$"
    r"|^\s+(?:(?:public|protected|private|static|final|synchronized|abstract|native|default)\s+)+"
    r"[\w<>\[\],.?\s]+?\s+(\w+)\s*\(")


def _java_symbols(source: str) -> list[dict]:
    symbols: list[dict] = []
    parent: str | None = None
    for i, line in enumerate(source.splitlines(), start=1):
        if m := _JAVA_TYPE.match(line):
            symbols.append({"n": m.group(1), "k": "class", "l": i})
            if parent is None:  # methods attach to the file's outermost type
                parent = m.group(1)
        elif parent and (m := _JAVA_METHOD.match(line)):
            name = m.group(1) or m.group(2)
            if name != parent:  # constructors already read as the class itself
                symbols.append({"n": name, "k": "method", "l": i, "p": parent})
    return symbols


_RB_CLASS = re.compile(r"^\s*(?:class|module)\s+([A-Z][\w:]*)")
_RB_DEF = re.compile(r"^(\s*)def\s+(?:self\.)?([\w]+[?!]?)")


def _ruby_symbols(source: str) -> list[dict]:
    symbols: list[dict] = []
    parent: str | None = None
    for i, line in enumerate(source.splitlines(), start=1):
        if m := _RB_CLASS.match(line):
            symbols.append({"n": m.group(1), "k": "class", "l": i})
            parent = m.group(1)
        elif m := _RB_DEF.match(line):
            if m.group(1) and parent:
                symbols.append({"n": m.group(2), "k": "method", "l": i, "p": parent})
            else:
                symbols.append({"n": m.group(2), "k": "function", "l": i})
    return symbols


_SYMBOL_EXTRACTORS = {
    ".py": _python_symbols,
    ".js": _js_symbols, ".jsx": _js_symbols, ".mjs": _js_symbols,
    ".cjs": _js_symbols, ".ts": _js_symbols, ".tsx": _js_symbols,
    ".go": _go_symbols,
    ".rs": _rust_symbols,
    ".java": _java_symbols,
    ".rb": _ruby_symbols,
}


# ---------------------------------------------------------------------------
# Import extraction (feeds the module dependency graph)
# ---------------------------------------------------------------------------

_PY_HANDLED_SEPARATELY = object()  # analyzer keeps its exact ast-based pass

_JS_IMPORT = re.compile(r"""(?:^\s*import\b[^'"]*|\brequire\s*\(\s*|^\s*export\b[^'"]*\bfrom\s*)["']([^"']+)["']""")
_GO_IMPORT = re.compile(r'^\s*(?:import\s+)?(?:\w+\s+)?"([\w./-]+)"')
_RS_USE = re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)")
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)")
_RB_REQUIRE = re.compile(r"""^\s*require(?:_relative)?\s+["']([^"']+)["']""")


def extract_imports(rel: str, source: str) -> list[str]:
    """Imported module/path strings for non-Python files (Python keeps its exact
    ast pass in the analyzer). Used only for the module dependency graph, so a
    plain string per import is enough."""
    ext = _ext(rel)
    if ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"):
        pat = _JS_IMPORT
    elif ext == ".go":
        out: list[str] = []
        in_block = False
        for line in source.splitlines():
            s = line.strip()
            if s.startswith("import ("):
                in_block = True
                continue
            if in_block and s.startswith(")"):
                in_block = False
                continue
            if (in_block or s.startswith("import")) and (m := _GO_IMPORT.match(line)):
                out.append(m.group(1))
        return out[:100]
    elif ext == ".rs":
        pat = _RS_USE
    elif ext == ".java":
        pat = _JAVA_IMPORT
    elif ext == ".rb":
        pat = _RB_REQUIRE
    else:
        return []
    return [m.group(1) for line in source.splitlines() if (m := pat.search(line))][:100]


# ---------------------------------------------------------------------------
# Edit-time parse gate (Dev loop rejects edits whose result no longer parses)
# ---------------------------------------------------------------------------

def syntax_error(rel: str, content: str) -> str | None:
    """Error string when `content` no longer parses; None = fine OR ungateable
    (no checker for this language / toolchain missing — fail open, the test run
    still catches real breakage later)."""
    ext = _ext(rel)
    if ext == ".py":
        try:
            ast.parse(content)
            return None
        except SyntaxError as exc:
            return f"line {exc.lineno}: {exc.msg}"
    if ext in (".js", ".mjs", ".cjs") and shutil.which("node"):
        return _node_check(content, ext)
    if ext == ".go" and shutil.which("gofmt"):
        try:
            p = subprocess.run(["gofmt", "-e"], input=content, capture_output=True,
                               text=True, timeout=15)
            if p.returncode != 0:
                first = (p.stderr or "").strip().splitlines()
                return re.sub(r"^<standard input>:", "line ", first[0])[:200] if first else "does not parse"
        except Exception:  # noqa: BLE001 — gate is best-effort
            return None
        return None
    return None  # TS/Rust/Java/…: no cheap ambient checker — fail open


def _node_check(content: str, ext: str) -> str | None:
    """Parse-check JS via node reading stdin. A bare `.js` file may be either
    dialect (package.json "type" decides, and we don't know it here), so it is
    checked as CommonJS AND as ESM and rejected only when BOTH fail — valid in
    either dialect passes. NB: `node --check <file.js>` alone silently exits 0
    on ESM sources, which is why the explicit --input-type runs are required."""
    modes = {".cjs": ("commonjs",), ".mjs": ("module",)}.get(ext, ("commonjs", "module"))
    last_err = "does not parse"
    for mode in modes:
        try:
            p = subprocess.run(["node", f"--input-type={mode}", "--check"],
                               input=content, capture_output=True, text=True, timeout=15)
        except Exception:  # noqa: BLE001 — gate is best-effort
            return None
        if p.returncode == 0:
            return None
        line = next((ln.strip() for ln in (p.stderr or "").splitlines()
                     if "Error" in ln), last_err)
        last_err = line[:200]
    return last_err


# ---------------------------------------------------------------------------
# Test ecosystems — detection + per-runner facts. Subprocess execution stays in
# git_ops; this layer only says WHAT to run and how to read the output.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Runner:
    kind: str                       # python | node | go | cargo
    label: str                      # command string advertised to the Dev agent
    cmd: list[str]                  # full-suite command (python filled by caller)
    fail_re: re.Pattern | None      # per-test failure ids; None = can't baseline
    accepts_paths: bool = True      # can targeted test files be appended?
    setup: str = ""                 # env note for git_ops.ensure_test_env
    no_tests_markers: tuple[str, ...] = field(default_factory=tuple)


_PYTEST = Runner(
    kind="python", label="pytest -q", cmd=["-m", "pytest", "-q"],
    fail_re=re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE), setup="venv")
_NODE = Runner(
    kind="node", label="npm test --silent", cmd=["npm", "test", "--silent"],
    fail_re=None, accepts_paths=False, setup="npm",
    no_tests_markers=("missing script: \"test\"", "missing script: test",
                      "no test specified"))
_GO = Runner(
    kind="go", label="go test ./...", cmd=["go", "test", "./..."],
    fail_re=re.compile(r"^--- FAIL: (\S+)", re.MULTILINE),
    no_tests_markers=("no test files",))
_CARGO = Runner(
    kind="cargo", label="cargo test", cmd=["cargo", "test"],
    fail_re=re.compile(r"^test (\S+) \.\.\. FAILED", re.MULTILINE),
    accepts_paths=False)


_RUNNERS_BY_KIND = {r.kind: r for r in (_PYTEST, _NODE, _GO, _CARGO)}


def detect_runner_by_kind(kind: str) -> Runner | None:
    return _RUNNERS_BY_KIND.get(kind)


def detect_runner(root: Path) -> Runner | None:
    """Which test ecosystem this repo uses. Python first (preserves existing
    behavior on mixed repos, e.g. a Go tool with a docs/ node project); then by
    manifest. None = unknown → tests report as inconclusive, never FAIL."""
    if ((root / "pyproject.toml").exists() or (root / "setup.py").exists()
            or (root / "setup.cfg").exists() or (root / "pytest.ini").exists()
            or (root / "tox.ini").exists() or list(root.glob("test_*.py"))
            or ((root / "tests").exists() and _dir_has(root, "*.py"))):
        return _PYTEST
    if (root / "go.mod").exists() and shutil.which("go"):
        return _GO
    if (root / "Cargo.toml").exists() and shutil.which("cargo"):
        return _CARGO
    if (root / "package.json").exists() and shutil.which("npm"):
        return _NODE
    return None


def _dir_has(root: Path, pattern: str) -> bool:
    try:
        next((root / "tests").rglob(pattern))
        return True
    except (StopIteration, OSError):
        return False


def parse_failures(runner: Runner, output: str) -> set[str] | None:
    """Stable per-test failure ids from a full-suite run, for baseline diffing
    (pre-existing failures vs regressions). None = this runner's output has no
    reliably parseable ids — callers must skip baselining, not assume clean."""
    if runner.fail_re is None:
        return None
    return set(runner.fail_re.findall(output))


def no_tests_found(runner: Runner, returncode: int, output: str) -> bool:
    """True when the run means 'nothing to run' rather than pass/fail."""
    if runner.kind == "python" and returncode == 5:
        return True
    low = output.lower()
    return bool(runner.no_tests_markers) and any(m in low for m in runner.no_tests_markers)

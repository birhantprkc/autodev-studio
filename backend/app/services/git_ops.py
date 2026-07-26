"""Git + gh operations on cloned working copies the agents edit."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from ..config import settings
from . import lang

_GITHUB_API = "https://api.github.com"

# One working copy is SHARED per repo, so two scopes that run it concurrently
# race on branch/commit state (reset --hard under a live run destroys work — this
# actually corrupted a benchmark run). A per-repo lock serializes same-repo work
# while still letting DIFFERENT repos run in parallel. Keyed by clone slug.
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


@contextmanager
def repo_lock(repo_url: str):
    """Hold the exclusive lock for this repo's shared working copy. Wrap any
    checkout→edit→commit→diff→PR sequence in it so concurrent scopes serialize
    instead of clobbering each other's branch state."""
    key = slug(repo_url)
    with _repo_locks_guard:
        lock = _repo_locks.setdefault(key, threading.Lock())
    with lock:
        yield


def _run(args: list[str], cwd: str | None = None, timeout: int = 600, check: bool = True,
         env: dict | None = None) -> str:
    run_env = {**os.environ, **env} if env else None
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=run_env)
    if check and p.returncode != 0:
        raise RuntimeError(f"`{' '.join(args)}` failed: {(p.stderr or p.stdout).strip()[:400]}")
    return p.stdout


def _identity_args() -> list[str]:
    """`git -c` flags stamping the agent as commit author/committer, so delivery
    history is attributed to the agent rather than the server's git config."""
    return ["-c", f"user.name={settings.agent_git_name}",
            "-c", f"user.email={settings.agent_git_email}"]


def _token_env(token: str | None) -> dict | None:
    """gh env for a specific token — `gh` (and its git credential helper) then
    authenticate as that account, so pushes and PRs are BY it. Falls back to the
    shared bot token when none is passed; None means 'use the host's gh login'."""
    tok = (token or settings.github_bot_token or "").strip()
    return {"GH_TOKEN": tok, "GITHUB_TOKEN": tok} if tok else None


def _gh_api(method: str, path: str, token: str | None, body: dict | None = None,
           timeout: int = 15) -> tuple[int, dict]:
    """Bare GitHub REST call (stdlib urllib, no `gh` dependency). Returns
    (status_code, json_body_or_{}). Raises only on network failure, never on
    non-2xx — callers read the status themselves."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AutoDev-Studio"}
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{_GITHUB_API}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {}


def github_identity(token: str) -> dict:
    """Validate a GitHub token by fetching the authenticated user. Returns
    {login, name, id, email} or raises ValueError with a readable reason."""
    try:
        status, u = _gh_api("GET", "/user", token)
    except Exception as e:  # noqa: BLE001 — network/DNS/timeout
        raise ValueError(f"Could not reach GitHub: {str(e)[:120]}")
    if status in (401, 403):
        raise ValueError("GitHub rejected this token (invalid, expired, or missing 'repo' scope).")
    if status >= 400:
        raise ValueError(f"GitHub API error ({status}).")
    login = u.get("login")
    if not login:
        raise ValueError("GitHub returned no account for this token.")
    # noreply email keeps the commit author unlinked from a private address.
    email = u.get("email") or f"{u.get('id')}+{login}@users.noreply.github.com"
    return {"login": login, "name": u.get("name") or login, "id": u.get("id"), "email": email}


def repo_owner_name(repo_url: str) -> tuple[str, str] | None:
    """Parse (owner, name) from a github.com remote URL, https or ssh form."""
    m = (re.match(r"https://(?:[^@/]+@)?github\.com/([^/]+)/(.+?)(?:\.git)?$", repo_url)
         or re.match(r"git@github\.com:([^/]+)/(.+?)(?:\.git)?$", repo_url))
    return (m.group(1), m.group(2)) if m else None


def github_can_push(owner: str, name: str, token: str) -> bool | None:
    """Whether this token's account has push access to owner/name. None when
    undeterminable (repo not found / API hiccup) — callers should not assume
    write access in that case."""
    try:
        status, r = _gh_api("GET", f"/repos/{owner}/{name}", token)
    except Exception:  # noqa: BLE001
        return None
    if status != 200:
        return None
    return bool((r.get("permissions") or {}).get("push"))


def fork_repo(owner: str, name: str, token: str, timeout: int = 60) -> dict:
    """Fork owner/name into the token's account (idempotent — GitHub returns the
    existing fork if one is already there) and poll until it's clone-ready.
    Returns {owner, name, full_name, clone_url}. Raises RuntimeError on failure."""
    status, r = _gh_api("POST", f"/repos/{owner}/{name}/forks", token, body={}, timeout=30)
    if status not in (200, 201, 202):
        raise RuntimeError(f"GitHub fork request failed ({status}): "
                           f"{(r.get('message') or 'unknown error')[:200]}")
    fork_owner = r.get("owner", {}).get("login")
    fork_name = r.get("name")
    if not fork_owner or not fork_name:
        raise RuntimeError("GitHub did not return the fork's location.")
    # Fork creation is async — poll until the repo actually exists and is
    # queryable (GitHub docs: usually seconds, occasionally longer on big repos).
    deadline = time.time() + timeout
    while time.time() < deadline:
        s, _ = _gh_api("GET", f"/repos/{fork_owner}/{fork_name}", token, timeout=10)
        if s == 200:
            return {"owner": fork_owner, "name": fork_name,
                    "full_name": f"{fork_owner}/{fork_name}",
                    "clone_url": f"https://github.com/{fork_owner}/{fork_name}.git"}
        time.sleep(2)
    raise RuntimeError("Fork was created but isn't ready yet — try again in a moment.")


def slug(repo_url: str) -> str:
    parts = [x for x in repo_url.rstrip("/").replace(".git", "").replace(":", "/").split("/") if x]
    return f"{parts[-2]}__{parts[-1]}" if len(parts) >= 2 else parts[-1]


def workdir(repo_url: str) -> Path:
    base = Path(settings.repos_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    return base / slug(repo_url)


def ensure_clone(repo_url: str) -> str:
    """Clone the repo into the workspace (or fetch if already there). Returns path."""
    path = workdir(repo_url)
    if (path / ".git").exists():
        _run(["git", "fetch", "--all", "--prune"], cwd=str(path), check=False)
        return str(path)
    _run(["git", "clone", repo_url, str(path)], timeout=1800)
    return str(path)


def count_files(path: str) -> int:
    out = _run(["git", "ls-files"], cwd=path, check=False)
    return len([l for l in out.splitlines() if l.strip()])


def list_files(path: str, limit: int = 250) -> list[str]:
    out = _run(["git", "ls-files"], cwd=path, check=False)
    return [l for l in out.splitlines() if l.strip()][:limit]


def tree_for(repo_url: str, limit: int = 300) -> list[str]:
    """Real file tree from the already-cloned working copy (no network). Empty
    if the repo hasn't been cloned yet."""
    p = workdir(repo_url)
    if (p / ".git").exists():
        return list_files(str(p), limit)
    return []


def read_file(path: str, rel: str, max_chars: int = 6000) -> str | None:
    fp = Path(path) / rel
    try:
        return fp.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return None


def write_file(path: str, rel: str, content: str) -> None:
    """Write a file inside the working copy. Refuses any path that resolves
    outside `path` (blocks absolute/drive-letter/UNC/`..`/symlink escapes)."""
    base = Path(path).resolve()
    fp = (base / rel).resolve()
    if not fp.is_relative_to(base):
        raise ValueError(f"refusing to write outside working copy: {rel!r}")
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content, encoding="utf-8")


def _grep(path: str, flags: str, pattern: str, pathspec: str, *,
          fixed: bool = False, ref: str | None = None) -> list[str]:
    """Run `git grep` and return its lines. With `ref` (e.g. 'origin/master')
    the search runs against that COMMITTED tree instead of the working copy —
    essential for anything computed before the pipeline resets the shared
    clone, which may still sit on a stale agent branch (unmerged code would
    otherwise look like it exists). The 'ref:' prefix git adds is stripped."""
    args = ["git", "grep", flags] + (["--fixed-strings"] if fixed else [])
    args += [pattern] + ([ref] if ref else []) + ["--", pathspec]
    out = _run(args, cwd=path, check=False, timeout=60)
    prefix = f"{ref}:" if ref else ""
    return [l.removeprefix(prefix) for l in out.splitlines() if l.strip()]


def grep_definition_files(path: str, name: str, max_files: int = 3,
                          ref: str | None = None) -> list[str]:
    """Files that DEFINE symbol `name` (def/class/function/const), via git grep.
    Empty when the symbol isn't defined anywhere (i.e. likely invented or new)."""
    esc = re.escape(name)
    pat = rf"(def|class|function|const|var|let)\s+{esc}\b|^\s*{esc}\s*[:=]"
    return _grep(path, "-lE", pat, ".", ref=ref)[:max_files]


def grep_lines(path: str, pattern: str, max_lines: int = 40, pathspec: str = ".",
               ignore_case: bool = False, ref: str | None = None) -> str:
    """`git grep -nE pattern` → 'file:line:text' lines (capped). Used as the
    fallback Dev loop's search tool so it can localize code itself instead of
    trusting the PM's pins. Invalid patterns just return no matches."""
    lines = _grep(path, "-niE" if ignore_case else "-nE", pattern, pathspec, ref=ref)
    body = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        body += f"\n… ({len(lines) - max_lines} more matches)"
    return body or "(no matches)"


def grep_files(path: str, pattern: str, pathspec: str = ".", max_files: int = 5,
               ignore_case: bool = False, ref: str | None = None) -> list[str]:
    """Files matching an extended-regex `pattern` under a git pathspec (git's
    `*` crosses directory boundaries, so '*test*' means 'any path containing
    test'). Used to localize the EXISTING tests that exercise a symbol."""
    return _grep(path, "-liE" if ignore_case else "-lE", pattern, pathspec, ref=ref)[:max_files]


def grep_mention_files(path: str, text: str, max_files: int = 3,
                       ref: str | None = None) -> list[str]:
    """Files merely CONTAINING `text` (fixed string) — existence check for a
    symbol/flag without assuming it's a definition."""
    return _grep(path, "-l", text, ".", fixed=True, ref=ref)[:max_files]


def rev_parse(path: str, ref: str = "HEAD") -> str:
    """Resolve a ref to a commit sha ('' when the ref doesn't exist)."""
    try:
        return _run(["git", "rev-parse", ref], cwd=path, check=False, timeout=30).strip()
    except Exception:  # noqa: BLE001
        return ""


def changed_files(path: str, old_sha: str, new_sha: str) -> tuple[list[str], list[str]] | None:
    """(changed_or_added, deleted) file paths between two commits, or None when
    the range can't be computed (e.g. `old_sha` no longer exists after a gc)."""
    try:
        out = _run(["git", "diff", "--name-status", f"{old_sha}..{new_sha}"],
                   cwd=path, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    changed: list[str] = []
    deleted: list[str] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, paths = parts[0], parts[1:]
        if status.startswith("D"):
            deleted.append(paths[0])
        elif status.startswith("R") and len(paths) >= 2:  # rename: old gone, new changed
            deleted.append(paths[0])
            changed.append(paths[1])
        else:
            changed.append(paths[-1])
    return changed, deleted


def is_merged(path: str, branch: str) -> bool:
    """True iff `branch`'s tip is already an ancestor of origin's default
    branch — i.e. its changes are actually part of the code future runs clone
    and analyze, not just sitting on an unpushed/unmerged local branch (which
    is ALWAYS the case under DEMO_MODE, and also true of a real PR that hasn't
    been merged yet)."""
    base = f"origin/{default_branch(path)}"
    p = subprocess.run(["git", "merge-base", "--is-ancestor", branch, base],
                       cwd=path, capture_output=True, timeout=30)
    return p.returncode == 0


def diff_names(path: str, base: str | None = None, ref: str | None = None) -> list[str]:
    """File paths touched by a branch (same range semantics as `diff`)."""
    base = base or f"origin/{default_branch(path)}"
    spec = f"{base}...{ref}" if ref else base
    out = _run(["git", "diff", "--name-only", spec], cwd=path, check=False, timeout=120)
    return [l.strip() for l in out.splitlines() if l.strip()]


def default_branch(path: str) -> str:
    out = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=path, check=False).strip()
    name = out.split("/")[-1] if out else ""
    return name or "main"


def current_branch(path: str) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path).strip()


def checkout_branch(path: str, name: str) -> None:
    """Start `name` fresh from origin's default branch. The clone is SHARED
    across runs, so first drop any leftovers from a previous/crashed run —
    uncommitted edits and untracked files (except the cached test env) would
    otherwise pollute this run's diff and QA verdict."""
    _run(["git", "reset", "--hard"], cwd=path, check=False)
    _run(["git", "clean", "-fd", "-e", ".agent-venv"], cwd=path, check=False)
    base = default_branch(path)
    _run(["git", "checkout", f"origin/{base}"], cwd=path, check=False)
    _run(["git", "checkout", "-B", name], cwd=path)


def add_commit(path: str, msg: str) -> bool:
    _run(["git", "add", "-A"], cwd=path)
    p = subprocess.run(["git", *_identity_args(), "commit", "-m", msg],
                       cwd=path, capture_output=True, text=True)
    return p.returncode == 0  # False when nothing to commit


def diff(path: str, base: str | None = None, ref: str | None = None) -> str:
    """Diff. With `ref` (a branch), diff `base...ref` so it's independent of
    which branch is currently checked out (the shared clone is reused per repo).
    Without `ref`, diff base vs the working tree."""
    base = base or f"origin/{default_branch(path)}"
    spec = f"{base}...{ref}" if ref else base
    return _run(["git", "diff", spec], cwd=path, check=False, timeout=120)


def diff_worktree(path: str) -> str:
    """Diff of UNCOMMITTED work only (incl. new files via intent-to-add) — i.e.
    exactly what the Dev loop has done for the current ticket."""
    _run(["git", "add", "-N", "."], cwd=path, check=False)
    return _run(["git", "diff", "HEAD"], cwd=path, check=False, timeout=120)


def diff_stat(path: str, base: str | None = None, ref: str | None = None) -> tuple[int, int]:
    base = base or f"origin/{default_branch(path)}"
    spec = f"{base}...{ref}" if ref else base
    out = _run(["git", "diff", "--numstat", spec], cwd=path, check=False)
    ins = dels = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            if parts[0].isdigit():
                ins += int(parts[0])
            if parts[1].isdigit():
                dels += int(parts[1])
    return ins, dels


def _token_push_url(path: str, token: str | None, target: tuple[str, str] | None = None) -> str | None:
    """Tokenized https push URL for `target` (owner, name) — or origin's own
    (owner, name) when target is omitted — so the push authenticates as the
    given (or shared bot) account. Never persisted; used only as an explicit
    push target. None for a non-GitHub remote or no token available."""
    tok = (token or settings.github_bot_token or "").strip()
    if not tok:
        return None
    if target is None:
        origin = _run(["git", "remote", "get-url", "origin"], cwd=path, check=False).strip()
        target = repo_owner_name(origin)
        if target is None:
            return None  # non-GitHub remote — fall back to the host's credentials
    owner, name = target
    return f"https://x-access-token:{tok}@github.com/{owner}/{name}.git"


def push(path: str, branch: str, token: str | None = None,
         target: tuple[str, str] | None = None) -> None:
    """Push `branch`. With `target` (owner, name), push to that repo (e.g. the
    user's fork) instead of origin — origin's own remote config is untouched."""
    url = _token_push_url(path, token, target)
    if url:  # push AS the token's account; keep origin's stored URL untouched
        _run(["git", "push", "-u", url, branch, "--force"], cwd=path, timeout=300)
        return
    if target is not None:
        raise RuntimeError("Pushing to a fork requires a token (none available).")
    _run(["git", "push", "-u", "origin", branch, "--force"], cwd=path, timeout=300)


def gh_pr_create(path: str, title: str, body: str, base: str | None = None,
                 token: str | None = None, repo: str | None = None,
                 head_owner: str | None = None, branch: str | None = None) -> str:
    """Open a PR for `branch` (defaults to whatever's checked out — but the
    working copy is a SHARED clone other runs can reset, so callers holding a
    specific branch name should always pass it explicitly rather than rely on
    the current checkout). With `repo` ("owner/name") + `head_owner`, opens a
    cross-repo PR from head_owner's fork branch into `repo` — the normal
    open-source contribution flow, usable even without write access to `repo`."""
    base = base or default_branch(path)
    branch = branch or current_branch(path)
    head = f"{head_owner}:{branch}" if head_owner else branch
    env = _token_env(token)  # with a token, the PR is created BY that account
    args = ["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", head]
    if repo:
        args += ["--repo", repo]
    out = _run(args, cwd=path, timeout=180, check=False, env=env)
    m = re.search(r"https?://\S+", out)
    if m:
        return m.group(0)
    # If a PR already exists, gh prints the existing URL to stderr; try to surface it.
    # (For a cross-repo/fork PR, `gh pr view` needs the head ref explicitly —
    # the checked-out branch name alone doesn't resolve against a foreign repo.)
    view_args = ["gh", "pr", "view", head, "--json", "url", "-q", ".url"] + (["--repo", repo] if repo else [])
    existing = _run(view_args, cwd=path, check=False, env=env)
    return existing.strip() or out.strip()


def _exclude_local(path: str, pattern: str) -> None:
    """Add a pattern to .git/info/exclude so agent-created dirs (like the test
    venv) never get committed by `git add -A`."""
    try:
        f = Path(path) / ".git" / "info" / "exclude"
        existing = f.read_text(encoding="utf-8") if f.exists() else ""
        if pattern not in existing:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(existing + f"\n{pattern}\n", encoding="utf-8")
    except OSError:
        pass


def _dev_requirements(root: Path) -> list[str]:
    """Test-only dependency NAMES declared outside PEP-621 extras.

    Poetry dev-groups and PEP-735 dependency-groups are invisible to
    ``pip install -e .[test]``, so a repo like Textualize/rich installs green
    and then cannot collect its own suite (missing ``attrs``) — QA reads that
    as a code failure and burns revision rounds on phantom problems. Version
    constraints are dropped on purpose: this env only has to import.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []
    try:
        import tomllib
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — malformed/unreadable: nothing to add
        return []

    tables: list[dict] = []
    poetry = data.get("tool", {}).get("poetry", {})
    tables.append(poetry.get("dev-dependencies", {}))
    for group in poetry.get("group", {}).values():
        tables.append(group.get("dependencies", {}))

    names: list[str] = []
    for table in tables:
        if isinstance(table, dict):
            names += [k for k in table if k != "python"]
    # PEP-735: values are requirement strings, not a name->spec mapping.
    for reqs in data.get("dependency-groups", {}).values():
        if isinstance(reqs, list):
            names += [re.split(r"[\[<>=!~; ]", r, maxsplit=1)[0] for r in reqs if isinstance(r, str)]

    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def test_python(path: str) -> str:
    """Interpreter to run the repo's tests with. Builds a per-repo env
    (.agent-venv, via uv: repo installed editable + pytest) so tests that import
    the package actually run; cached across runs. Falls back to this server's
    interpreter when uv is missing or the install fails."""
    root = Path(path).resolve()  # absolute — uv runs with cwd=path, so a relative
    venv_py = root / ".agent-venv" / "bin" / "python"  # path would double-prefix
    if venv_py.exists():
        return str(venv_py)
    uv = shutil.which("uv")
    installable = (root / "setup.py").exists() or (root / "pyproject.toml").exists() or (root / "setup.cfg").exists()
    if uv and installable:
        _exclude_local(path, ".agent-venv/")
        try:
            _run([uv, "venv", ".agent-venv"], cwd=path, timeout=120)
            _run([uv, "pip", "install", "--python", str(venv_py), "-e", ".", "pytest"],
                 cwd=path, timeout=600)
            # Test extras (pytest plugins, fixtures…) — best-effort, name varies.
            for extra in ("test", "tests", "dev"):
                _run([uv, "pip", "install", "--python", str(venv_py), "-e", f".[{extra}]"],
                     cwd=path, timeout=600, check=False)
            # Poetry dev-groups / PEP-735 groups — not reachable via extras.
            dev_reqs = _dev_requirements(root)
            if dev_reqs:
                _run([uv, "pip", "install", "--python", str(venv_py), *dev_reqs],
                     cwd=path, timeout=600, check=False)
            if venv_py.exists():
                return str(venv_py)
        except Exception:  # noqa: BLE001 — best-effort; fall through to server python
            shutil.rmtree(root / ".agent-venv", ignore_errors=True)
    return sys.executable


def ensure_test_env(path: str) -> "lang.Runner | None":
    """Detect the repo's test ecosystem and make it runnable (best-effort):
    python → per-repo venv via test_python(); node → npm install when
    node_modules is missing; go/cargo fetch their own deps on first run.
    Returns the runner, or None when no ecosystem is recognized."""
    root = Path(path).resolve()
    runner = lang.detect_runner(root)
    if runner is None:
        return None
    if runner.setup == "npm" and not (root / "node_modules").exists():
        _exclude_local(path, "node_modules/")
        try:
            has_lock = (root / "package-lock.json").exists()
            _run(["npm", "ci" if has_lock else "install", "--no-audit", "--no-fund"],
                 cwd=path, timeout=600, check=False)
        except Exception:  # noqa: BLE001 — best-effort; tests then report None
            pass
    return runner


def test_command(path: str) -> str:
    """Command string the Dev agent should run tests with, '' when tests can't
    actually run here (advertising a broken command just misleads the agent).
    For Python this builds the per-repo venv as a side effect."""
    runner = ensure_test_env(path)
    if runner is None:
        return ""
    if runner.kind == "python":
        py = test_python(path)
        return "" if py == sys.executable else f"{py} -m pytest -q"
    return runner.label


def import_check(path: str, files: list[str]) -> tuple[bool | None, str]:
    """Smoke-check edited source files for semantic corruption the edit-time
    parse gate can't see (e.g. a stray module-scope reference). Python modules
    are imported in the repo's test env; Go repos get `go build ./...`; Rust
    `cargo check`. Returns (ok, detail); ok is None when nothing is checkable
    (unknown language / missing toolchain — fail open)."""
    src = [f for f in files if not lang.is_test_file(f)]
    if any(f.endswith(".go") for f in src):
        return _build_smoke(path, ["go", "build", "./..."], "go.mod", "go")
    if any(f.endswith(".rs") for f in src):
        return _build_smoke(path, ["cargo", "check", "-q"], "Cargo.toml", "cargo")
    return _python_import_check(path, src)


def _build_smoke(path: str, cmd: list[str], manifest: str, tool: str) -> tuple[bool | None, str]:
    root = Path(path).resolve()
    if not (root / manifest).exists() or shutil.which(tool) is None:
        return None, f"no {tool} toolchain/manifest — skipped"
    try:
        p = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=300)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not run {' '.join(cmd)}: {exc}"
    if p.returncode == 0:
        return True, f"`{' '.join(cmd)}` OK"
    return False, (p.stderr or p.stdout)[-2000:]


def _python_import_check(path: str, files: list[str]) -> tuple[bool | None, str]:
    root = Path(path).resolve()  # absolute — subprocess runs with cwd=path
    venv_py = (root / ".agent-venv" / "bin" / "python")
    py = str(venv_py) if venv_py.exists() else sys.executable
    mods: list[str] = []
    for f in files:
        if not f.endswith(".py"):
            continue
        # file path → dotted module (only for files inside a package with __init__)
        parts = f[:-3].split("/")
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            mods.append(".".join(parts))
    if not mods:
        return None, "no importable modules edited"
    stmt = "import importlib,sys\n" + "\n".join(
        f"importlib.import_module({m!r})" for m in mods)
    try:
        p = subprocess.run([py, "-c", stmt], cwd=path, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not run import check: {exc}"
    if p.returncode == 0:
        return True, f"imported OK: {', '.join(mods)}"
    return False, (p.stderr or p.stdout)[-2000:]


def _has_pytest_suite(root: Path) -> bool:
    return (root / "tests").exists() or (root / "pytest.ini").exists() or bool(list(root.glob("test_*.py")))


def failing_tests(path: str, timeout: int = 600) -> set[str] | None:
    """Ids of tests that FAIL/ERROR on the tree AS IT STANDS NOW. Returns None
    when there is no runnable suite OR the ecosystem's output has no reliably
    parseable per-test ids (e.g. npm — the runner behind `npm test` varies).
    Captured on the clean tree BEFORE Dev edits so QA can subtract these
    pre-existing failures and judge only the regressions the change actually
    introduced — a repo that ships with failing tests (e.g. textual's
    env-sensitive snapshot tests) otherwise makes QA read every stale failure
    as a new regression and burn revision rounds on nothing."""
    root = Path(path)
    runner = ensure_test_env(path)
    if runner is None or runner.fail_re is None:
        return None
    if runner.kind == "python":
        if not _has_pytest_suite(root):
            return None
        cmd = [test_python(path), "-m", "pytest", "-q", "--tb=no", "-rfE",
               "-p", "no:cacheprovider"]
    else:
        cmd = list(runner.cmd)
    try:
        p = subprocess.run(cmd, cwd=path, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001 — test envs vary wildly
        return None
    out = p.stdout + p.stderr
    if lang.no_tests_found(runner, p.returncode, out):
        return None
    return lang.parse_failures(runner, out)


def run_tests(path: str, test_paths: list[str] | None = None,
              timeout: int = 600) -> tuple[bool | None, str]:
    """Best-effort: detect and run the repo's test suite (pytest / npm test /
    go test / cargo test — see lang.detect_runner). Returns (passed, output).
    passed is None when no suite is found or deps are missing — never a false
    FAIL. With `test_paths`, runs only those test files where the ecosystem
    supports targeting (pytest files; go per-package)."""
    root = Path(path)
    try:
        runner = ensure_test_env(path)
        if runner is None:
            return None, "No recognized test suite found."
        if runner.kind == "python":
            if not _has_pytest_suite(root):
                return None, "No recognized test suite found."
            args = [test_python(path), "-m", "pytest", "-q"]
            if test_paths:
                existing = [t for t in test_paths if (root / t).exists()]
                if existing:
                    args += existing
            p = subprocess.run(args, cwd=path, capture_output=True, text=True, timeout=timeout)
            out = (p.stdout + p.stderr)[-4000:]
            # Exit 5 = no tests collected; import/usage errors = deps missing → None,
            # so QA reports "couldn't run" instead of a false FAIL.
            if p.returncode in (2, 3, 4, 5) and ("error" in out.lower() or p.returncode == 5):
                return None, out
            return p.returncode == 0, out
        args = list(runner.cmd)
        if test_paths and runner.accepts_paths and runner.kind == "go":
            # go test targets packages, not files — map the touched test files
            # to their package dirs.
            pkgs = sorted({"./" + str(Path(t).parent) for t in test_paths
                           if (root / t).exists()})
            if pkgs:
                args = ["go", "test", *pkgs]
        p = subprocess.run(args, cwd=path, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout + p.stderr)[-4000:]
        if lang.no_tests_found(runner, p.returncode, out):
            return None, out
        return p.returncode == 0, out
    except Exception as exc:  # noqa: BLE001 — test envs vary wildly
        return None, f"Could not run tests: {exc}"

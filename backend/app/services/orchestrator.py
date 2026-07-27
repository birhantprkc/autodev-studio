"""Real agent pipeline: Dev → QA → Review → PR (gh).

Runs on a cloned working copy branch. Dev edits (via whichever agent backend the
stage is pointed at — Claude Code, Codex, Cursor, Aider, Gemini CLI, or an HTTP
coding loop); QA runs tests and an unbiased chat review; Review is a separate
agent pass over the diff (deliberately selectable on a different backend/model
family than Dev); PR pushes the branch and opens a real PR via gh. The task is
left in the `pr` column for a human to merge.
"""

import logging
import re
import time
from pathlib import Path

from sqlmodel import Session, select

from ..config import settings
from ..database import engine
from ..models import Repo, ScopeSession, Task, TaskStatus, utcnow
from . import (
    agent_backends,
    agent_runner,
    deepwiki,
    git_ops,
    lang,
    llm,
    openai_agent,
    precision,
    prompts,
    providers,
)
from .knowledge import freshness, symbol_map, write_back

logger = logging.getLogger(__name__)

# Model aliases the Claude CLI understands (vs an OpenAI-compatible model id).
_CLI_ALIASES = {"sonnet", "opus", "haiku", "default"}


def _review_label(provider: str) -> str:
    if providers.agent_backend(provider) == "claude-code":
        m = settings.review_model if settings.review_model in _CLI_ALIASES else settings.claude_model
        return f"claude-cli {m}"
    return providers.label(provider, settings.review_model)


def _agent_label(provider: str, model: str | None) -> str:
    """Run-row label for an agent-backend provider ('auto'/'anthropic' show as
    the claude-cli they resolve to)."""
    if providers.agent_backend(provider) == "claude-code":
        return f"claude-cli {model or settings.claude_model}"
    return providers.label(provider, model or "")


def _agent_model(provider: str, backend: str, configured: str) -> str | None:
    """Model to hand an agent backend: for the Claude CLI only its aliases are
    valid; other backends take the configured model id (or their default)."""
    if backend == "claude-code":
        return configured if configured in _CLI_ALIASES else None
    p = providers.PROVIDERS.get(provider)
    return (configured or "").strip() or (p.default_model if p else "") or None


def _qa_label() -> str:
    return providers.label(settings.qa_provider, settings.qa_model)


def _prs_enabled() -> bool:
    """Real pushes/PRs only when explicitly enabled AND not in demo mode."""
    return settings.open_real_pr and not settings.demo_mode


def _kb_context(repo_url: str, info: dict, on_event=None) -> str:
    """Feed the Dev agent the RIGHT knowledge for this task: a use-case-scoped,
    token-budgeted slice from Deep Analysis (with exact source files), so Claude
    goes straight to the right files instead of grepping the whole repo. Falls
    back to a focused DeepWiki ask when the analysis isn't available."""
    if not repo_url:
        return ""
    query = f"{info.get('title', '')}: {info.get('description', '')}"
    # Precision retrieval first — right knowledge, not more.
    try:
        ctx = precision.retrieve(query, use_case="task-breakdown", repo_url=repo_url)
        if ctx:
            if on_event:
                on_event("info", f"Precision KB: task-scoped slice ({len(ctx)} chars)")
            return ctx
    except Exception:  # noqa: BLE001
        pass
    # Fallback: focused DeepWiki ask.
    try:
        ctx = deepwiki.ask(repo_url, [{"role": "user", "content": (
            f"For implementing '{info['title']}: {info.get('description', '')}', list the specific "
            "repository files and functions that must change and how they currently work. "
            "Be concise and cite exact file paths.")}], timeout=150)
        if on_event and ctx:
            on_event("info", f"KB context ({len(ctx)} chars, DeepWiki fallback)")
        return ctx
    except Exception:  # noqa: BLE001
        return ""


def _pick_dev_model(info: dict) -> str:
    """Cheaper Haiku for pure test-writing subtasks, Sonnet for real code. A
    whole-scope work order always gets the full model (it mixes code + tests)."""
    if info.get("is_scope"):
        return settings.claude_model
    text = f"{info.get('title', '')} {info.get('description', '')}".lower()
    is_test = any(k in text for k in ("test", "coverage", "pytest", "unit test"))
    return settings.claude_test_model if is_test else settings.claude_model


def _cli_dev_model(info: dict) -> str:
    """CLI model for the Dev stage: the operator's ``dev_model`` when it names a CLI
    alias (sonnet/opus/haiku), else the size-picked default (which also honors the
    cheaper test model for test-writing subtasks)."""
    m = (settings.dev_model or "").strip()
    return m if m in _CLI_ALIASES else _pick_dev_model(info)


def _auto_http_target() -> tuple[str, str]:
    """(provider, model) for the 'auto' Dev fallback when the Claude CLI can't run.
    Routes dev_model by name so a legacy gemini-* dev model still reaches Gemini
    (matching the pre-registry auto-fallback behavior)."""
    m = settings.dev_model
    return ("gemini" if (m or "").startswith("gemini") else "openai"), m


_SYM_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

# Injected file-content budget for the Dev prompt: combined char cap across all
# pinned files, per-file cap, and the overall verified-locations block cap
# (raised from 4800 to fit real content, not just outlines/snippets).
_FILE_CONTENT_BUDGET = 9000
_FILE_CONTENT_MAX_PER_FILE = 4000
_VERIFIED_BLOCK_MAX = 16000


def _verified_locations(path: str, files: list[str] | None, symbols: list[str] | None) -> str:
    """Harness-computed localization brief for the Dev prompt: exact file:line
    pins for every known symbol plus a symbol outline of every affected file.
    Live data shows the Dev agent otherwise spends ~70% of its tool calls (and
    most of the run's input tokens) on find/ls/whole-file reads rediscovering
    exactly this.

    Two free sources, cross-checked: the precomputed line-numbered symbol map
    (kept current by knowledge/freshness.py at run entry) names the DEFINITION
    site and powers the outlines; live `git grep` verifies against this exact
    working copy and adds usage pins. Symbols found nowhere get 'did you mean'
    suggestions from the map instead of sending Dev hunting."""
    smap = symbol_map.load_slug(Path(path).name)
    lines: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    test_files: dict[str, None] = {}  # ordered set of existing tests to surface
    noise = (".md", ".rst", ".txt", ".yml", ".yaml", ".lock", ".cfg", ".toml")
    for sym in (symbols or [])[:12]:
        s = str(sym)
        if "(new" in s:  # tagged by ground_tickets: not found anywhere in the repo
            unknown.append(s.split("(")[0].strip())
            continue
        pinned_file = s.split("::")[0].strip() if "::" in s else ""
        name = s.split("::")[-1].strip()
        idents = _SYM_NAME_RE.findall(name)
        if not idents:
            continue
        ident = max(idents, key=len)
        if ident in seen:
            continue
        seen.add(ident)
        entry: list[str] = []
        # Definition site(s) from the symbol map — the highest-value pin.
        map_hits = smap.lookup(ident) if smap else []
        if map_hits:
            defs = "; ".join(
                f"{f}:{m['l']} ({m['k']}" + (f" of {m['p']}" if m.get("p") else "") + ")"
                for f, m in map_hits[:2])
            entry.append(f"    defined at {defs}")
            if not pinned_file:  # grep the definition file for precise usage rows
                pinned_file = map_hits[0][0]
        # Grep the symbol's own pinned file first (precise), repo-wide only as
        # fallback — and drop docs/config hits, which just distract the agent.
        pat = rf"\b{re.escape(ident)}\b"
        hits = git_ops.grep_lines(path, pat, max_lines=3, pathspec=pinned_file) if pinned_file else ""
        if not hits or hits == "(no matches)":
            hits = git_ops.grep_lines(path, pat, max_lines=10)
        rows = [h for h in hits.splitlines()
                if ":" in h and not h.split(":", 1)[0].endswith(noise)][:3]
        entry.extend(f"    {h[:140]}" for h in rows)
        if entry:
            lines.append(f"  {ident}:\n" + "\n".join(entry))
            # Localize the EXISTING tests exercising this symbol too — Dev should
            # extend the real suite (and run it), not write a parallel one.
            for tf in git_ops.grep_files(path, pat, pathspec="*test*", max_files=3):
                if not tf.endswith(noise):
                    test_files.setdefault(tf)
        elif smap:  # nowhere in map or grep — likely a PM invention
            unknown.append(ident)
    # Full contents of the pinned files, budgeted — this is what actually saves
    # Dev a Read tool call. Live runs showed Dev re-reading whole files it was
    # already pointed at (~70% of its input tokens on rediscovery); an outline
    # alone still leaves Dev needing the real text before it can edit.
    content_budget = _FILE_CONTENT_BUDGET
    contents: list[str] = []
    for f in (files or [])[:8]:
        text = git_ops.read_file(path, f, max_chars=_FILE_CONTENT_MAX_PER_FILE + 1)
        if text is None:
            lines.append(f"  file {f}: DOES NOT EXIST (create it if needed)")
            continue
        outline = smap.outline(f, max_symbols=25) if smap else ""
        lines.append(f"  file {f}: exists" + (f" — contains: {outline}" if outline else ""))
        if content_budget > 500 and len(contents) < 3:
            snippet = text[:min(_FILE_CONTENT_MAX_PER_FILE, content_budget)]
            note = "\n... (truncated — read the rest yourself if you need it)" if len(text) > len(snippet) else ""
            contents.append(f"--- {f} ---\n{snippet}{note}")
            content_budget -= len(snippet)
    out = ""
    if lines:
        out += ("Verified code locations (symbol map + `git grep` against this working copy "
                "just now — these pins are real, go straight to them):\n" + "\n".join(lines))
    if test_files:
        out += ("\n  existing tests exercising these symbols (extend/update THESE and run "
                "them — don't create a parallel suite): " + ", ".join(list(test_files)[:6]))
    if unknown:
        notes = []
        for u in dict.fromkeys(unknown):
            close = smap.suggest(u) if smap else []
            notes.append(u + (f" (similar existing: {', '.join(close)})" if close else ""))
        out += ("\nNot found anywhere in the repo (either new things to create, or PM "
                "inventions to ignore — use judgment): " + ", ".join(notes))
    if contents:
        out += ("\n\nFull current contents of the pinned files (already read for you — do NOT "
                "spend a Read tool call on these; only read a file yourself if it's not shown "
                "here, or a shown file was truncated and you need the rest):\n\n"
                + "\n\n".join(contents))
    return out[:_VERIFIED_BLOCK_MAX]


def _test_cmd(path: str) -> str:
    """Test command the Dev agent should verify with — '' when tests can't
    actually run here (advertising a broken command just misleads the agent).
    Ecosystem-aware: pytest in the per-repo venv, npm test, go test, cargo test,
    mvn/gradlew test, rspec (see lang.detect_runner); building the env is a
    side effect."""
    try:
        return git_ops.test_command(path)
    except Exception:  # noqa: BLE001
        return ""


def _is_transient(err: str) -> bool:
    """Errors worth retrying on the SAME (strong) model: overload, rate limit,
    timeout, flaky network. Distinct from 'Claude genuinely can't run here'."""
    low = (err or "").lower()
    return any(k in low for k in ("429", "rate limit", "overload", "timed out",
                                  "timeout", "529", "500", "connection", "network"))


def _backend_unavailable(backend: str, err: str) -> bool:
    """True only when the agent backend can't run AT ALL on this machine (missing
    binary, no headless mode, or unauthenticated) — the only case where degrading
    to the HTTP coding loop beats failing visibly."""
    if not agent_backends.is_available(backend):
        return True
    low = (err or "").lower()
    return any(k in low for k in ("not runnable", "login", "authent", "api key",
                                  "credit balance", "billing", "unknown agent backend",
                                  "no scriptable", "headless"))


def _dev_agent(path: str, key: str, info: dict, on_event, context: str = "",
               test_cmd: str = "") -> tuple[dict, str]:
    """Run the Dev coding agent. When dev_provider maps to an agent backend
    (Claude CLI, Codex, Cursor, Aider, Gemini CLI, or 'auto'), run that headless
    CLI; a backend that can't run at all on this machine falls back to the HTTP
    coding loop instead of failing the scope. A concrete OpenAI-compatible
    provider runs the SEARCH/REPLACE loop on that provider.
    Returns (result, model_label)."""
    provider = settings.dev_provider
    backend = providers.agent_backend(provider)
    if backend:
        model = _cli_dev_model(info) if backend == "claude-code" \
            else _agent_model(provider, backend, settings.dev_model)
        label = _agent_label(provider, model)
        on_event("info", f"Dev model: {label}")
        prompt = prompts.dev(key, info["title"], info["description"], info["criteria"], context,
                             affected_files=info.get("affected_files"),
                             target_symbols=info.get("target_symbols"),
                             test_cmd=test_cmd,
                             verified=_verified_locations(path, info.get("affected_files"),
                                                          info.get("target_symbols")))
        res = agent_backends.run(backend, path, prompt, on_event, model=model)
        if res["error"] and _is_transient(res["error"]):
            # Overload/rate-limit/timeout: retry the same backend once. NEVER hand
            # transient failures to the free-tier coder — it produces broken edits
            # (proven in the run logs), which is worse than failing loudly.
            on_event("warn", f"{backend} transient error ({res['error'][:80]}) — retrying once")
            time.sleep(15)
            res = agent_backends.run(backend, path, prompt, on_event, model=model)
        # Fail open: degrade to the HTTP coder only when the backend can't run at
        # all (not installed / no headless mode / unauthenticated).
        if not res["error"] or not _backend_unavailable(backend, res["error"]):
            return res, label
        on_event("warn", f"{backend} unavailable ({(res['error'] or '')[:50]}…) — "
                         "falling back to the HTTP coding agent")
        fb_provider, fb_model = _auto_http_target()
    else:
        fb_provider, fb_model = provider, settings.dev_model
    cr = openai_agent.code(path, key, info["title"], info["description"], info["criteria"],
                           on_event, provider=fb_provider, model=fb_model, context=context,
                           affected_files=info.get("affected_files"),
                           target_symbols=info.get("target_symbols"))
    return ({"text": cr["summary"], "tokens_in": cr["tokens_in"], "tokens_out": cr["tokens_out"],
             "cost": cr["cost"], "error": cr["error"]}, providers.label(fb_provider, fb_model))


def _inconclusive(res: dict) -> bool:
    """The agent call produced NO verdict at all (errored with empty text) —
    fundamentally different from a verdict of PASS/FAIL/APPROVED."""
    return bool(res.get("error")) and not (res.get("text") or "").strip()


def _review_once(path: str, key: str, criteria: list, diff: str, on_event) -> tuple[dict, str]:
    provider = settings.review_provider  # separate from Dev — unbiased reviewer
    backend = providers.agent_backend(provider)
    if backend:  # any agentic CLI can review — cross-provider vs Dev by config
        model = _agent_model(provider, backend, settings.review_model)
        res = agent_backends.run(backend, path, prompts.review(key, criteria, diff), on_event,
                                 model=model)
        return res, provider
    r = llm.chat(prompts.REVIEW_SYSTEM, prompts.review(key, criteria, diff),
                 provider=provider, model=settings.review_model)
    if r.get("text"):
        on_event("info", r["text"])  # full review — the log panel shows the whole verdict
    return ({"text": r.get("text", ""), "tokens_in": r.get("tokens_in", 0),
             "tokens_out": r.get("tokens_out", 0), "cost": r.get("cost", 0.0),
             "error": r.get("error")}, provider)


def _review_agent(path: str, key: str, criteria: list, diff: str, on_event) -> tuple[dict, str]:
    """Review with an explicit INCONCLUSIVE outcome: when the reviewer can't run
    at all (network/provider failure, even after in-call retries), retry once,
    then stamp a loud INCONCLUSIVE verdict instead of an empty summary. A blank
    review_summary reads as 'no issues' downstream (board, PR body, write-back) —
    which is how an unreviewed delivery once shipped looking clean."""
    res, provider = _review_once(path, key, criteria, diff, on_event)
    if _inconclusive(res):
        on_event("warn", f"Review agent could not run ({(res.get('error') or '')[:80]}) — retrying once")
        time.sleep(20)
        res, provider = _review_once(path, key, criteria, diff, on_event)
    if _inconclusive(res):
        res["text"] = (f"VERDICT: INCONCLUSIVE — the review agent could not run "
                       f"({(res.get('error') or 'unknown error')[:160]}). "
                       "This change has NOT been code-reviewed.")
        on_event("error", "Review INCONCLUSIVE — this delivery has NOT been code-reviewed")
    return res, provider


def _qa_agent(key: str, title: str, criteria: list, diff: str, test_out: str, on_event) -> dict:
    """QA with the same explicit INCONCLUSIVE outcome as _review_agent."""
    user = prompts.qa_user(key, title, criteria, diff, test_out)
    qa = llm.chat(prompts.QA_SYSTEM, user, provider=settings.qa_provider, model=settings.qa_model)
    if _inconclusive(qa):
        on_event("warn", f"QA agent could not run ({(qa.get('error') or '')[:80]}) — retrying once")
        time.sleep(20)
        qa = llm.chat(prompts.QA_SYSTEM, user, provider=settings.qa_provider, model=settings.qa_model)
    if _inconclusive(qa):
        qa["text"] = (f"VERDICT: INCONCLUSIVE — the QA agent could not run "
                      f"({(qa.get('error') or 'unknown error')[:160]}). "
                      "This change has NOT been verified by QA.")
        on_event("error", "QA INCONCLUSIVE — this delivery has NOT been verified")
    return qa


# Bounded automated back-and-forth: if Review requests changes (or QA fails),
# feed the feedback back to Dev and re-run QA+Review, up to this many rounds.
# Runtime-configurable (Settings → Pipeline); read at loop entry so an edit
# mid-flight applies to the next run, not the one in progress.
def _max_revision_rounds() -> int:
    return max(0, int(settings.max_revision_rounds))


# --- Trivial-task fast path ---------------------------------------------------
# The fixed PM+QA+Review floor (~$0.14 in the benchmarks) makes very cheap
# greppable edits lose to a cold `claude -p`; on trivial tasks LLM QA is also
# the false-fail risk that burns paid revision rounds. Triage is deterministic
# on BOTH sides of Dev (no LLM self-estimate — same rationale as
# pm_agent.ground_tickets: cheap and can't hallucinate): pre-Dev the scope must
# be one ticket with small, grep-pinned localization; post-Dev the actual diff
# must be small AND the deterministic test gate green (runnable suite, zero new
# failures). Only then are the LLM QA + Review passes skipped, with explicit
# fast-path verdicts stamped — never blank summaries. No suite, new failures,
# or a bigger change than triaged all fall through to the full loop.

_FAST_PATH_MAX_FILES = 2      # scoped AND actually-touched non-test source files
_FAST_PATH_MAX_LINES = 120    # changed non-test lines in the final diff


def _fast_path_eligible(subs: list[dict]) -> bool:
    """Pre-Dev triage: one ticket, ≤2 affected files, few criteria, and every
    target symbol grep-pinned to a real definition ('file::name' — the form
    ground_tickets emits only after verifying the definition site). Unpinned or
    '(new — not in repo yet)' symbols mean unverified localization → full loop."""
    if not settings.fast_path_enabled or len(subs) != 1:
        return False
    s = subs[0]
    files = s.get("affected_files") or []
    symbols = s.get("target_symbols") or []
    if not files or len(files) > _FAST_PATH_MAX_FILES:
        return False
    if len(s.get("criteria") or []) > 4:
        return False
    return bool(symbols) and all(
        "::" in str(y) and "(new" not in str(y) for y in symbols)


def _diff_stats(diff_text: str) -> tuple[int, int]:
    """(non-test source files touched, changed non-test lines) of a unified
    diff — the post-Dev reality check that the change is as small as triaged.
    Test files don't count against the budget: a one-line fix plus real
    regression tests is exactly the shape the fast path is for."""
    files: set[str] = set()
    in_test = False
    changed = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            rel = line.split(" b/", 1)[-1]
            in_test = lang.is_test_file(rel)
            if not in_test:
                files.add(rel)
        elif not in_test and (
                (line.startswith("+") and not line.startswith("+++"))
                or (line.startswith("-") and not line.startswith("---"))):
            changed += 1
    return len(files), changed


def _review_changes_requested(text: str) -> bool:
    return "CHANGES REQUESTED" in (text or "").upper()


def _qa_failed(text: str) -> bool:
    """True only on an explicit VERDICT: FAIL (CONCERNS does not trigger a revise)."""
    up = (text or "").upper()
    i = up.find("VERDICT")
    return i != -1 and "FAIL" in up[i:i + 40]


def _dev_revise(path: str, key: str, title: str, criteria: list, review_text: str,
                qa_text: str, on_event, context: str = "",
                affected_files: list | None = None, target_symbols: list | None = None,
                diff: str = "", test_cmd: str = "") -> tuple[dict, str]:
    """Revision Dev pass: edit the already-committed change to address feedback.
    Inherits the PM's localization (affected_files/target_symbols) and the current
    branch diff, so the reviser edits the right files instead of scraping paths
    out of review prose (which produced zero-change revisions)."""
    provider = settings.dev_provider
    prompt = prompts.revise(key, title, criteria, review_text, qa_text, context, test_cmd=test_cmd,
                            verified=_verified_locations(path, affected_files, target_symbols))
    backend = providers.agent_backend(provider)
    if backend:
        if backend == "claude-code":
            model = settings.dev_model if settings.dev_model in _CLI_ALIASES else settings.claude_model
        else:
            model = _agent_model(provider, backend, settings.dev_model)
        label = _agent_label(provider, model)
        on_event("info", f"Dev model: {label} (revision)")
        res = agent_backends.run(backend, path, prompt, on_event, model=model)
        if res["error"] and _is_transient(res["error"]):
            on_event("warn", f"{backend} transient error ({res['error'][:80]}) — retrying once")
            time.sleep(15)
            res = agent_backends.run(backend, path, prompt, on_event, model=model)
        if not res["error"] or not _backend_unavailable(backend, res["error"]):
            return res, label
        on_event("warn", f"{backend} unavailable — HTTP coding agent for revision")
        fb_provider, fb_model = _auto_http_target()
    else:
        fb_provider, fb_model = provider, settings.dev_model
    # OpenAI-compatible path: feedback becomes the task; the current diff rides in the context.
    desc = (f"REVISION of an already-committed change (see the current branch diff in the "
            f"repository knowledge below). Address this reviewer feedback:\n{review_text[:4000]}\n\n"
            f"QA findings:\n{qa_text[:2000]}")
    ctx = f"Current branch diff (the change being revised):\n```diff\n{diff[:8000]}\n```\n\n{context}"
    cr = openai_agent.code(path, key, title, desc, criteria, on_event, provider=fb_provider,
                           model=fb_model, context=ctx,
                           affected_files=affected_files, target_symbols=target_symbols)
    return ({"text": cr["summary"], "tokens_in": cr["tokens_in"], "tokens_out": cr["tokens_out"],
             "cost": cr["cost"], "error": cr["error"]}, providers.label(fb_provider, fb_model))


def _update(task_id: int, **fields) -> None:
    with Session(engine) as db:
        task = db.get(Task, task_id)
        if task is None:
            return
        for k, v in fields.items():
            setattr(task, k, v)
        task.updated_at = utcnow()
        db.add(task)
        db.commit()


def _update_all(ids: list[int], **fields) -> None:
    for i in ids:
        _update(i, **fields)


def _load(task_id: int) -> tuple[dict, str] | None:
    with Session(engine) as db:
        task = db.get(Task, task_id)
        if task is None:
            return None
        repo = db.get(Repo, task.repo_id)
        info = {
            "key": task.key, "title": task.title, "description": task.description or "",
            "criteria": task.acceptance_criteria or [],
            "affected_files": task.affected_files or [],
            "target_symbols": task.target_symbols or [],
        }
        return info, (repo.git_url if repo else "")




def run_scope(session_id: int) -> None:
    """Public entry point. Serializes all work on a repo's SHARED working copy so
    two scopes never run it at once — concurrent scopes race on branch/commit
    state (a `reset --hard` under a live run destroys the other's work), which is
    a real corruption we hit, not a theoretical one. Different repos still run in
    parallel; only same-repo scopes queue behind this lock."""
    with Session(engine) as db:
        session = db.get(ScopeSession, session_id)
        repo = db.get(Repo, session.repo_id) if session else None
        repo_url = repo.git_url if repo else ""
    if not repo_url:
        _run_scope_locked(session_id)
        return
    with git_ops.repo_lock(repo_url):
        _run_scope_locked(session_id)


def _run_scope_locked(session_id: int) -> None:
    """Run a whole scope as ONE deliverable: every approved subtask is implemented
    on a SINGLE branch (accumulating), then one QA + Review + a single PR for the
    scope. All subtasks share that PR — so the PR is a complete, valid feature.
    Always called under `git_ops.repo_lock` via `run_scope`."""
    with Session(engine) as db:
        session = db.get(ScopeSession, session_id)
        if session is None:
            return
        repo = db.get(Repo, session.repo_id)
        repo_url = repo.git_url if repo else ""
        subtasks = db.exec(
            select(Task).where(
                Task.session_id == session_id, Task.approved == True,  # noqa: E712
                Task.status.in_([TaskStatus.scoped.value, TaskStatus.backlog.value]),
            ).order_by(Task.key)
        ).all()
        subs = [{"id": t.id, "key": t.key, "title": t.title, "description": t.description or "",
                 "criteria": t.acceptance_criteria or [],
                 "affected_files": t.affected_files or [], "target_symbols": t.target_symbols or []}
                for t in subtasks]
        scope_title = session.title or f"Scope #{session_id}"
    if not subs:
        logger.info("run_scope %s: no approved subtasks", session_id)
        return
    ids = [s["id"] for s in subs]
    all_criteria = [c for s in subs for c in s["criteria"]]

    try:
        path = git_ops.ensure_clone(repo_url)
        branch = f"agent/scope-{session_id}"
        git_ops.checkout_branch(path, branch)
    except Exception as exc:  # noqa: BLE001
        logger.error("run_scope %s workspace prep failed: %s", session_id, exc)
        return

    # --- Dev: ONE work order for the whole scope ---
    # Fragmenting a feature into per-ticket Dev runs is what produced "flag
    # registered but never threaded through": no single run ever saw the whole
    # feature. One Dev session gets every subtask + criteria and can wire the
    # change end-to-end; tickets stay as tracking/approval artifacts.
    rep = subs[0]["id"]  # scope-level Dev/QA/Review/PR runs hang off the first subtask
    all_files = list(dict.fromkeys(f for s in subs for f in s["affected_files"]))
    all_symbols = list(dict.fromkeys(y for s in subs for y in s["target_symbols"]))
    desc = "\n\n".join(
        f"Subtask {s['key']}: {s['title']}\n{s['description']}\nAcceptance criteria:\n"
        + ("\n".join(f"- {c}" for c in s["criteria"]) or "- (use your judgment)")
        for s in subs)
    work = {"key": f"scope-{session_id}", "title": scope_title, "description": desc,
            "criteria": all_criteria, "affected_files": all_files,
            "target_symbols": all_symbols, "is_scope": True}

    _update_all(ids, status=TaskStatus.in_dev.value, branch=branch)
    rid, t0 = agent_runner.start_run(rep, "dev")
    agent_runner.log(rid, "info",
                     f"Dev agent implementing {len(subs)} subtask(s) as one work order on {branch}")
    # The tree was just reset to origin's default branch — the one safe moment
    # to sync the knowledge layers (symbol map free; prose views incrementally).
    freshness.refresh_if_stale(repo_url, on_event=agent_runner.logger_for(rid))
    kb = _kb_context(repo_url, work, agent_runner.logger_for(rid))
    test_cmd = _test_cmd(path)  # builds the per-repo test env so Dev can verify
    if test_cmd:
        agent_runner.log(rid, "info", f"Repo test env ready: {test_cmd}")
    # Baseline the suite on the CLEAN tree (before Dev edits) so QA later judges
    # only NEW failures. A repo that ships failing tests otherwise makes QA read
    # every stale failure as a regression and burn revision rounds fixing nothing.
    baseline_fail = git_ops.failing_tests(path)
    if baseline_fail:
        agent_runner.log(rid, "warn",
                         f"{len(baseline_fail)} test(s) already fail on clean "
                         f"{git_ops.default_branch(path)} — QA will discount these as pre-existing")
    res, dev_label = _dev_agent(path, work["key"], work, agent_runner.logger_for(rid), kb, test_cmd)
    agent_runner.set_model(rid, dev_label)
    committed = git_ops.add_commit(path, f"scope-{session_id}: {scope_title[:60]}")
    agent_runner.log(rid, "success" if committed else "warn",
                     "Committed changes" if committed else "No file changes produced")
    agent_runner.finish_run(rid, rep, t0, tokens_in=res["tokens_in"], tokens_out=res["tokens_out"],
                            cost=res["cost"], error=res["error"])
    if res["error"] and not committed:
        logger.error("run_scope %s: Dev produced nothing (%s) — stopping before QA", session_id, res["error"])
        # Recovery: undo the in_dev marker on every subtask so the scope is
        # immediately re-runnable (no manual DB reset needed).
        _update_all(ids, status=TaskStatus.scoped.value)
        return

    diff = git_ops.diff(path, ref=branch)

    # --- QA + Review with a bounded revise loop: on CHANGES REQUESTED or QA FAIL,
    #     feed the feedback back to Dev, re-commit, and re-run — up to N rounds. ---
    qa_text = ""
    rev_text = ""
    fast_candidate = _fast_path_eligible(subs)
    max_rounds = _max_revision_rounds()
    for attempt in range(max_rounds + 1):
        # QA (OpenAI) over the whole scope diff
        _update_all(ids, status=TaskStatus.qa.value)
        rid, t0 = agent_runner.start_run(rep, "qa")
        passed, test_out = git_ops.run_tests(path)
        # Only re-diff failures when the suite failed AND the repo had pre-existing
        # failures — the exact case where QA would otherwise misread stale failures
        # as regressions. Clean repos / clean runs pay no extra suite run.
        if passed is False and baseline_fail:
            now_fail = git_ops.failing_tests(path) or set()
            new_fail = sorted(now_fail - baseline_fail)
            if new_fail:
                banner = ("REGRESSION CHECK: this change introduced " f"{len(new_fail)} NEW "
                          "test failure(s) not present before it — judge ONLY these, the rest are "
                          "pre-existing:\n  " + "\n  ".join(new_fail[:25]) + "\n\n")
            else:
                banner = ("REGRESSION CHECK: every failing test below ALSO failed on the clean tree "
                          "BEFORE this change (pre-existing, unrelated to it). This change introduced "
                          "ZERO new failures — treat the suite as GREEN for acceptance purposes.\n\n")
            test_out = banner + test_out
            passed = not new_fail  # no NEW failures ⇒ green for this change
        agent_runner.log(rid, "success" if passed else ("warn" if passed is None else "error"),
                         f"Tests: {'passed' if passed else ('no suite/deps' if passed is None else 'failures')}")
        # Fast path: the triaged-trivial change verified through the deterministic
        # gate (suite ran, zero new failures) and the diff is as small as triaged
        # — skip the paid LLM QA + Review passes with explicit verdicts stamped.
        if attempt == 0 and fast_candidate and passed is True and not res["error"]:
            n_files, n_lines = _diff_stats(diff)
            if n_files <= _FAST_PATH_MAX_FILES and n_lines <= _FAST_PATH_MAX_LINES:
                qa_text = (
                    "VERDICT: PASS — FAST PATH (task triaged trivial: one ticket, grep-pinned "
                    f"localization). Deterministic test gate green: suite ran with zero new "
                    f"failures; diff touches {n_files} source file(s), ~{n_lines} changed "
                    "line(s). LLM QA was skipped for this delivery.")
                rev_text = (
                    "FAST PATH: code review skipped — trivial, fully-localized change verified "
                    "by the deterministic test gate (see QA verdict). This diff was NOT "
                    "LLM-reviewed.")
                _update_all(ids, qa_summary=qa_text, review_summary=rev_text)
                agent_runner.set_model(rid, "deterministic-gate")
                agent_runner.log(rid, "success",
                                 f"Fast path: skipping LLM QA + Review (trivial task, suite green, "
                                 f"{n_files} file(s)/{n_lines} line(s)) — saved the QA+Review gate cost")
                agent_runner.finish_run(rid, rep, t0)
                break
        qa = _qa_agent(f"scope-{session_id}", scope_title, all_criteria, diff, test_out,
                       agent_runner.logger_for(rid))
        agent_runner.set_model(rid, _qa_label())
        if qa.get("text"):
            agent_runner.log(rid, "info", qa["text"])  # full QA verdict for the log panel
        qa_text = qa.get("text", "")
        if qa_text:
            _update_all(ids, qa_summary=qa_text)  # errored calls keep the last good verdict
        agent_runner.finish_run(rid, rep, t0, tokens_in=qa.get("tokens_in", 0), tokens_out=qa.get("tokens_out", 0),
                                cost=qa.get("cost", 0.0), error=qa.get("error"))

        # Review over the whole scope diff
        _update_all(ids, status=TaskStatus.review.value)
        rid, t0 = agent_runner.start_run(rep, "review")
        rev, rprov = _review_agent(path, f"scope-{session_id}", all_criteria, diff, agent_runner.logger_for(rid))
        agent_runner.set_model(rid, _review_label(rprov))
        rev_text = rev.get("text", "")
        if rev_text:
            _update_all(ids, review_summary=rev_text)  # errored calls keep the last good verdict
        agent_runner.finish_run(rid, rep, t0, tokens_in=rev["tokens_in"], tokens_out=rev["tokens_out"],
                                cost=rev["cost"], error=rev["error"])

        # Decide: ship, or send back to Dev for another round?
        needs_fix = _review_changes_requested(rev_text) or _qa_failed(qa_text)
        if not needs_fix:
            break
        if attempt >= max_rounds:
            agent_runner.log(rid, "warn",
                             f"Still CHANGES REQUESTED after {max_rounds} revision round(s) — "
                             "opening the PR with the outstanding feedback noted")
            break

        # --- Revision Dev pass over the whole scope: fix, re-commit, re-diff ---
        round_no = attempt + 1
        _update_all(ids, status=TaskStatus.in_dev.value)
        rid, t0 = agent_runner.start_run(rep, "dev")
        agent_runner.log(rid, "info",
                         f"Revision round {round_no}/{max_rounds}: addressing review + QA feedback")
        kb = _kb_context(repo_url, {"title": scope_title, "description": ""}, agent_runner.logger_for(rid))
        # The reviser inherits the scope's localization plus the current branch diff.
        res, dev_label = _dev_revise(path, f"scope-{session_id}", scope_title, all_criteria,
                                     rev_text, qa_text, agent_runner.logger_for(rid), kb,
                                     affected_files=all_files, target_symbols=all_symbols,
                                     diff=diff, test_cmd=test_cmd)
        agent_runner.set_model(rid, dev_label)
        committed = git_ops.add_commit(path, f"scope-{session_id}: address review feedback (round {round_no})")
        agent_runner.log(rid, "success" if committed else "warn",
                         "Committed revision" if committed else "No changes produced by revision")
        agent_runner.finish_run(rid, rep, t0, tokens_in=res["tokens_in"], tokens_out=res["tokens_out"],
                                cost=res["cost"], error=res["error"])
        if not committed:
            # Dev disputed the feedback (or had nothing to change). Re-running QA+Review
            # on a byte-identical diff buys the same verdict twice — stop the loop and
            # ship with the outstanding feedback noted instead of burning rounds.
            agent_runner.log(rid, "warn",
                             "Revision produced no changes — skipping re-QA/re-review of an "
                             "identical diff; opening the PR with the outstanding feedback noted")
            break
        diff = git_ops.diff(path, ref=branch)

    # --- ONE PR for the whole scope ---
    _update_all(ids, status=TaskStatus.pr.value)
    rid, t0 = agent_runner.start_run(rep, "pr")
    pr_title = f"Scope: {scope_title[:60]}"
    pr_body = prompts.scope_pr_body(scope_title, subs, qa_text)
    pr_link = ""
    if not _prs_enabled():
        agent_runner.log(rid, "info",
                         f"[DEMO_MODE] Dry-run — would open one scope PR for {len(subs)} subtasks: {pr_title}")
        agent_runner.log(rid, "info", pr_body)
        agent_runner.finish_run(rid, rep, t0)
    else:
        try:
            git_ops.push(path, branch)
            agent_runner.log(rid, "info", f"Pushed {branch} to origin")
            pr_link = git_ops.gh_pr_create(path, pr_title, pr_body)
            _update_all(ids, pr_url=pr_link)
            agent_runner.log(rid, "success", f"Opened one scope PR for {len(subs)} subtasks: {pr_link}")
            agent_runner.finish_run(rid, rep, t0)
        except Exception as exc:  # noqa: BLE001
            agent_runner.finish_run(rid, rep, t0, error=f"PR failed: {exc}")

    # --- Write-back: persist what this delivery learned (files, symbols,
    # gotchas) into the knowledge base so future runs on this repo start from
    # it instead of rediscovering everything. ---
    write_back.record_delivery(
        repo_url, f"scope-{session_id}", scope_title, path, branch,
        dev_summary=res.get("text", ""), qa_text=qa_text, review_text=rev_text,
        pr_url=pr_link, on_event=lambda lvl, msg: agent_runner.log(rid, lvl, msg))

    with Session(engine) as db:
        session = db.get(ScopeSession, session_id)
        if session is not None:
            session.status = "delivered"
            db.add(session)
            db.commit()
    logger.info("run_scope %s finished (%d subtasks)", session_id, len(subs))

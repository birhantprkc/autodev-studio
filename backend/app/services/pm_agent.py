"""Conversational PM agent — an agentic retrieval loop modeled on oxygen/PM_agent.

Orchestration is done by hand (no framework). Per user turn:

    1. Bootstrap (each turn, cheap): load repository + architecture + modules
       knowledge docs so the PM has a repo-shaped anchor.
    2. Agent loop (capped at pm_max_retrieval_rounds): the PM decides ONE action:
         - "retrieve": it needs repo knowledge it doesn't have yet → we pull the
           most relevant knowledge docs for its `retrieval_query`, add them to the
           context, and loop again.
         - "question": the requirement is unclear → ask ONE clarifying question.
         - "answer": respond conversationally without locking scope yet.
         - "jira": it has enough to lock the scope (title, acceptance criteria,
           affected files + target symbols — the localization the Dev agent reuses).
       On the final allowed round we force it to stop retrieving and act.

    PM agent → retrieve knowledge → grow context → PM agent → … → question | jira

The LLM never sees raw code — only the structured knowledge docs. Runs on Groq
(OpenAI-compatible) so the whole flow works without a Claude terminal.
"""

from __future__ import annotations

import json
import re

from ..config import settings
from . import llm
from .knowledge import retriever as knowledge_retriever
from .knowledge import store

_SYSTEM = (
    "You are a sharp, senior Product Manager agent scoping a feature request for a "
    "specific software repository. You turn a requirement into a locked, "
    "unambiguous scope, using your KNOWLEDGE of the repository (never raw code).\n\n"
    "On every turn choose EXACTLY ONE action:\n"
    '- "retrieve": you need repository knowledge you don\'t have yet. Provide '
    "`retrieval_query` (a specific search phrase for what to look up) and "
    "`information_need` (1-2 sentences on WHAT you need to understand and which "
    "decision it informs). Describe the information itself — do NOT name storage "
    "or document types. Put a brief note in `message` about what you're looking for.\n"
    '- "question": the requirement is unclear or missing detail. Ask ONE focused '
    "clarifying question in `message`. Hunt for: vague quantifiers ('some', 'fast', "
    "'50% chance' — pin down exact deterministic rules), undefined terms, missing "
    "edge cases (idempotency, concurrency, empty/boundary values), authorization "
    "boundaries, where the result is shown/recorded, and failure modes.\n"
    '- "answer": respond conversationally (e.g. explaining part of the repo) '
    "without locking scope yet.\n"
    '- "jira": you have enough understanding to lock the scope. Fill `scope` and put '
    "a short note in `message`.\n\n"
    "Guidelines:\n"
    "- Bootstrap knowledge (repository, architecture, modules) is loaded first. Read "
    "it before deciding whether you need more.\n"
    "- Retrieve ITERATIVELY: each round has a specific purpose in `information_need`. "
    "Stop retrieving once you have what you need. Don't retrieve everything at once.\n"
    "- Ask clarifying questions before guessing at scope — but don't manufacture "
    "questions once the scope is clear (typically 1-3 for a non-trivial feature).\n"
    "- Only choose 'jira' once the requirement is reasonably clear. When the user "
    "confirms the scope is good, choose 'jira', return it unchanged, and set "
    "`finalized` true.\n"
    "- Ground acceptance criteria and files in the loaded knowledge. For "
    "`affected_files` use ONLY real paths you've seen in the knowledge (a new file "
    "path is fine); for `target_symbols` name only classes/functions you've seen. "
    "Never invent identifiers — if unsure of an exact name, describe its location.\n"
    "- acceptance_criteria describe OBSERVABLE BEHAVIOR only (given X, the user "
    "sees Y) — never files, symbols, or code structure. Structural claims belong "
    "in affected_files/target_symbols where they are treated as fallible hints; "
    "in a criterion a wrong one becomes an unsatisfiable hard requirement.\n"
    "- SCOPE MINIMALISM: criteria restate the USER'S ask and nothing more. Do not "
    "invent adjacent requirements, extra edge cases, or hardening the user didn't "
    "imply — every criterion is paid Dev+QA+Review work and a rejection ground. "
    "3-6 criteria for the whole scope is the norm; more means you're expanding "
    "the request. Note worthwhile extras in `message` instead of the scope.\n\n"
    "Respond ONLY as JSON: "
    '{"action": "retrieve"|"question"|"answer"|"jira", "message": string, '
    '"retrieval_query": string|null, "information_need": string|null, '
    '"reason": string|null, "finalized": boolean, '
    '"scope": {"summary": string, "acceptance_criteria": [string], '
    '"affected_files": [string], "target_symbols": [string]} | null}. '
    "scope is non-null ONLY on a 'jira' action."
)

_DRAFT_SYSTEM = (
    "You are a Product Manager agent. Turn the locked scope into concrete, small, "
    "implementable engineering tickets grounded in the repository. You have already "
    "done the localization — HAND THE DEV AGENT everything it needs so it goes "
    "straight to the right code without rediscovering the repo.\n\n"
    "For EACH ticket you MUST fill:\n"
    "  - affected_files: exact real file paths this ticket changes or adds (a NEW "
    "file path is fine). Never invent paths for existing code.\n"
    "  - target_symbols: the specific classes/functions/endpoints (by name) to edit "
    "or call, as 'path::symbol' when you know the file else the symbol name.\n"
    "  - description: precise and implementation-oriented — what to change in which "
    "file and how, referencing the symbols.\n"
    "  - acceptance_criteria: concrete, testable conditions about OBSERVABLE "
    "BEHAVIOR only (given input X, the user sees Y). NEVER name files, symbols, "
    "decorators, or code structure in a criterion — you work from summaries and a "
    "wrong structural claim becomes a hard requirement the reviewer enforces and "
    "the Dev cannot satisfy, deadlocking the pipeline in paid revision rounds. All "
    "localization goes in affected_files/target_symbols/description, which Dev "
    "treats as fallible hints.\n\n"
    "TICKET MINIMALISM — every ticket and criterion is a paid Dev+QA+Review cycle: "
    "default to ONE ticket covering the whole scope (a fix and its regression tests "
    "are one ticket, not two; implementation and wiring are one ticket). Split only "
    "when the scope truly contains independent deliverables. Do not add criteria "
    "beyond the locked scope's — the Dev implements exactly what criteria say and "
    "the Reviewer enforces them, so an invented one becomes real paid work.\n\n"
    "Respond ONLY as JSON: {\"tickets\": [{\"title\": string, \"description\": string, "
    '"acceptance_criteria": [string], "affected_files": [string], '
    '"target_symbols": [string], "priority": "low"|"medium"|"high"}]}. '
    "Produce 1-4 tickets (default 1)."
)


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


def _tree_block(file_tree: list[str] | None) -> str:
    if not file_tree:
        return "(repository not indexed yet — avoid naming specific files)"
    return "\n".join(file_tree[:200])


# --- Context: accumulated knowledge the PM has pulled this turn ---------------
def _render_doc(doc) -> str:
    head = f"[{doc.type}] {doc.name}"
    # Some doc types are inferred, not extracted (business rules are LOW by
    # design) — the PM must see that, or speculation reads with the same
    # authority as AST-verified facts and gets locked into scopes unchecked.
    conf = (doc.confidence or "").upper()
    if conf and conf != "HIGH":
        head += f" (confidence: {conf} — inferred, verify before relying on it)"
    lines = [head]
    if doc.summary:
        lines.append(f"  {doc.summary}")
    desc = doc.content.get("description") or doc.content.get("purpose")
    if desc:
        lines.append(f"  {str(desc)[:400]}")
    files = doc.content.get("files") or []
    if files:
        lines.append(f"  files: {', '.join(files[:8])}")
    syms = doc.content.get("symbols") or []
    if syms:
        lines.append(f"  symbols: {', '.join(str(s) for s in syms[:20])}")
    return "\n".join(lines)


def _bootstrap_context(repo_url: str) -> tuple[list[str], set[str]]:
    """Repository + architecture + top modules — the minimal repo-shaped anchor."""
    blocks: list[str] = []
    seen: set[str] = set()
    for doc_id in ("repository", "architecture"):
        doc = store.load(repo_url, doc_id)
        if doc is not None:
            blocks.append(_render_doc(doc))
            seen.add(doc.id)
    modules = [d for d in store.load_all(repo_url) if d.type == "module"]
    modules.sort(key=lambda d: -len(d.content.get("files") or []))
    for doc in modules[:8]:
        blocks.append(_render_doc(doc))
        seen.add(doc.id)
    return blocks, seen


def _retrieve_more(repo_url: str, query: str, seen: set[str]) -> list[str]:
    """Pull the most relevant not-yet-seen knowledge docs for `query`."""
    blocks: list[str] = []
    for doc, _score in knowledge_retriever.retrieve(repo_url, query, per_domain_k=3):
        if doc.id in seen:
            continue
        seen.add(doc.id)
        blocks.append(_render_doc(doc))
    return blocks


def _ask(repo_name: str, overview: str, file_tree: list[str] | None,
         context_blocks: list[str], conversation: str, force_answer: bool) -> dict:
    """One structured PM decision over the currently-loaded knowledge."""
    knowledge = "\n\n".join(context_blocks) if context_blocks else "(no structured knowledge loaded)"
    force = ("\n\nIMPORTANT: You have reached the maximum retrieval rounds for this "
             "turn. Do NOT choose 'retrieve' again — either ask a clarifying question "
             "(action='question') or lock the scope (action='jira') with what you have."
             ) if force_answer else ""
    user = (
        f"Repository: {repo_name}\n\nRepository overview:\n{overview or '(none)'}\n\n"
        f"Currently loaded repository knowledge:\n{knowledge}\n\n"
        f"Repository files (real paths — use ONLY these for affected_files):\n"
        f"{_tree_block(file_tree)}\n\n"
        f"Conversation so far:\n{conversation}\n\n"
        f"Decide your next action as JSON.{force}"
    )
    return llm.chat(_SYSTEM, user, provider=settings.pm_provider, model=settings.pm_model, json_mode=True)


def scope_turn(repo_name: str, repo_url: str, overview: str, history: list[dict],
               file_tree: list[str] | None = None) -> dict:
    """Run one agentic PM turn. `history` is [{role, content}] (user/agent).

    Returns {action, message, ready, scope, retrieval_rounds, retrieved,
    tokens_in, tokens_out, cost, error}. `ready` is True on a locked scope.
    """
    conversation = "\n".join(
        f"{'PM (human)' if m['role'] == 'user' else 'PM agent'}: {m['content']}" for m in history
    )
    context_blocks, seen = _bootstrap_context(repo_url)
    tin = tout = 0
    cost = 0.0
    retrieved: list[str] = []
    last_err = None

    max_rounds = max(1, settings.pm_max_retrieval_rounds)
    for round_num in range(max_rounds + 1):
        force = round_num >= max_rounds
        r = _ask(repo_name, overview, file_tree, context_blocks, conversation, force)
        tin += r.get("tokens_in", 0) or 0
        tout += r.get("tokens_out", 0) or 0
        cost += r.get("cost", 0.0) or 0.0
        last_err = r.get("error")
        data = _load_json(r.get("text", ""))
        action = data.get("action")

        if action == "retrieve" and not force and data.get("retrieval_query"):
            new_blocks = _retrieve_more(repo_url, str(data["retrieval_query"]), seen)
            retrieved.append(str(data.get("information_need") or data["retrieval_query"]))
            if not new_blocks:
                # Nothing new to add — force the PM to act with what it has.
                context_blocks.append(f"(no additional knowledge found for: {data['retrieval_query']})")
            else:
                context_blocks.extend(new_blocks)
            continue

        # Terminal action: question | answer | jira (or a malformed/forced turn).
        scope = data.get("scope") if (action == "jira" and isinstance(data.get("scope"), dict)) else None
        ready = scope is not None
        message = data.get("message") or last_err or "Could you tell me a bit more about what you need?"
        return {
            "action": action or "question", "message": message, "ready": ready,
            "scope": _normalize_scope(scope, set(file_tree or [])) if scope else None,
            "finalized": bool(data.get("finalized")),
            "retrieval_rounds": round_num, "retrieved": retrieved,
            "tokens_in": tin, "tokens_out": tout, "cost": cost, "error": last_err,
        }

    # Unreachable (loop always returns), but keep the type checker happy.
    return {"action": "question", "message": "Could you tell me a bit more?", "ready": False,
            "scope": None, "finalized": False, "retrieval_rounds": max_rounds,
            "retrieved": retrieved, "tokens_in": tin, "tokens_out": tout, "cost": cost, "error": last_err}


def _normalize_scope(scope: dict, real_paths: set[str]) -> dict:
    def _clean(key: str) -> list[str]:
        v = scope.get(key)
        return [str(x) for x in v if isinstance(x, (str, int))] if isinstance(v, list) else []

    files = _clean("affected_files")
    kept = [f for f in files if f in real_paths or ("/" in f or "." in f.rsplit("/", 1)[-1])]
    return {
        "summary": scope.get("summary", ""),
        "acceptance_criteria": _clean("acceptance_criteria"),
        "affected_files": kept,
        "target_symbols": _clean("target_symbols"),
    }


def draft_tickets(repo_name: str, scope: dict, context: str, file_tree: list[str] | None = None,
                  knowledge: str = "") -> dict:
    """Draft implementable tickets from the locked scope, carrying the PM's
    localization (affected_files / target_symbols) through to each ticket."""
    knowledge_block = f"Structured knowledge (architecture / modules / features):\n{knowledge}\n\n" if knowledge else ""
    user = (
        f"Repository: {repo_name}\n\nLocked scope:\n{json.dumps(scope, indent=2)}\n\n"
        f"{knowledge_block}"
        f"Repository files (real paths — reference ONLY these):\n{_tree_block(file_tree)}\n\n"
        f"Relevant repo context:\n{context or '(none)'}\n\nProduce the tickets as JSON."
    )
    r = llm.chat(_DRAFT_SYSTEM, user, provider=settings.pm_provider, model=settings.pm_model, json_mode=True)
    data = _load_json(r.get("text", ""))
    tickets = data.get("tickets") if isinstance(data.get("tickets"), list) else []
    real_paths = set(file_tree or [])
    tickets = [_normalize_ticket(t, real_paths) for t in tickets
               if isinstance(t, dict) and t.get("title")]
    return {"tickets": tickets, "tokens_in": r.get("tokens_in", 0),
            "tokens_out": r.get("tokens_out", 0), "cost": r.get("cost", 0.0), "error": r.get("error")}


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def ground_tickets(repo_url: str, tickets: list[dict]) -> list[dict]:
    """Deterministically verify each ticket's localization against the actual
    clone with `git grep` — the PM works from summarized knowledge and sometimes
    pins the wrong file or invents symbols (observed: arg-parser work pinned to
    options.py when httpie's arg definitions live in cli/definition.py).

    For every target symbol: if it's DEFINED somewhere, pin that real file (and
    add it to affected_files); if it merely appears, keep it as-is; if it appears
    NOWHERE, tag it '(new — not in repo yet)' so the Dev agent creates rather
    than hunts for it — with a 'did you mean' when a near-identical symbol
    exists (the usual PM failure is a slightly-wrong invented name).

    Lookup order: the precomputed symbol map (instant, line-numbered, kept
    current by knowledge/freshness.py) first, live git-grep as the fallback.
    Deterministic either way, no LLM — cheap and can't hallucinate."""
    from . import git_ops
    from .knowledge import symbol_map

    path = git_ops.workdir(repo_url)
    if not (path / ".git").exists():
        return tickets
    smap = symbol_map.load(repo_url)
    for t in tickets:
        files = list(t.get("affected_files") or [])
        symbols: list[str] = []
        tests: dict[str, None] = {}  # existing tests exercising this ticket's symbols
        for sym in t.get("target_symbols") or []:
            name = str(sym).split("::")[-1].strip()
            idents = _IDENT_RE.findall(name)
            ident = max(idents, key=len) if idents else ""
            if not ident:
                symbols.append(str(sym))
                continue
            defined_in = ([f for f, _ in smap.lookup(ident)] if smap else []) \
                or git_ops.grep_definition_files(str(path), ident)
            if defined_in:
                for f in dict.fromkeys(defined_in[:2]):
                    if f not in files:
                        files.append(f)
                symbols.append(f"{defined_in[0]}::{name}")
                # Localization includes the TESTS: name the existing tests that
                # exercise this symbol so the ticket (and its human approver)
                # sees the test surface before Dev starts.
                for tf in git_ops.grep_files(str(path), rf"\b{re.escape(ident)}\b",
                                             pathspec="*test*", max_files=3):
                    tests.setdefault(tf)
            elif git_ops.grep_mention_files(str(path), ident):
                symbols.append(str(sym))
            else:
                close = smap.suggest(ident) if smap else []
                hint = f"; similar existing: {', '.join(close)}" if close else ""
                symbols.append(f"{name} (new — not in repo yet{hint})")
        t["affected_files"] = files[:8]
        t["target_symbols"] = symbols
        if tests:
            t["description"] = (str(t.get("description") or "").rstrip()
                                + "\n\nExisting tests exercising the target symbols "
                                  "(grep-verified — extend these): "
                                + ", ".join(list(tests)[:5]))
    return tickets


def _normalize_ticket(t: dict, real_paths: set[str]) -> dict:
    def _clean_list(key: str) -> list[str]:
        v = t.get(key)
        return [str(x) for x in v if isinstance(x, (str, int))] if isinstance(v, list) else []

    files = _clean_list("affected_files")
    kept_files = [f for f in files if f in real_paths or ("/" in f or "." in f.rsplit("/", 1)[-1])]
    return {
        "title": t.get("title"),
        "description": t.get("description", ""),
        "acceptance_criteria": t.get("acceptance_criteria") or [],
        "affected_files": kept_files,
        "target_symbols": _clean_list("target_symbols"),
        "priority": t.get("priority"),
    }

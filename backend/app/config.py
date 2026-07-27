import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _load_key_from_env_file(name: str, env_path: str) -> str:
    """Read NAME=value from an arbitrary .env-style file (used as a cross-cwd
    fallback for keys not present in our own env)."""
    try:
        for line in Path(env_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class Settings(BaseSettings):
    """Runtime configuration, read from environment / .env."""

    # Anchor .env to the project root (AGENTS-SDLC/.env) so it's read regardless
    # of the launch directory (uvicorn may run from the repo root or backend/).
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[2] / ".env"),
        extra="ignore",
    )

    # Database — anchored to an ABSOLUTE path at the AGENT root so it never
    # depends on the server's launch cwd (a relative path silently created a
    # second, empty DB when the server was started from backend/).
    database_url: str = (
        f"sqlite:///{(Path(__file__).resolve().parents[2] / 'sdlc_agents.db').as_posix()}"
    )

    # --- Knowledge base / RAG ---
    # Retrieval backend for the agents' repo knowledge base:
    #   "local"    — the built-in, self-contained RAG (services/local_rag.py).
    #                No external services required; this is the default.
    #   "deepwiki" — an external DeepWiki Open server (optional, advanced).
    rag_backend: str = "local"
    # Embedding strategy for the local RAG:
    #   "semantic" — dense embeddings via fastembed (local, no API key) stored in
    #                an embedded Qdrant vector DB. The default. Automatically
    #                falls back to "tfidf" if fastembed/qdrant aren't installed.
    #   "api"      — any OpenAI-compatible /embeddings endpoint (OpenAI, Gemini,
    #                Voyage, Ollama/LM Studio locally, …): bring your own base
    #                URL + key + model. Vectors still live in embedded Qdrant.
    #   "tfidf"    — pure-Python TF-IDF + cosine (zero extra deps).
    rag_embeddings: str = "semantic"
    # Embedding model: a fastembed model id for "semantic" (bge-small is small,
    # fast, and free) or the endpoint's model id for "api" (e.g.
    # text-embedding-3-small, nomic-embed-text).
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    # "api" embeddings endpoint (OpenAI-compatible POST {base}/embeddings).
    # Examples: https://api.openai.com/v1 · http://localhost:11434/v1 (Ollama).
    embedding_api_base_url: str = ""
    embedding_api_key: str = ""
    # Embedded Qdrant storage path (per-repo collections live here; persists
    # across restarts, so a repo stays indexed without re-embedding).
    qdrant_path: str = "./.qdrant"

    # --- Structured knowledge (multi-view repo understanding) ---
    # Beyond chunk-level RAG, build structured *views* of each repo (architecture,
    # modules, features, workflows, entrypoints, domain concepts, business rules,
    # integrations) as JSON docs embedded per-domain. Fed to the PM agent when it
    # scopes work. Needs an LLM key + the semantic stack; degrades to chunk RAG.
    generate_knowledge: bool = True
    knowledge_dir: str = "./.knowledge"
    # Cost guard: describe at most this many (largest) modules per repo.
    knowledge_max_modules: int = 25
    # A top-level package with more analyzable files than this is split into
    # per-subdirectory module views (httpie/ → httpie/cli, httpie/output, …).
    # One doc per 60-file package is too coarse for retrieval to localize
    # anything inside it; ~15 keeps docs subsystem-sized.
    kb_module_split_files: int = 15

    # --- Knowledge freshness (services/knowledge/freshness.py) ---
    # Auto-refresh the KB at pipeline-run entry when origin has moved. The
    # symbol map (localization layer) is always synced for free; the LLM prose
    # views refresh incrementally per affected module, escalating to a full
    # rebuild only past the drift thresholds below.
    kb_auto_refresh: bool = True
    # Drift that triggers a full LLM rebuild instead of per-module refresh:
    # fraction of analyzable files changed since the last view build, or an
    # absolute changed-file count — whichever hits first.
    kb_full_rebuild_fraction: float = 0.25
    kb_full_rebuild_files: int = 100
    # Max module views regenerated per incremental refresh (1 LLM call each).
    kb_refresh_max_modules: int = 8
    # Rate limit on full rebuilds (hours) so a fast-moving repo doesn't re-pay
    # the whole ingest LLM cost on every run; deferred rebuilds are flagged and
    # happen in the next allowed window.
    kb_full_rebuild_min_hours: float = 24.0

    # --- Knowledge write-back (services/knowledge/write_back.py) ---
    # After a scope delivers, persist a delivery-note knowledge doc (files
    # touched, symbols added, gotchas) so future PM/Dev retrieval compounds
    # across runs instead of rediscovering the repo every time.
    kb_write_back: bool = True
    # Newest N delivery notes kept per repo (older ones are pruned).
    kb_delivery_notes_max: int = 40
    # When pruning, distill retiring notes' gotchas/wiring into per-module
    # `lesson` docs (survive full rebuilds) instead of discarding them — the KB
    # compounds without cluttering (one LLM call per affected module, at prune
    # time only).
    kb_consolidate: bool = True

    # --- Optional external DeepWiki Open server (only used when rag_backend="deepwiki") ---
    deepwiki_url: str = "http://localhost:8001"
    deepwiki_provider: str = "openai"
    deepwiki_model: str = "gpt-4o"
    repo_type: str = "github"                   # github | gitlab | bitbucket

    # --- Precision retrieval: right-sized, use-case-scoped, ranked context ---
    # Feed agents a token-budgeted slice with exact source files (instead of a
    # broad KB blob). Served by the local RAG by default.
    precision_retrieval: bool = True
    # Optional external "Deep Analysis" (AST+LLM) service for precision retrieval.
    # Off by default; the local RAG provides precision slices standalone.
    use_deep_analysis: bool = False
    functional_analysis_url: str = "http://localhost:8002"

    # --- Agents: Claude Code CLI (PM, Dev, Review, PR) ---
    claude_cli_path: str = "claude"
    # Efficient default for coding. The CLI default here is opus-4-6[1m] (very
    # expensive); "sonnet" is ~5x cheaper and excellent for code. Override with
    # CLAUDE_MODEL=opus for the hardest tasks.
    claude_model: str = "sonnet"
    # Cheaper model for test-writing subtasks (boilerplate-ish, well-specified).
    claude_test_model: str = "haiku"
    # Optional explicit API key; otherwise the CLI uses the host's Claude login.
    anthropic_api_key: str = ""
    # Autonomous file edits + tool use inside the isolated workspace clone.
    claude_skip_permissions: bool = True
    # Hard dollar ceiling per Claude agent run (--max-budget-usd). A normal
    # scope's Dev pass costs well under $1; the cap stops runaway sessions.
    # 0 disables the cap.
    claude_max_budget_usd: float = 2.0

    # --- Other headless coding agents (adapter registry: services/agent_backends).
    # Each stage can point at any of these; a tool that isn't installed is shown
    # as unavailable in Settings instead of failing a run. ---
    codex_cli_path: str = "codex"               # OpenAI Codex CLI (`codex exec`)
    cursor_cli_path: str = "cursor-agent"       # Cursor CLI headless agent
    cursor_api_key: str = ""                    # else the host's `cursor-agent login`
    aider_cli_path: str = "aider"
    gemini_cli_path: str = "gemini"             # Google Gemini CLI
    # Dev (code-writing) agent provider: "claude" | "openai" | "auto".
    # "auto" tries Claude, then falls back to the OpenAI coding agent if the
    # Claude CLI can't authenticate.
    coding_provider: str = "auto"
    # Review agent provider — intentionally a DIFFERENT model than Dev for a less
    # biased review. Defaults to OpenAI so Claude's code is reviewed by OpenAI.
    review_provider: str = "openai"

    # --- QA agent: a DIFFERENT provider (OpenAI) for less bias ---
    qa_provider: str = "openai"
    qa_model: str = "gpt-4o"
    # The "primary" OpenAI-compatible endpoint. Its base URL is configurable and
    # points at Groq's free tier out of the box (set to https://api.openai.com/v1
    # for real OpenAI). Exposed in the UI as the "openai" provider.
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"

    # --- Additional OpenAI-compatible providers (see services/providers.py) ---
    # Groq's own key + static endpoint, so it can be selected as a distinct provider
    # from the (configurable) "openai" primary above.
    groq_api_key: str = ""
    # xAI (Grok). Static https://api.x.ai/v1 endpoint.
    xai_api_key: str = ""
    # A user-defined OpenAI-compatible provider (OpenRouter, Together, Ollama, …).
    custom_api_key: str = ""
    custom_base_url: str = ""

    # --- Google AI Studio (Gemini) via its OpenAI-compatible endpoint ---
    # Gemini's free tier has a FAR larger per-minute token budget than Groq's
    # (e.g. gemini-2.0-flash ~1M TPM vs gpt-oss-120b's 8000), so it's the natural
    # overflow for big Dev/revision requests. Any model whose name starts with
    # "gemini" is routed here automatically. Set GEMINI_API_KEY to enable.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Added to the fallback pool (comma-separated) once a key is set. These absorb
    # requests Groq rejects for size (413/TPM) or daily quota.
    gemini_models: str = "gemini-3.1-flash-lite,gemini-flash-lite-latest"

    # --- Per-stage models (Groq free tier, OpenAI-compatible) ---
    # Each stage can run on a different model. This spreads load across separate
    # free-tier rate-limit pools AND gives real model diversity (so QA/Review are
    # genuinely less correlated with Dev, not "different provider" in name only).
    # Empty = fall back to qa_model (see resolution at the bottom of this file).
    #   knowledge — high call volume, purely interpretive short JSON → fast/cheap.
    #   pm        — the "strong PM" stage: hardest reasoning + strict JSON.
    #   dev       — precise SEARCH/REPLACE instruction-following.
    #   review    — a different pool/model from Dev for a less biased review.
    knowledge_model: str = "openai/gpt-oss-20b"
    pm_model: str = "openai/gpt-oss-120b"
    dev_model: str = "openai/gpt-oss-120b"
    review_model: str = "llama-3.3-70b-versatile"

    # --- Per-stage provider (which registry provider runs each stage) ---
    # A provider id from services/providers.py (groq | openai | gemini | xai |
    # anthropic | custom), plus "claude-cli" for dev/review and "auto" for dev.
    # These start empty and are backfilled at the bottom of this file from the
    # legacy coding/qa/review_provider + model names, so existing installs keep
    # their exact current routing (gemini-* models auto-route to Gemini, etc.).
    knowledge_provider: str = ""
    pm_provider: str = ""
    dev_provider: str = ""

    # --- Agentic Dev loop (openai_agent.code) ---
    # Max model calls per ticket: round 1 edits, then verify-against-diff rounds.
    dev_max_rounds: int = 4
    # Per-file char budget shown to the Dev model. 12K blind-truncated real test
    # files mid-file (the model then emitted SEARCH blocks that could never
    # match); 30K covers most source files whole. Gemini's 250K-TPM budget takes
    # this fine; Groq-routed requests are still middle-trimmed to max_request_chars.
    dev_file_chars: int = 30000
    # Run targeted pytest on touched test files inside the Dev loop (feeds real
    # failures back to the model before QA ever sees the change).
    dev_run_tests: bool = True
    # Hard cap on total request size (chars, ~4/token) sent to the LLM. Sized to
    # fit Groq free tier's tightest per-minute budget (gpt-oss-120b = 8000 TPM):
    # ~5.5k input tokens + headroom for output. Requests over this are trimmed
    # (middle-cut) instead of 413-ing. Raise it for paid tiers / roomier models.
    max_request_chars: int = 22000

    # Bounded automated back-and-forth in the pipeline: if Review requests
    # changes (or QA fails), feedback is fed back to Dev and QA+Review re-run —
    # up to this many rounds before the pipeline gives up and surfaces the state.
    max_revision_rounds: int = 2

    # Trivial-task fast path. The pipeline's fixed PM+QA+Review floor (~$0.14 in
    # the benchmarks) makes very cheap greppable edits cost more than a cold
    # `claude -p`. When a scope is deterministically trivial (one ticket, small
    # grep-pinned localization) AND the Dev change verifies through the
    # deterministic test gate (runnable suite, zero new failures) with a small
    # non-test diff, skip the paid LLM QA + Review passes and stamp an explicit
    # fast-path verdict instead. Any doubt — no runnable suite, new failures,
    # a bigger diff than triaged — falls through to the full QA+Review loop.
    fast_path_enabled: bool = True

    # PM agent (agentic retrieval loop, modeled on oxygen/PM_agent): max on-demand
    # knowledge-retrieval rounds the PM may run within a single turn before it must
    # answer / ask / draft. Bootstrap (repository+architecture+modules) is loaded
    # first, then the PM pulls more knowledge only as it decides it needs it.
    pm_max_retrieval_rounds: int = 3

    # Where agents check out working copies (cloned per repo).
    repos_dir: str = "./.workspace"

    # Optional fallback source for OPENAI_API_KEY (e.g. a sibling DeepWiki .env).
    # Empty by default — the primary source is OPENAI_API_KEY in env / .env.
    deepwiki_env_path: str = ""
    # Optional fallback .env for ANTHROPIC_API_KEY. Empty by default — primary
    # source is ANTHROPIC_API_KEY in env / .env.
    agent_env_path: str = ""

    # Safety: in demo mode the pipeline never pushes branches or force-updates a
    # remote — it logs the PR title/body it WOULD open and stops. Turn this OFF
    # (DEMO_MODE=false) only against a repo you own and intend to push to.
    demo_mode: bool = True
    # Open a real PR via the gh CLI at the end of the pipeline. Ignored (treated
    # as a dry-run) while demo_mode is on.
    open_real_pr: bool = True

    # --- Agent identity on deliveries ---
    # Commits the pipeline makes are authored by this name/email, so the change
    # history shows the agent (not whoever's laptop the server runs on).
    agent_git_name: str = "AutoDev Agent"
    agent_git_email: str = "autodev-agent@users.noreply.github.com"
    # Optional GitHub token of a dedicated bot/machine account. When set, branch
    # pushes and `gh pr create` authenticate as that account, so the PR itself is
    # "created by" the agent on GitHub. Without it, gh falls back to the host's
    # login (commits are still authored by the agent).
    github_bot_token: str = ""

    # --- Jira Cloud (optional) — approved tickets are pushed as Stories ---
    # All four must be set for the integration to activate; editable at runtime
    # from the Settings screen (services/jira.py rebuilds its client on change).
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""

    # Demo seed (real integration boots empty by default; set true for sample data)
    seed_on_startup: bool = False

    # --- Bootstrap admin ---
    # Password for the `admin` account created on first boot. Leave blank to have
    # one generated and printed to the server log once (see services/auth.py) —
    # there is deliberately no guessable default.
    admin_password: str = ""


settings = Settings()

# Per-stage models default to qa_model when left unset, so a single QA_MODEL still
# configures the whole system if the operator doesn't split them.
for _stage_field in ("knowledge_model", "pm_model", "dev_model", "review_model"):
    if not getattr(settings, _stage_field):
        setattr(settings, _stage_field, settings.qa_model)

# Reuse DeepWiki's configured OpenAI key for the QA agent if ours is unset.
if not settings.openai_api_key:
    settings.openai_api_key = os.environ.get("OPENAI_API_KEY", "") or _load_key_from_env_file(
        "OPENAI_API_KEY", settings.deepwiki_env_path
    )

# Gemini key: env first, then AGENT/.env (GEMINI_API_KEY or GOOGLE_API_KEY).
if not settings.gemini_api_key:
    settings.gemini_api_key = (
        os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
    )

# ANTHROPIC_API_KEY: env first, then AGENT/.env (works regardless of launch cwd).
if not settings.anthropic_api_key:
    settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "") or _load_key_from_env_file(
        "ANTHROPIC_API_KEY", settings.agent_env_path
    )

# GROQ_API_KEY: env first. Existing installs put their Groq key in OPENAI_API_KEY with
# OPENAI_BASE_URL pointed at Groq — mirror it into GROQ_API_KEY so the explicit 'groq'
# provider works without re-entering the key.
if not settings.groq_api_key:
    settings.groq_api_key = os.environ.get("GROQ_API_KEY", "")
if not settings.groq_api_key and "groq.com" in (settings.openai_base_url or "") and settings.openai_api_key:
    settings.groq_api_key = settings.openai_api_key
if not settings.xai_api_key:
    settings.xai_api_key = os.environ.get("XAI_API_KEY", "")


# --- Per-stage provider backfill --------------------------------------------------
# Preserves today's routing exactly for installs that predate the provider registry:
# gemini-* models auto-route to Gemini, everything else to the primary 'openai'
# endpoint, and dev/review honor the legacy claude/auto coding provider.
def _provider_for_model(model: str) -> str:
    return "gemini" if (model or "").startswith("gemini") else "openai"


if not settings.knowledge_provider:
    settings.knowledge_provider = _provider_for_model(settings.knowledge_model)
if not settings.pm_provider:
    settings.pm_provider = _provider_for_model(settings.pm_model)
if not settings.dev_provider:
    if settings.coding_provider == "auto":
        settings.dev_provider = "auto"
    elif settings.coding_provider == "claude":
        settings.dev_provider = "claude-cli"
    else:
        settings.dev_provider = _provider_for_model(settings.dev_model)

# qa_provider / review_provider already exist with legacy values ("openai"/"claude"/
# "auto"). Normalize them to real provider ids from services/providers.py.
if settings.review_provider in ("claude", "auto"):
    settings.review_provider = "claude-cli"
elif settings.review_provider in ("", "openai"):
    settings.review_provider = _provider_for_model(settings.review_model)
if settings.qa_provider in ("", "openai", "claude", "auto"):
    settings.qa_provider = _provider_for_model(settings.qa_model)

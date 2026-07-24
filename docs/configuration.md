# Configuration

There are two ways to configure AutoDev Studio, and they layer:

1. **Environment / `.env`** — the baseline, read at startup. Copy
   [`.env.example`](../.env.example) to `.env` and edit. Every setting has a sensible
   default except the API keys.
2. **The Settings screen (admin, runtime)** — most values are also editable live from
   the UI. UI-saved values are stored in the database and **override** the env
   baseline; API keys saved this way are encrypted at rest (Fernet). A value never
   touched in the UI keeps its env/`.env` value.

So the effective value of a setting is: *UI override, if one exists; otherwise the
env/`.env` value; otherwise the built-in default.*

## First-boot admin

On first boot an `admin` account is created. Its password comes from
`ADMIN_PASSWORD` if you set it; otherwise a random one is generated and printed to
the server log **once**. There is deliberately no guessable default. Change it from
**Settings → Access & users** (the UI nags until you do), or preset it:

```bash
ADMIN_PASSWORD=choose-your-own
```

## LLM providers

The pipeline calls LLMs through an OpenAI-compatible client, the native Anthropic
Messages API, and/or the Claude Code CLI. You only need keys for the providers you
actually use. The defaults are tuned to run the non-Dev stages on **Groq's free
tier**.

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Primary OpenAI-compatible key (PM/QA agents, KB synthesis). |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Point at any OpenAI-compatible endpoint. |
| `ANTHROPIC_API_KEY` | — | Optional; lets the Dev/Review agents use the Claude Code CLI. |
| `GROQ_API_KEY` | — | Groq free-tier key (fast small models for QA/Review/PM). |
| `GEMINI_API_KEY` | — | Google Gemini (OpenAI-compatible endpoint). |
| `XAI_API_KEY` | — | xAI / Grok. |
| `CUSTOM_API_KEY` / `CUSTOM_BASE_URL` | — | Any other OpenAI-compatible provider. |

### Per-stage model + provider

Each pipeline stage picks its own provider and model, so you can put a frontier model
where it matters (Dev) and cheap models everywhere else:

| Stage | Provider var | Model var |
|---|---|---|
| Knowledge synthesis | `KNOWLEDGE_PROVIDER` | `KNOWLEDGE_MODEL` |
| PM (scoping) | `PM_PROVIDER` | `PM_MODEL` |
| Dev (coding) | `CODING_PROVIDER` | `DEV_MODEL` / `CLAUDE_MODEL` |
| QA | `QA_PROVIDER` | `QA_MODEL` |
| Review | `REVIEW_PROVIDER` | `REVIEW_MODEL` |

`CODING_PROVIDER=auto` (recommended) uses the Claude Code CLI when it's installed and
authenticated, and falls back to the OpenAI-compatible edit loop otherwise. Code
writing is the one stage that reliably needs a frontier model; small free-tier models
fail at precise multi-file edits.

## Knowledge base

| Variable | Default | Purpose |
|---|---|---|
| `RAG_BACKEND` | `local` | `local` (built-in) or `deepwiki` (external server). |
| `RAG_EMBEDDINGS` | `semantic` | `semantic` (fastembed + Qdrant) or `tfidf` (pure-Python fallback). |
| `GENERATE_KNOWLEDGE` | `true` | Build the structured multi-view knowledge on ingest. |
| `KNOWLEDGE_MAX_MODULES` | `25` | Cost guard: describe at most this many modules per repo. |
| `KB_MODULE_SPLIT_FILES` | `15` | Split a package into per-subdirectory views past this many files. |
| `KB_AUTO_REFRESH` | `true` | Refresh the KB at run entry when origin has moved. |
| `KB_FULL_REBUILD_FRACTION` | `0.25` | Drift fraction that forces a full rebuild vs incremental. |
| `KB_FULL_REBUILD_FILES` | `100` | Absolute changed-file count that forces a full rebuild. |

See [knowledge-base.md](knowledge-base.md) for what these control.

## Pipeline behaviour

| Variable | Default | Purpose |
|---|---|---|
| `MAX_REVISION_ROUNDS` | `2` | Dev↔QA/Review revise loops before giving up (0 disables). |
| `DEV_MAX_ROUNDS` | `4` | Max model calls per ticket inside the Dev agent. |
| `DEV_RUN_TESTS` | `true` | Run targeted tests inside the Dev loop. |
| `DEV_FILE_CHARS` | `30000` | Cap on pinned-file content injected into the Dev prompt. |
| `PM_MAX_RETRIEVAL_ROUNDS` | `3` | KB retrieval rounds the PM agent may run while scoping. |
| `MAX_REQUEST_CHARS` | `22000` | Cap on the raw request text accepted for scoping. |

## Delivery & safety

| Variable | Default | Purpose |
|---|---|---|
| `DEMO_MODE` | `true` | Dry-run the PR stage (logs the PR it would open; never pushes). |
| `OPEN_REAL_PR` | `true` | Whether real PRs are opened *when* demo mode is off. |
| `REPOS_DIR` | `~/.autodev/workspace` | Where agent clones + test envs live. **Use a path with no spaces** — some repos' tests assert on rendered file paths and fail spuriously otherwise. |
| `CLAUDE_MAX_BUDGET_USD` | `2.0` | Per-run spend cap for the Claude CLI. |

To open real PRs: set `DEMO_MODE=false`, authenticate the `gh` CLI (or connect a
GitHub token per user from the UI), and point it at a repo you own.

## Optional Jira integration

Leave blank to disable. When set, approved tickets can be pushed to a Jira project.

| Variable | Purpose |
|---|---|
| `JIRA_BASE_URL` | Your Jira Cloud site, e.g. `https://acme.atlassian.net`. |
| `JIRA_EMAIL` | Account email for API auth. |
| `JIRA_API_TOKEN` | Jira API token. |
| `JIRA_PROJECT_KEY` | Target project key. |

## Secrets & storage

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLite by default; anchored to an absolute path so the launch directory doesn't matter. |
| `AUTODEV_SECRET_KEY` | Passphrase for Fernet encryption of stored secrets. If unset, a key file (`.secret.key`, chmod 600) is generated at the repo root. |
| `SEED_ON_STARTUP` | Seed demo data on first boot (off by default). |

> If `AUTODEV_SECRET_KEY` and `.secret.key` are both lost, previously encrypted
> settings become unreadable (they're treated as unset, not fatal) — you'll just
> re-enter the affected API keys.

The complete, commented list lives in [`.env.example`](../.env.example).

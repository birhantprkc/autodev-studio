<div align="center">

# AutoDev Studio

**An autonomous, multi-agent SDLC harness.**

Point it at a Git repo, describe a feature in plain English, and a team of AI agents
scopes it, writes the code, tests it, reviews it, and opens a pull request — grounded
in a knowledge base of that repo, with a live board and full cost accounting.

[![CI](https://github.com/krishagarwal314/autodev-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/krishagarwal314/autodev-studio/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## Why this exists

A single "write me code" prompt sends a model into a cold repo to grep around, burn
tokens, and guess. AutoDev Studio instead builds a **knowledge base of the target
repository once**, then feeds each agent a scoped, file-cited slice of it. The result
is cheaper and better-localized changes — and on hard-to-find tasks, it beats a plain
`claude -p` run outright.

### It measurably beats a cold agent on hard tasks

I ran the pipeline head-to-head against plain Claude Code across **four repos of
increasing size (2k → 82k LOC) and twelve tasks**, giving both systems the identical
plain-English request. The full method and per-task numbers are in
**[benchmarks/kb-vs-claude-code.md](benchmarks/kb-vs-claude-code.md)**. The headline:

- **What drives baseline cost is *localizability*, not repo size.** A cross-cutting
  bug can cost a cold agent 3.5× the tokens of a greppable one in the *same* repo. On
  one 82k-LOC repo the cold baseline spent **$6.83 over 207 turns** hunting a single
  cache-decorator bug.
- **The knowledge base reliably buys a cheap, accurate localization step**, and that
  saving grows with how hard the task is to find. With pinned files injected into the
  Dev prompt, the tuned pipeline came in **−26% to −45%** on well-localized tasks and
  **−36% / −75%** on the two hardest-to-localize bugs.
- **It's not a free lunch, and the report says so.** The five gated stages have a
  fixed cost floor that hurts on cheap greppable edits, and a cheap "win" on a hard
  bug can hide a fix that only covers part of the root cause. The write-up is honest
  about every loss.

> The benchmark is the interesting part of this project — read it before the code.

---

## Features

| | Feature |
|---|---|
| **Repo knowledge base** | Clone → chunk → embed any Git repo into a local vector DB; agents query it for grounded context. Free local embeddings ([fastembed](https://github.com/qdrant/fastembed) `bge-small`) + embedded Qdrant, with a pure-Python TF-IDF fallback that needs no model. |
| **Structured multi-view knowledge** | Beyond chunks, each repo is statically analyzed (`ast`) into interpreted *views* — architecture, modules, features, workflows, entry points, domain concepts, rules, integrations. Facts come from code; only interpretation comes from the LLM. |
| **Agentic PM scoping** | A PM agent runs a Socratic clarify-loop, hunting for ambiguity before locking a scope, then drafts concrete engineering tickets. |
| **Human approval gate** | No agent touches code until a human approves the ticket (optionally pushed to Jira). |
| **Dev → QA → Review → PR pipeline** | Runs on an isolated branch of a cloned working copy; opens a real PR via `gh`. |
| **Cross-provider review** | The model that *writes* the code is deliberately a different family than the one that *reviews* it, to decorrelate blind spots. |
| **Bounded revise loop** | QA/Review feedback is fed back to Dev for up to N rounds, with conservative verdict parsing. |
| **Live board + cost meter** | Kanban lanes, streamed agent logs, and real token/cost breakdowns per ticket, scope, and agent. |
| **Auth + roles** | Cookie-session login with `admin` / `member` / `viewer` roles enforced server-side. |
| **Runtime settings** | API keys, per-stage models/providers, loop bounds, demo mode, and Jira — all editable live from the UI, no restart. Keys encrypted at rest. |
| **Zero-CDN frontend** | Hand-rolled design system (dark/light), inline SVG icons, system fonts — works fully offline. |

---

## Quick start

### Prerequisites
- **Python 3.11+** and **git**
- At least one LLM key (an `OPENAI_API_KEY`, or any OpenAI-compatible provider — the
  defaults target Groq's free tier)
- *Optional:* the **Claude Code CLI** authenticated (best Dev/Review quality; falls
  back to the OpenAI path without it) · the **`gh`** CLI (only to open real PRs)

### Option A — one command

```bash
cp .env.example .env       # add an API key
./run.sh                   # creates a venv, installs, starts on :8017
```

### Option B — Docker

```bash
OPENAI_API_KEY=sk-... docker compose up --build
```

### Option C — manual (installable package)

```bash
pip install -e ".[semantic]"   # omit [semantic] to use the TF-IDF fallback
autodev                        # starts the server (see `autodev --help`)
```

Then open **http://localhost:8017** (API docs at `/docs`). Sign in as `admin` with
the one-time password printed in the server log on first boot (or set `ADMIN_PASSWORD`
in `.env`).

### Try it
1. **Knowledge** → paste a public Git URL → *Build knowledge base* → *Set active*.
2. **Scope Chat** → describe a feature; answer the PM agent until it locks the scope →
   *Draft tickets*.
3. **Board** → approve a ticket → *Run* → watch Dev → QA → Review → PR stream live.
4. **Settings** → set keys, pick a model per stage, tune the revise loop — all live.
5. **Demo mode is on by default**, so the PR stage is a dry-run. Turn it off (and
   authenticate `gh`) only for a repo you own.

> The knowledge base uses free local embeddings in an embedded Qdrant DB — no API key,
> no Docker. The first ingest downloads a ~90 MB model once. Set `RAG_EMBEDDINGS=tfidf`
> to skip embeddings entirely.

---

## How it works

```
ingest repo → build knowledge base
  → PM agent clarifies + drafts tickets → human approval (+ optional Jira)
  → Dev writes code on an agent/<key> branch
  → QA runs tests + reviews          ┐
  → Review checks diff vs criteria    ├─ revise loop ×N on failure
  → PR pushes + opens a real PR       ┘
  → human merges
```

The HTTP request that starts a run returns immediately; the pipeline executes on a
worker thread and streams progress through the database that the UI polls.

Full write-ups:
- **[docs/architecture.md](docs/architecture.md)** — components, the pipeline, the
  request flow, cross-provider review, and the revise loop.
- **[docs/knowledge-base.md](docs/knowledge-base.md)** — the RAG index and the
  structured views, and how they degrade gracefully.
- **[docs/configuration.md](docs/configuration.md)** — every environment variable and
  runtime setting.

---

## Development

```bash
pip install -e ".[dev]"    # pytest, pytest-cov, ruff
pytest                     # run the suite (no network; the LLM boundary is mocked)
ruff check backend/app tests
```

Tests cover the security-critical and pipeline-logic paths — encryption at rest,
password hashing and the bootstrap admin, role-based access control, runtime-settings
validation and masking, the revise-loop verdict parsing, and knowledge-base helpers.
CI runs lint + tests on Python 3.11 and 3.12.

See **[CONTRIBUTING.md](CONTRIBUTING.md)** to get started and **[SECURITY.md](SECURITY.md)**
to report a vulnerability.

---

## Tech stack

| Area | Stack |
|---|---|
| Backend | Python, **FastAPI**, **SQLModel** (SQLAlchemy + Pydantic), Uvicorn |
| Database | SQLite |
| Frontend | Jinja2 SSR, vanilla JS, hand-rolled CSS design system (dark/light, zero CDN) |
| Auth | Cookie sessions, PBKDF2 (stdlib), role-based access control |
| LLMs | Claude Code CLI (headless, streaming JSON), Anthropic Messages API, any OpenAI-compatible Chat Completions endpoint |
| Knowledge base | fastembed (`bge-small`) + embedded Qdrant, TF-IDF fallback; static `ast` analysis → per-domain views |
| Integrations | GitHub `gh` CLI, `git`, Jira Cloud REST v3 (optional) |

---

## Roadmap

- Swap the in-process thread pool for a durable queue (Celery / RQ / Arq).
- WebSocket/SSE streaming to the UI instead of polling.
- Sandboxed test execution (containers) for untrusted repos.
- A pluggable agent graph for richer branching/parallelism.
- Workspace-scoped multi-tenancy on top of the existing roles.

---

<div align="center">
<sub>MIT Licensed · built with FastAPI, Claude, and an OpenAI-compatible stack</sub>
</div>

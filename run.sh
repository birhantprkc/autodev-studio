#!/usr/bin/env bash
# ============================================================================
# AutoDev Studio — one-command local launcher.
#   ./run.sh               create a venv, install, start on :8017
#   PORT=9000 ./run.sh     start on a different port
#   HOST=0.0.0.0 ./run.sh  expose on the network (only behind a trusted network)
#
# The app is self-contained (built-in local RAG); no external services needed.
# Copy .env.example to .env and add an API key first.
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-8017}"
HOST="${HOST:-127.0.0.1}"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || { echo "error: no python on PATH" >&2; exit 1; }

"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "error: Python 3.11+ is required (found $("$PY" -V 2>&1))" >&2; exit 1; }

command -v git >/dev/null 2>&1 \
  || { echo "error: git is required — the agents clone and branch working copies" >&2; exit 1; }

VENV="$ROOT/backend/.venv"
if [ ! -d "$VENV" ]; then
  echo "→ creating virtualenv…"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
if [ -f "$VENV/bin/activate" ]; then source "$VENV/bin/activate"; else source "$VENV/Scripts/activate"; fi

echo "→ installing…"
python -m pip install --quiet --upgrade pip
# [semantic] pulls fastembed + qdrant for local embeddings. If no wheels exist
# for your platform, the core install still works — the knowledge base falls
# back to its pure-Python TF-IDF index.
python -m pip install --quiet -e "$ROOT[semantic]" || {
  echo "→ semantic extras unavailable; installing core only (TF-IDF fallback)"
  python -m pip install --quiet -e "$ROOT"
}

if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ]; then
  echo "→ no .env found — copied .env.example; add an API key to it"
  cp "$ROOT/.env.example" "$ROOT/.env"
fi

echo "→ AutoDev Studio → http://${HOST}:${PORT}  (API docs: /docs)"
exec autodev --host "$HOST" --port "$PORT"

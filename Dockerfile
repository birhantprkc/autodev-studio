# AutoDev Studio — self-contained image (API + UI + built-in local RAG).
FROM python:3.12-slim

# git is required (the agents clone/branch/diff working copies); gh is optional
# and only needed to open real PRs when DEMO_MODE=false.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The package lives under backend/, so the source must be present for the
# install to resolve it — dependencies and code share one layer.
COPY pyproject.toml README.md LICENSE ./
COPY backend ./backend
RUN pip install --no-cache-dir .

# Run as a non-root user: this app executes agent-authored code from cloned
# repos, so it should not own the container.
RUN useradd --create-home --uid 10001 autodev \
    && mkdir -p /data /workspace \
    && chown -R autodev:autodev /app /data /workspace
USER autodev

ENV PYTHONUNBUFFERED=1 \
    RAG_BACKEND=local \
    RAG_EMBEDDINGS=tfidf \
    DEMO_MODE=true \
    SEED_ON_STARTUP=false \
    REPOS_DIR=/workspace \
    HOST=0.0.0.0 \
    PORT=8017

EXPOSE 8017
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD ["sh", "-c", "curl -fsS http://127.0.0.1:${PORT}/health || exit 1"]

CMD ["autodev"]

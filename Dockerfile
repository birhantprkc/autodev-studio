# CodeJury — self-contained image (API + UI + bundled code-graph engine).
FROM python:3.12-slim

# git is required (the agents clone/branch/diff working copies); gh is optional
# and only needed to open real PRs when DEMO_MODE=false.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The knowledge base's code-graph engine: a single static binary. Bundled here
# so the container's KB runs at full fidelity out of the box. Pinned + checksum
# would be verified in a hardened build; the portable linux-amd64 build works
# across glibc variants. If the download fails, the app still runs (the KB
# degrades to the symbol-map + git-grep fallback), so don't fail the build.
ARG CMM_VERSION=v0.9.0
ARG TARGETARCH=amd64
RUN curl -fsSL -o /tmp/cmm.tgz \
        "https://github.com/DeusData/codebase-memory-mcp/releases/download/${CMM_VERSION}/codebase-memory-mcp-linux-${TARGETARCH}-portable.tar.gz" \
    && tar -xzf /tmp/cmm.tgz -C /tmp \
    && mv /tmp/codebase-memory-mcp /usr/local/bin/codebase-memory-mcp \
    && chmod +x /usr/local/bin/codebase-memory-mcp \
    && rm -f /tmp/cmm.tgz \
    && /usr/local/bin/codebase-memory-mcp --version \
    || echo "WARNING: codebase-memory-mcp not installed — KB will use the symbol-map fallback"

WORKDIR /app

# The package lives under backend/, so the source must be present for the
# install to resolve it — dependencies and code share one layer.
COPY pyproject.toml README.md LICENSE ./
COPY backend ./backend
RUN pip install --no-cache-dir .

# Run as a non-root user: this app executes agent-authored code from cloned
# repos, so it should not own the container.
RUN useradd --create-home --uid 10001 codejury \
    && mkdir -p /data /workspace \
    && chown -R codejury:codejury /app /data /workspace
USER codejury

ENV PYTHONUNBUFFERED=1 \
    RAG_BACKEND=local \
    DEMO_MODE=true \
    SEED_ON_STARTUP=false \
    REPOS_DIR=/workspace \
    HOST=0.0.0.0 \
    PORT=8017

EXPOSE 8017
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD ["sh", "-c", "curl -fsS http://127.0.0.1:${PORT}/health || exit 1"]

# The container serves the web UI: a detached container has no terminal for the
# interactive shell, and the healthcheck above needs an HTTP endpoint. To use
# the shell in Docker, attach one:  docker compose run --rm codejury codejury
CMD ["codejury", "serve"]

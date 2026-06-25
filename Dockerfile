FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

# Layer 1: resolve project deps for cache stability across source changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

# Layer 2: install the project itself + the three sibling MCP servers
# cobalt-grinding spawns as children. README.md is part of pyproject's
# metadata (project.readme); the first uv sync skipped reading it, but
# the second sync (with --install-project) needs it. Bundling the
# children keeps the "out of the box demo" working — `docker run …`
# starts a daemon that has a Smalt + lab notebook + code parser ready
# without extra installs. Operators who want to swap or omit a child
# can override the [mcp.clients.*] config or remove its env entry.
COPY README.md ./
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev && \
    uv pip install --system smalt-mcp ebony-enriching deco-assaying

# Container env. The three substrate / capability dirs land under
# /data, mounted via VOLUME. cobalt-grinding's config layer reads
# `COBALT_GRINDING_SMALT_DIR` / `_EBONY_DIR` / `_COBALT_GRINDING_DIR`
# and injects them into the matching child's env at startup.
#
# `MCP_HOST=0.0.0.0` is the bind address for the HTTP transport —
# 127.0.0.1 (the default) wouldn't accept connections from outside
# the container.
ENV PYTHONUNBUFFERED=1 \
    COBALT_GRINDING_SMALT_DIR=/data/smalt \
    COBALT_GRINDING_EBONY_DIR=/data/ebony \
    COBALT_GRINDING_COBALT_GRINDING_DIR=/data/cobalt_grinding \
    COBALT_GRINDING_MCP_HOST=0.0.0.0 \
    COBALT_GRINDING_MCP_PORT=7474

EXPOSE 7474
VOLUME ["/data"]

# `ANTHROPIC_API_KEY` must be passed at run time
# (`docker run -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY …`). Without it
# the daemon still starts and substrate-bootstraps; cognitive-skill
# tools (wiki.ask, etc.) will return a clear error at call time.
CMD ["uv", "run", "cobalt-grinding"]

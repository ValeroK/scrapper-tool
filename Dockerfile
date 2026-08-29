# syntax=docker/dockerfile:1
# scrapper-tool — DEFAULT image (Pattern A-E in one container).
#
# Bundles:
#   - Python 3.13 + uv
#   - scrapper-tool[agent,full]   (MCP + Pattern D Scrapling + Pattern E
#                                  Camoufox + Patchright + Crawl4AI +
#                                  browser-use + Tier 1 captcha solver)
#   - Patched Chromium for Patchright + Playwright
#   - System libs Playwright/Camoufox need
#
# WHY THIS IS THE DEFAULT: "all capabilities enabled in one container".
# Scrapling ([hostile]) pins lxml>=6, Crawl4AI ([llm-agent]) pins
# lxml~=5.3, so they normally don't coexist. The `[full]` extra is
# enabled by an lxml override declared in pyproject.toml's `[tool.uv]`
# section, which forces lxml>=6.0.3 and lets both packages resolve.
# The override is safe because both libraries actually use the lxml
# HTML/XPath surface that is stable across 5/6.
#
# Image size: ~1.6 GB. If you don't need Pattern D, use the lighter
# Dockerfile.slim (~1.2 GB, Pattern E only). If you DON'T need Pattern E
# (no LLM, just Scrapling) use Dockerfile.hostile (~1.0 GB).
#
# Build:
#   docker build -t scrapper-tool .
# Or via compose (default service):
#   docker compose build scrapper-tool

FROM python:3.13-slim-bookworm AS builder

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_LINK_MODE=copy

# Copy manifests only — this layer is cached until pyproject.toml or uv.lock changes.
COPY pyproject.toml uv.lock README.md ./
# `[full]` pulls hostile + llm-agent + turnstile-solver + agent.
# `[http]` pulls FastAPI + uvicorn for the REST sidecar (`scrapper-tool-serve`).
# The lxml override in pyproject.toml's [tool.uv] section makes this resolve.
# --no-install-project: install only dependencies, not the project itself.
# This layer survives source-code-only changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra dev --extra agent --extra full --extra http

# Copy source and install the project (fast — all packages already in the layer above).
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --extra agent --extra full --extra http

# ---- Stage 2: runtime --------------------------------------------------------

FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="scrapper-tool" \
      org.opencontainers.image.description="All five patterns in one image — A/B/C HTTP, D Scrapling, E Camoufox + Crawl4AI + browser-use, MCP server. Via lxml override." \
      org.opencontainers.image.source="https://github.com/ValeroK/scrapper-tool" \
      org.opencontainers.image.licenses="MIT"

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcairo-gobject2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libgbm1 \
        libgdk-pixbuf-2.0-0 \
        libglib2.0-0 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxcursor1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xdg-utils \
        xvfb

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
# The skill is the tool's own manual and is served over HTTP and MCP, so it has
# to exist inside the image. It previously did not: `skills/` was in the sdist
# but never copied here, so every containerised deployment served a sidecar that
# could not explain itself.
COPY skills/ /app/skills/
COPY pyproject.toml README.md ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCRAPPER_TOOL_AGENT_BROWSER=patchright \
    SCRAPPER_TOOL_AGENT_HEADFUL=0 \
    # Default LLM endpoint = host machine's port 11434 (Ollama default).
    # Override at runtime to point at LM Studio, llama.cpp (8080), vLLM
    # (8000), or a remote server. For LM Studio set LLM=openai_compat and
    # OLLAMA_URL=http://host.docker.internal:<lmstudio-port> (the backend
    # appends /v1). The container does NOT bundle an LLM — bring your own.
    SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:11434

# Install OS-level Playwright runtime deps as root — needs apt-get.
RUN /app/.venv/bin/patchright install-deps chromium && \
    /app/.venv/bin/playwright install-deps chromium

RUN useradd --uid 1000 --create-home scrapper && chown -R scrapper /app
USER scrapper

# Install browser BINARIES as the runtime user so they land in
# /home/scrapper/.cache/ms-playwright (where Patchright/Playwright look at
# launch time). Three are needed:
#   - Patchright Chromium → Pattern E "fast mode" backend
#   - Playwright Chromium → Crawl4AI default + Scrapling (Pattern D)
#   - Playwright Firefox  → browser-use (E2) + Camoufox stealth profile
#                           (Camoufox is a Firefox fork; browser-use's
#                           default backend is Firefox).
# Without Firefox, /scrape mode=auto fails on E1/E2 escalation with
# "BrowserType.launch: Executable doesn't exist at firefox-*/firefox" —
# while /ready still reports agent_installed=true (false-positive).
RUN /app/.venv/bin/patchright install chromium && \
    /app/.venv/bin/playwright install chromium firefox

# Camoufox stealth-profile download. Defaults to ON in v1.1.2 because
# the published image's `SCRAPPER_TOOL_AGENT_BROWSER` default is
# `patchright`, but the unified-image promise covers Camoufox too — and
# without `camoufox fetch` the stealth Firefox profile is missing,
# leaving Pattern E2 broken when callers flip to camoufox. Override at
# build time with `--build-arg INSTALL_CAMOUFOX=0` for the lighter
# image variant.
ARG INSTALL_CAMOUFOX=1
RUN if [ "$INSTALL_CAMOUFOX" = "1" ]; then /app/.venv/bin/camoufox fetch || true ; fi

# 8000 — default HTTP/SSE / streamable-HTTP MCP port (when transport != stdio).
# 5792 — REST sidecar (`scrapper-tool-serve`). Affiliate-service / non-MCP callers.
# 8080 — reserved for HTTP-based MCP behind a reverse proxy.
EXPOSE 8000 5792 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:5792/health || exit 1

# Default entrypoint: REST sidecar (`scrapper-tool-serve`) on port 5792.
# Exposes /scrape /fetch /extract /browse /ready /health /version /docs.
# This matches what the README + docs/http-sidecar.md treat as the
# primary surface for non-MCP callers.
#
# **Breaking change in v1.1.2** — previous images defaulted to
# `scrapper-tool-mcp` (stdio MCP). MCP-mode users override with
# `entrypoint: ["scrapper-tool-mcp"]` in compose; see docs/mcp.md.
ENTRYPOINT ["scrapper-tool-serve"]

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

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        libffi-dev \
        libssl-dev \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

ENV UV_LINK_MODE=copy
# `[full]` pulls hostile + llm-agent + turnstile-solver + agent.
# The lxml override in pyproject.toml's [tool.uv] section makes this resolve.
RUN uv sync --frozen --extra dev --extra agent --extra full

# ---- Stage 2: runtime --------------------------------------------------------

FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="scrapper-tool" \
      org.opencontainers.image.description="All five patterns in one image — A/B/C HTTP, D Scrapling, E Camoufox + Crawl4AI + browser-use, MCP server. Via lxml override." \
      org.opencontainers.image.source="https://github.com/ValeroK/scrapper-tool" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libexpat1 \
        libgbm1 \
        libglib2.0-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        xdg-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml README.md ./

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCRAPPER_TOOL_AGENT_BROWSER=patchright \
    SCRAPPER_TOOL_AGENT_HEADFUL=0 \
    # Default LLM endpoint = host machine's port 11434 (Ollama default).
    # Override at runtime to point at LM Studio (1234), llama.cpp (8080),
    # vLLM (8000), or a remote server. The container does NOT bundle an
    # LLM — bring your own.
    SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:11434

# Install OS-level Playwright runtime deps as root — needs apt-get.
RUN /app/.venv/bin/patchright install-deps chromium && \
    /app/.venv/bin/playwright install-deps chromium

RUN useradd --uid 1000 --create-home scrapper && chown -R scrapper /app
USER scrapper

# Install browser BINARIES as the runtime user so they land in
# /home/scrapper/.cache/ms-playwright (where Patchright/Playwright look at
# launch time). Both are needed:
#   - Patchright Chromium → Pattern E "fast mode" backend
#   - Playwright Chromium → Crawl4AI default + Scrapling (Pattern D)
RUN /app/.venv/bin/patchright install chromium && \
    /app/.venv/bin/playwright install chromium

# Camoufox download is OPTIONAL (heavy ~300 MB).
ARG INSTALL_CAMOUFOX=0
RUN if [ "$INSTALL_CAMOUFOX" = "1" ]; then /app/.venv/bin/camoufox fetch || true ; fi

# 8000 — default HTTP/SSE / streamable-HTTP MCP port (when transport != stdio).
# 8080 — reserved for HTTP-based MCP behind a reverse proxy.
EXPOSE 8000 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "from scrapper_tool import agent; from scrapper_tool.patterns import d; from scrapper_tool.agent.types import AgentConfig; AgentConfig.from_env()" || exit 1

# Default entrypoint: stdio MCP server. All six tools are wired
# (fetch_with_ladder, extract_product, extract_microdata_price, canary,
# agent_extract, agent_browse).
ENTRYPOINT ["scrapper-tool-mcp"]

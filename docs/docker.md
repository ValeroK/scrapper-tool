<!-- Moved out of README.md on 2026-08-27. The README had grown to 839 lines by
carrying full reference material for Docker, MCP, the sidecar and every setting;
this page is the Docker half of that, verbatim, so nothing was lost in the trim. -->

# Running scrapper-tool in Docker

The repository ships **one** image — `Dockerfile` — that bundles **all five
patterns** (A/B/C/D/E + MCP server): Scrapling, Camoufox-ready, Patchright,
Crawl4AI, browser-use, captcha solvers. Built on the `[full]` extra.

The image **does NOT bundle an LLM**. You bring your own — Ollama, LM Studio,
llama.cpp, vLLM — running on the host (or a remote server) and the container
talks to it over `host.docker.internal` (Mac/Windows Docker Desktop maps this
natively; on Linux the compose file declares `extra_hosts`).

### One-liner — assuming Ollama on host

```bash
ollama pull qwen3-vl:8b                           # one-time on the host
docker compose run --rm scrapper-tool python -c "
import asyncio
from scrapper_tool.agent import agent_extract
print(asyncio.run(agent_extract(
    'https://quotes.toscrape.com/',
    schema={'type':'object','properties':{'quotes':{'type':'array'}}},
)))
"
```

The container resolves `SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:11434`
by default. Override in `.env` or environment to point elsewhere — see the
[external LLM section below](#external-llms-lm-studio-llamacpp-vllm-remote-ollama).

### What's in the image

| Capability | Status |
|------------|--------|
| Pattern A (JSON API), B (embedded JSON), C (CSS / microdata) | ✅ always |
| Pattern D (Scrapling hostile-site fetcher) | ✅ pre-installed |
| Pattern E1 (`agent_extract`) | ✅ pre-installed |
| Pattern E2 (`agent_browse`) | ✅ pre-installed |
| Browser: Patchright (Pattern E "fast mode") | ✅ pre-installed |
| Browser: Playwright Chromium (Pattern D Scrapling) | ✅ pre-installed |
| Browser: Camoufox (Pattern E best-stealth) | optional via `--build-arg INSTALL_CAMOUFOX=1` (+300 MB) |
| Browser: Obscura (experimental, lightweight CDP sidecar) | run `obscura serve` and set `SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL` (see `docker-compose.yml`) |
| LLM: external Ollama / LM Studio / llama.cpp / vLLM | ✅ via `host.docker.internal` (see below). The image does NOT bundle an LLM. |
| Captcha Tier 0 (Camoufox auto-pass) | ✅ when `INSTALL_CAMOUFOX=1` |
| Captcha Tier 1 (Theyka) | ✅ pre-installed |
| Captcha Tier 2 (CapSolver / NopeCHA / 2Captcha) | ✅ via env key |
| MCP server (stdio JSON-RPC) | ✅ via the `scrapper-tool` compose service (which sets `entrypoint: ["scrapper-tool-mcp"]`) |
| Canary CLI (`scrapper-tool`) | ✅ |

#### Why this works — the `[full]` extra and the lxml override

Scrapling pins `lxml>=6.0.3` and Crawl4AI pins `lxml~=5.3`. These are
**conservative pins**, not real API breakage — both libraries use the stable
`lxml.html` / XPath surface that's compatible across lxml 5/6.
`pyproject.toml` declares `[tool.uv] override-dependencies = ["lxml>=6.0.3"]`,
which forces a single resolved lxml across both packages. Verified in CI:
238 tests pass with both extras installed simultaneously.

If you prefer plain pip (which doesn't honor `[tool.uv]` overrides), use uv
instead, or pass `pip install --constraint constraints.txt scrapper-tool[full]`
with `lxml>=6.0.3` in `constraints.txt`.

### Pull the published image

Tagged releases are published to GitHub Container Registry. Pull the latest:

```bash
docker pull ghcr.io/valerok/scrapper-tool:latest
# or pin to a specific version
docker pull ghcr.io/valerok/scrapper-tool:1.0.0
```

Tags published per release: `<major>.<minor>.<patch>`, `<major>.<minor>`, and
`latest` (only on non-prerelease tags).

### Build options (local / fork)

```bash
# All five patterns in one image (~1.6 GB).
docker build -t scrapper-tool .
# Or via compose: docker compose build scrapper-tool

# Plus Camoufox baked in (~+300 MB; highest-stealth backend).
docker build --build-arg INSTALL_CAMOUFOX=1 -t scrapper-tool:camoufox .
```

### External LLMs (LM Studio, llama.cpp, vLLM, remote Ollama)

The image talks to whichever LLM server you run, on the host or remotely.
Set the right `SCRAPPER_TOOL_AGENT_*` env vars in your `.env` next to
`docker-compose.yml`:

| Server | `SCRAPPER_TOOL_AGENT_LLM` | `SCRAPPER_TOOL_AGENT_OLLAMA_URL` |
|--------|---------------------------|----------------------------------|
| Ollama on host (default) | `ollama` | `http://host.docker.internal:11434` |
| LM Studio on host | `openai_compat` | `http://host.docker.internal:1234` |
| llama.cpp `server` on host | `llama_cpp` | `http://host.docker.internal:8080` |
| vLLM on host | `vllm` | `http://host.docker.internal:8000` |
| Remote Ollama / OpenAI-compat | `ollama` / `openai_compat` | `https://my-llm.example/v1` etc. |

LM Studio example:

1. LM Studio → Developer / Local Server tab → Start Server (port 1234 by default).
2. Note the model name shown there (e.g. `qwen3-vl-8b-instruct`).
3. `.env`:
   ```env
   SCRAPPER_TOOL_AGENT_LLM=openai_compat
   SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:1234
   SCRAPPER_TOOL_AGENT_MODEL=qwen3-vl-8b-instruct
   ```
4. `docker compose run --rm -T scrapper-tool`.

The compose file already declares `extra_hosts: ["host.docker.internal:host-gateway"]`
so `host.docker.internal` resolves on Linux too (Mac/Windows Docker Desktop
maps it natively).

### Run as MCP server in Docker

The image's default entrypoint is `scrapper-tool-serve` (the REST sidecar) since
v1.1.2, so MCP mode is selected by the compose service rather than inherited:
the `scrapper-tool` service declares `entrypoint: ["scrapper-tool-mcp"]`. Wire
your MCP client to invoke `docker compose run --rm -T scrapper-tool` and you're
done — see the JSON example above. The `-T` flag keeps stdio attached cleanly.

If you run the image directly rather than through compose, pass the entrypoint
yourself:

```bash
docker run --rm -i --entrypoint scrapper-tool-mcp scrapper-tool:latest
```

### Live integration tests inside Docker

```bash
docker compose --profile live up canary    # runs tests/integration/test_agent_live.py
```


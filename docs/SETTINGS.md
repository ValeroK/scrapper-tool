# Settings reference

Every knob in `scrapper-tool` is overridable via:

1. **Code** — pass `AgentConfig(...)` or kwargs to `agent_extract` / `agent_browse`.
2. **Environment variable** — `SCRAPPER_TOOL_*` (loaded by `AgentConfig.from_env()`).
3. **`.env` file** — see [`.env.example`](../.env.example). The library does NOT auto-load `.env` — wire it in via `uv run --env-file .env`, `docker compose`, or `python-dotenv`.

Resolution precedence (highest first): explicit kwargs → `config=AgentConfig(...)` → env vars → built-in defaults.

This page is the canonical reference. If a setting isn't documented here, it isn't a public knob.

## Where do I put settings when using as a library?

Pick one of three places. They compose; you can use all three.

| Location | Best for | How |
|----------|----------|-----|
| **OS environment variables** | Deployment, secrets management, 12-factor apps | `export SCRAPPER_TOOL_AGENT_*=...` then call functions normally. |
| **`.env` file + `python-dotenv`** | Local development | `load_dotenv()` BEFORE importing `scrapper_tool`. Or use `uv run --env-file .env ...` / `docker compose` (which loads automatically). |
| **`AgentConfig(...)` Python object** | Tests, multi-tenant apps that vary config per call | `agent_extract(..., config=AgentConfig(model="..."))`. Per-call kwargs override the config. |

`scrapper-tool` itself does **not** auto-load `.env` — that's the calling app's
job, so the library stays predictable. The bundled `docker-compose.yml` does
auto-load `.env` (compose's standard behavior).

### Code examples

```python
# A) Pure env-driven (CI, production, k8s):
import asyncio
from scrapper_tool.agent import agent_extract
result = asyncio.run(agent_extract(url, schema={"type": "object"}))

# B) .env-driven (local dev):
from dotenv import load_dotenv
load_dotenv()                                  # MUST run before imports below
from scrapper_tool.agent import agent_extract  # noqa: E402
# ...

# C) Pure-code (tests, programmatic):
from scrapper_tool.agent import AgentConfig, agent_extract
cfg = AgentConfig(browser="patchright", model="qwen3-coder:30b")
result = await agent_extract(url, schema=..., config=cfg)

# Per-call overrides win over (cfg / env / defaults):
result = await agent_extract(url, schema=..., config=cfg, headful=True)
```

---

## Pattern E — LLM-agent layer (v1.0.0+)

These settings drive `agent_extract`, `agent_browse`, and `agent_session`.

### Browser backend

| Field | Env var | Default | Choices | Notes |
|-------|---------|---------|---------|-------|
| `browser` | `SCRAPPER_TOOL_AGENT_BROWSER` | `camoufox` | `camoufox` / `patchright` / `zendriver` / `scrapling` / `botasaurus` | Camoufox = best stealth, ~200 MB RAM, ~42 s/bypass. Patchright = fast mode, weaker stealth. |
| `fingerprint` | `SCRAPPER_TOOL_AGENT_FINGERPRINT` | `browserforge` | `browserforge` / `none` | Per-session UA/Accept/Canvas/WebGL randomization. Camoufox ignores this (has its own). |
| `behavior` | `SCRAPPER_TOOL_AGENT_BEHAVIOR` | `humanlike` | `humanlike` / `fast` / `off` | Mouse-path bezier + jittered keystroke timing. Defeats DataDome behavior detection. |
| `headful` | `SCRAPPER_TOOL_AGENT_HEADFUL` | `0` (false) | `0`/`1`/`true`/`false`/`yes`/`no`/`on`/`off` | Show the browser window. Useful for debugging. |
| `proxy` | `SCRAPPER_TOOL_AGENT_PROXY` | unset | URL string | `http://user:pass@host:port` or `socks5://host:port`. Forwarded to the browser. |

#### When to switch backends

| Target | Use |
|--------|-----|
| Cloudflare Enterprise / DataDome / Akamai Bot Manager v4 / Imperva | `camoufox` (default) |
| Lightly-protected SPAs, batch throughput, CI runs | `patchright` |
| Sites that detect Playwright API itself | `zendriver` (CDP-direct, requires `[zendriver-backend]` extra) |
| You already installed `[hostile]` and don't want another browser | `scrapling` |
| Decorator-style workflow with humanlike behavior emulation | `botasaurus` (requires `[botasaurus-backend]` extra) |

### LLM backend

| Field | Env var | Default | Choices |
|-------|---------|---------|---------|
| `llm` | `SCRAPPER_TOOL_AGENT_LLM` | `ollama` | `ollama` / `openai_compat` / `llama_cpp` / `vllm` |
| `model` | `SCRAPPER_TOOL_AGENT_MODEL` | `qwen3-vl:8b` | any tag pulled by your LLM server |
| `ollama_url` | `SCRAPPER_TOOL_AGENT_OLLAMA_URL` | `http://localhost:11434` | also serves as base URL for `openai_compat` / `llama_cpp` / `vllm` |

#### Recommended models (local, May 2026)

Pick by VRAM. Qwen3-VL is the current open-source SOTA for agentic UI
grounding + screenshot understanding, which is what the browse mode does.

| Model | VRAM target | Strength | Use case |
|-------|-------------|----------|----------|
| `qwen3-vl:8b` | **16 GB** | Best 8B vision-language for web agents; strong tool calling, 256K context | **Default.** Q4_K_M ~6.1 GB; Q8_0 fits in 16 GB for higher OCR fidelity. |
| `qwen3-vl:4b` | **8 GB** | Same family at smaller scale, fits next to browser + vision encoder overhead | Recommended on 8 GB cards / laptops. Q4_K_M ~3.3 GB. |
| `qwen3-vl:2b` | 4-6 GB | Lightweight fallback | Low-end GPUs / iGPUs. |
| `qwen3-vl:30b` | 20+ GB | MoE A3B — top open-source agent quality | When you have the headroom. |
| `qwen3-coder:30b` | 24 GB | Top-tier function calling, text-only | DOM-heavy E2 flows; vision auto-disabled. |
| `deepseek-v3.2` | very large | Best general reasoning + tool use | Heaviest hardware. |

The library auto-detects vision-capable models by name (`vl`, `vision`, `llava`, `minicpm-v`) and disables image input for text-only models to save tokens.

> Vision models carry a fixed ~1.4 GB encoder overhead in addition to the
> quantized weights. The 8 GB / 16 GB targets above account for that plus
> typical browser RAM and a 4-8K KV cache.

### Run budget

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `max_steps` | `SCRAPPER_TOOL_AGENT_MAX_STEPS` | `20` | E2 only. Once exhausted, returns `AgentResult(error="no-match")` (does not raise). |
| `timeout_s` | `SCRAPPER_TOOL_AGENT_TIMEOUT_S` | `120` | Hard ceiling. Exceeded → `AgentTimeoutError`. |

### ToS / safety

| Field | Env var | Default | Notes |
|-------|---------|---------|-------|
| `respect_robots` | `SCRAPPER_TOOL_AGENT_RESPECT_ROBOTS` | `1` (true) | When true, fetch `/robots.txt` and refuse if disallowed. |

---

## CAPTCHA solver cascade

Free OSS by default. Escalates to a paid solver only when an API key is configured.

| Field | Env var | Default | Choices |
|-------|---------|---------|---------|
| `captcha_solver` | `SCRAPPER_TOOL_CAPTCHA_SOLVER` | `auto` | `auto` / `camoufox-auto` / `theyka` / `capsolver` / `nopecha` / `twocaptcha` / `none` |
| `captcha_api_key` | `SCRAPPER_TOOL_CAPTCHA_KEY` | unset | Paid-vendor API key. Triggers Tier-2 escalation. |
| `captcha_paid_fallback` | `SCRAPPER_TOOL_CAPTCHA_PAID_FALLBACK` | `capsolver` | `capsolver` / `nopecha` / `twocaptcha` / `none` |
| `captcha_timeout_s` | `SCRAPPER_TOOL_CAPTCHA_TIMEOUT_S` | `120` | Per-solve cap. |

### `auto` cascade order

| Tier | Solver | Cost | Solves |
|------|--------|------|--------|
| 0 | Camoufox auto-pass | $0 | Most CF Turnstile interstitials |
| 1 | [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver) | $0 | CF Turnstile (managed + invisible) |
| 2 | CapSolver / NopeCHA / 2Captcha | paid | hCaptcha, reCAPTCHA v2/v3, Funcaptcha, GeeTest, AWS WAF, DataDome |

Without a key, Tier 2 is skipped — captcha-encountered → `AgentBlockedError("captcha-encountered")`.

> **Legal/ToS warning.** Solving CAPTCHAs may violate the target site's ToS. Use only on sites you own or have written permission to automate.

---

## Install extras

**Recommended SDK install** for all capabilities:

```bash
uv pip install scrapper-tool[full,agent]
```

The default Docker image (`Dockerfile` / `docker compose build scrapper-tool`)
is also the full one — every pattern wired up.

| Extra | What it adds | Mutually exclusive with |
|-------|--------------|-------------------------|
| (none) | Patterns A/B/C — `httpx` + `curl_cffi` + `selectolax` + `extruct`. | — |
| `[agent]` | The MCP server (`scrapper-tool-mcp`). Compatible with everything. | — |
| `[hostile]` | Pattern D — Scrapling + Playwright. Pins `lxml>=6`. | `[llm-agent]` (when installed via plain pip) |
| `[llm-agent]` | Pattern E — Camoufox, Patchright, browser-use, Crawl4AI, Browserforge, langchain-ollama, Pillow. Pins `lxml~=5.3`. | `[hostile]` (when installed via plain pip) |
| `[turnstile-solver]` | Captcha cascade Tier 1 (Theyka). Compatible with `[llm-agent]`. | — |
| **`[full]`** ⭐ | All five patterns: A/B/C/D/E in one environment via uv's `override-dependencies` declaration. | — (uv-only) |
| `[zendriver-backend]` | Adds Zendriver as an alternative Pattern E browser. | — |
| `[botasaurus-backend]` | Adds Botasaurus as an alternative Pattern E browser. | — |
| `[skyvern-backend]` | Reserved for a future Skyvern E2 backend. | — |

`[full]` is a uv-only install path — it relies on `[tool.uv] override-dependencies`
in `pyproject.toml` to coerce both Scrapling and Crawl4AI onto a single
`lxml>=6.0.3`. Plain `pip` doesn't honor that override; with pip, install
`[hostile]` and `[llm-agent]` in separate environments OR pass
`--constraint` with `lxml>=6.0.3` and accept the resolver warning.

## Live test toggles

| Env var | Default | Purpose |
|---------|---------|---------|
| `SCRAPPER_TOOL_LIVE` | unset | Set to `1` to enable Pattern A/B/C live probes (`tests/integration/test_live_probes.py`). |
| `SCRAPPER_TOOL_AGENT` | unset | Set to `1` (with `SCRAPPER_TOOL_LIVE=1`) to enable Pattern E live probes (`tests/integration/test_agent_live.py`). |

---

## Settings NOT covered by env vars

A few power-user knobs are code-only because they don't fit a flat env-var shape:

| Knob | Set via | Purpose |
|------|---------|---------|
| `instruction` (E1) | `agent_extract(..., instruction="...")` | Free-form extraction guidance. |
| `schema` (E1/E2) | function arg | Pydantic class, JSON Schema dict, or natural-language string. |
| `BehaviorPolicy` constructor knobs | `HumanlikePolicy(keystroke_median_ms=..., …)` | Fine-tune timing distributions. |
| `BrowserforgeGenerator(browser=..., os_family=...)` | constructor | Override fingerprint distribution. |

---

## Examples

### Override a single value per call

```python
from scrapper_tool.agent import agent_extract

# Overrides go via **kwargs and merge with env / defaults.
result = await agent_extract(
    "https://example.com",
    schema={"type": "object"},
    model="qwen3-coder:30b",     # override default model
    browser="patchright",         # override default backend
    timeout_s=240,
)
```

### Build a config once, reuse for many calls

```python
from scrapper_tool.agent import AgentConfig, agent_session

cfg = AgentConfig(
    browser="camoufox",
    model="qwen3-vl:8b",
    behavior="humanlike",
    captcha_solver="auto",
    timeout_s=180,
)
async with agent_session(config=cfg) as s:
    a = await s.extract("https://a.example", schema=...)
    b = await s.browse("https://b.example", "log in and ...")
```

### Read everything from env (deployment-friendly)

```python
from scrapper_tool.agent import AgentConfig

cfg = AgentConfig.from_env()   # reads all SCRAPPER_TOOL_* vars
```

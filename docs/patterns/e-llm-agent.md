# Pattern E — LLM-driven scraping for any protected site

Pattern E adds two modes of "scrape any website" power on top of the existing
A → B → C → D ladder, both driven by a **local LLM** (Ollama by default — zero
API cost) running through a **stealth browser** (Camoufox by default).

| Mode | Function | LLM calls / page | When to use |
|------|----------|------------------|-------------|
| **E1** | `agent_extract(url, schema)` | **1** | Default for "scrape this data". Render → markdown → one LLM call. Fast, cheap, reliable. |
| **E2** | `agent_browse(url, instruction)` | 5–20 | Multi-step interactive tasks: login, paginate, dynamic forms, "click load more", conditional UI. |

Both modes reuse the same backend stack: stealth browser + fingerprint + behavior
+ optional captcha solver + LLM.

## Quick start

```bash
pip install scrapper-tool[llm-agent]
camoufox fetch                  # one-time ~300 MB Firefox download
ollama pull qwen3-vl:8b         # default vision-language model (16 GB VRAM)
# or, for 8 GB cards:
# ollama pull qwen3-vl:4b
```

```python
import asyncio
from scrapper_tool.agent import agent_extract

schema = {
    "type": "object",
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "author": {"type": "string"}},
                "required": ["text", "author"],
            },
        }
    },
    "required": ["quotes"],
}

result = asyncio.run(
    agent_extract("https://quotes.toscrape.com/", schema=schema)
)
print(result.data["quotes"])
```

For an interactive flow:

```python
from scrapper_tool.agent import agent_browse

result = asyncio.run(
    agent_browse(
        "https://example.com/login",
        instruction="Log in with username 'demo' and password 'demo123', "
                    "then return the user's email shown on the dashboard.",
    )
)
print(result.data)
```

## Choosing a browser backend

The default is **Camoufox** because it scores ~0% headless detection on 2026
benchmarks (CreepJS, DataDome, CF Turnstile + Interstitial, Imperva,
reCAPTCHA v2/v3, Fingerprint.com, most WAFs). For unprotected or
lightly-protected sites where speed dominates, switch to **Patchright**.

| Backend | Bypass rate | RAM/instance | Per-page latency | Install size | Use when |
|---------|-------------|--------------|------------------|--------------|----------|
| **camoufox** (default) | ~100% on 2026 benchmarks | ~200 MB | ~42 s | ~300 MB | Hard sites (CF Enterprise, DataDome, Akamai). |
| patchright | ~67% reduction | ~120 MB | ~3-8 s | ~250 MB | Speed/throughput on lightly-protected sites. |
| zendriver | ~75% (CF/DataDome/Akamai/CloudFront) | ~80 MB | ~5 s | small | When Patchright leaks AND Playwright API itself is the giveaway. CDP-direct. |
| botasaurus | claims wide coverage | ~150 MB | varies | medium | Decorator-paradigm workflows. |
| scrapling | depends on Pattern D config | ~150 MB | ~30 s | shared with `[hostile]` | Already have `[hostile]` and want to skip another browser dep. |

Configure via env:

```bash
export SCRAPPER_TOOL_AGENT_BROWSER=patchright
```

## Choosing a local LLM (May 2026)

Pick by VRAM. The Qwen3-VL family is the current open-source SOTA for
agentic UI grounding + screenshot understanding — exactly what browse mode
needs.

| Model | VRAM target | Best for |
|-------|-------------|----------|
| `qwen3-vl:8b` (**default**) | **16 GB** | Best 8B VLM for web agents in May 2026. Strong tool calling, 256K context. Q4_K_M ~6.1 GB; Q8_0 fits in 16 GB. |
| `qwen3-vl:4b` | **8 GB** | Recommended on 8 GB cards. Q4_K_M ~3.3 GB; leaves headroom for the vision encoder + browser process. |
| `qwen3-vl:2b` | 4-6 GB | Laptop / iGPU fallback. |
| `qwen3-vl:30b` | 20+ GB | MoE A3B, top open-source agent quality if you have the headroom. |
| `qwen3-coder:30b` | 24 GB | Text-only, top-tier function calling. Use for DOM-only E2 flows. Vision auto-disabled. |
| `deepseek-v3.2` | very large | Best general reasoning + tool use. Heaviest hardware ask. |

Configure via env:

```bash
export SCRAPPER_TOOL_AGENT_MODEL=qwen3-coder:30b
export SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://10.0.0.5:11434
```

## CAPTCHA cascade

The captcha system runs a **2-tier free OSS cascade** by default; only escalates
to a paid solver if `SCRAPPER_TOOL_CAPTCHA_KEY` is set.

| Tier | Solver | Solves | Cost | License |
|------|--------|--------|------|---------|
| 0 | Camoufox auto-pass | Most CF Turnstile interstitials | $0 | MPL-2.0 |
| 1 | [Theyka/Turnstile-Solver](https://github.com/Theyka/Turnstile-Solver) | CF Turnstile (managed + invisible) | $0 | MIT |
| 2 (paid, opt-in) | [CapSolver](https://www.capsolver.com/) | hCaptcha, reCAPTCHA v2/v3, Funcaptcha, GeeTest, AWS WAF, DataDome | ~$0.80–$3 / 1000 | proprietary |
| 2 (paid, opt-in) | NopeCHA | Subset; has free dev tier | low | proprietary |
| 2 (paid, opt-in) | 2Captcha | Broadest incl. complex image | $2-3 / 1000 | proprietary |

```bash
export SCRAPPER_TOOL_CAPTCHA_KEY=sk_capsolver_xxx
export SCRAPPER_TOOL_CAPTCHA_PAID_FALLBACK=capsolver
```

### Honest disclaimer

There is **no** OSS solver in 2026 that matches CapSolver's coverage of hCaptcha
v3 / reCAPTCHA v3 / Funcaptcha / DataDome. The cascade reliably handles CF
Turnstile (the most common 2026 challenge); everything else needs a paid key.

### Future additions (NOT in initial implementation)

These are documented here as user-runnable adjuncts; they are NOT bundled in
`[llm-agent]`. Wire them in yourself if your traffic shape demands them:

- [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) — self-hosted
  Docker proxy that pre-clears CF JS challenges via undetected-chromedriver.
  Reliability against modern Turnstile is dropping but it still helps on older
  CF deployments.
- [Buster](https://github.com/dessant/buster) — browser extension that solves
  reCAPTCHA v2 audio challenges via Whisper. Narrow scope.

### Legal / ToS warning

Solving CAPTCHAs may violate the target site's Terms of Service. Use only on
sites you own or have written permission to automate. The library emits a
one-time runtime warning when a paid solver is first invoked.

## Behavior policy

DataDome and similar 2026 anti-bot systems detect *behavior* (timing, mouse
paths, scroll cadence) rather than just fingerprint. The default
``HumanlikePolicy`` injects:

- Jittered keystroke delays (60-180 ms median, log-normal distribution).
- Bezier-curve mouse trajectories with overshoot + correction.
- Variable scroll cadence (50-300 ms between wheel events).
- Random read-time pauses on page load (300-1500 ms).

Disable for tests / unprotected sites:

```bash
export SCRAPPER_TOOL_AGENT_BEHAVIOR=fast   # or "off"
```

## Fingerprint randomization

For non-Camoufox backends, [Browserforge](https://github.com/daijro/browserforge)
generates per-session UA / Accept / viewport / Canvas / WebGL fingerprints
consistent with a real browser.

```bash
export SCRAPPER_TOOL_AGENT_FINGERPRINT=none   # disable; useful in CI
```

## Hardware sizing

| Hardware | Concurrency budget |
|----------|-------------------|
| 16 GB RAM, no GPU | ~3 Camoufox sessions, ollama on CPU = unusable for 7B |
| 16 GB RAM + 8 GB VRAM | ~3 Camoufox sessions, qwen3-vl:4b @ 5-12 s/action |
| 32 GB RAM + 16 GB VRAM | ~4 Camoufox sessions, qwen3-vl:8b @ 5-15 s/action (default) |
| 32 GB RAM + 24 GB VRAM | ~6 Camoufox sessions, qwen3-coder:30b @ 3-8 s/action |
| 64 GB RAM + 48 GB VRAM | ~12 sessions, full DeepSeek-V3.2 |

Latency:
- E1 typical: 5-10 s per page (Patchright) / 30-60 s (Camoufox).
- E2 typical: 30-300 s per task depending on `max_steps`.

Always reuse the browser via `agent_session()` for batches — Camoufox cold-start
costs ~10 s.

## Errors

| Failure | Exception | Caught by |
|---------|-----------|-----------|
| Ollama unreachable | `AgentLLMError` | `AgentError`, `ScrapingError` |
| Timeout exceeded | `AgentTimeoutError` | `AgentError`, `ScrapingError` |
| Stealth browser still blocked | `AgentBlockedError` | `BlockedError`, `AgentError`, `ScrapingError` |
| Schema mismatch | `AgentResult(error="schema-validation-failed")` | (returned, not raised) |
| Captcha encountered, no solver | `AgentBlockedError("captcha-encountered")` | `BlockedError` |
| Solver vendor failure | `CaptchaSolveError` | `AgentError` |

`AgentBlockedError` deliberately multi-inherits `BlockedError` so existing
`except BlockedError` handlers in consumer code transparently absorb agent-stage
blocks.

## When NOT to use Pattern E

- Sites where Pattern A/B/C succeed — those are 1000× cheaper.
- 2FA / SMS / email-verification flows — out of scope by design.
- Per-request latency-critical workloads — even E1 is 30 s/page floor.

The lib's escalation contract: A → B → C → D → E. Pattern E is the LAST resort,
not the first.

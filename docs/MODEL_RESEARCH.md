# Local LLM model research — Pattern E (web-scraping agent)

> **Last updated:** 2026-05-02
> **Reviewed by:** ValeroK (with Claude Code research assistance)
> **Re-run cadence:** quarterly, or when a new SOTA open-weight VLM ships

This doc captures *why* the current default and recommended models in
[`SETTINGS.md`](SETTINGS.md) and [`patterns/e-llm-agent.md`](patterns/e-llm-agent.md)
were chosen, what alternatives were considered, and how to reproduce the
research. Treat the recommendations as time-bounded — open-weight VLMs
move fast and the right answer in May 2026 will not be the right answer
in November 2026.

## Use-case requirements

Pattern E uses the LLM in two ways:

1. **`agent_browse` (E2)** — multi-step interactive flows via
   [browser-use](https://github.com/browser-use/browser-use). Feeds the
   model a screenshot + DOM and expects:
   - **Vision input** (screenshot understanding, UI grounding)
   - **Tool / function calling** in JSON or Hermes format
   - Reasonable latency (~5-15 s per action target)
2. **`agent_extract` (E1)** — single-call structured-JSON extraction via
   Crawl4AI + LiteLLM. Needs:
   - Strict JSON-schema adherence
   - Decent OCR if the page is image-heavy
   - Long context (full rendered markdown, often 30-100K tokens)

Hard constraints for "best local default":

- **Runnable on Ollama OR LM Studio.** Either: an official Ollama tag,
  a community Ollama tag (e.g. the `blaifa/` namespace), an imported
  GGUF via Ollama's `Modelfile`, or a GGUF that LM Studio loads
  directly. Official first-party tags are *preferred* (cleaner vision-
  projector wiring, no manual TEMPLATE block) but not required.
- **Vision-capable** (the `is_vision_model()` heuristic in
  [llm.py](../src/scrapper_tool/agent/backends/llm.py) gates this).
- **Function/tool calling supported** by browser-use's adapter
  (Hermes-style JSON, native or prompt-engineered).
- **Fits in 8 GB or 16 GB VRAM** including the vision encoder
  (~1.4 GB fixed overhead) plus a 4-8K KV cache, with headroom for the
  browser process (~200 MB Camoufox, ~120 MB Patchright).

## Candidates evaluated + quality ranking (May 2026)

One unified table. **Rank** is pure quality for *our* use case (vision +
UI grounding + JSON tool calling + long context), VRAM ignored — useful
when the user has datacenter hardware or a hosted endpoint. **Fits**
shows the smallest VRAM tier the model fits into at Q4_K_M with the
~1.4 GB vision encoder, KV cache, and browser process accounted for.

Bold rows = our defaults at the 8 GB / 16 GB tiers.

| Rank | Tier | Model | Q4_K_M size | Fits | Context | Vision | Tool calling | Runnable via | Headline benchmarks / notes |
|-----:|:----:|-------|-------------|:----:|---------|--------|--------------|--------------|-----------------------------|
| 1 | S | Holo3-122B-A10B | ~70 GB | datacenter | varies | ✅ | ✅ native | LM Studio GGUF | OSWorld-Verified 78.8% (Apr 2026). Purpose-built desktop/web agent. |
| 2 | S | Holo3-35B-A3B | ~22 GB | 24 GB+ | varies | ✅ | ✅ native | LM Studio GGUF only (no Ollama tag) | OSWorld-Verified **82.6%** — leads the leaderboard. 3B active MoE. |
| 3 | S | Qwen3-VL-235B-A22B-Instruct | 143 GB | datacenter | 256K | ✅ native | ✅ native | Official Ollama (`qwen3-vl:235b`) + cloud | Rivals Gemini 2.5 Pro / GPT-5 on 2D/3D grounding, video, OCR. OSWorld 66.7%. |
| 4 | S | Llama 4 Scout (109B / 17B active) | ~20 GB | 24 GB+ | 10M | ✅ native | ✅ native | Official Ollama (`llama4:scout`) + LM Studio | 17B active MoE. Native early-fusion multimodal. Largest context in class. |
| 5 | A | Qwen3-VL-30B (MoE A3B) | 20 GB | 24 GB+ | 256K | ✅ native | ✅ native | Official Ollama (`qwen3-vl:30b`) | Best Qwen3-VL that fits a single 24 GB card. Same agentic toolkit as 8B at higher quality. |
| 6 | A | Mistral Small 4 (Mar 2026) | ~14-24 GB | 16-24 GB | 128K | ✅ | ✅ native | Official Mistral channels + LM Studio + partial Ollama | Unifies Pixtral + Magistral + Devstral. Best-in-class agentic Mistral, native FC + JSON. |
| 7 | A | Gemma 4 31B | 20 GB | 24 GB+ | 128K | ✅ | ✅ native | Official Ollama (`gemma4:31b`) + LM Studio | Full-fat Gemma 4. MMMU-Pro 76.9%; LiveCodeBench v6 80.0%. |
| 8 | A | **qwen3-vl:8b** ⭐ | **6.1 GB** | **16 GB ✅** | 256K | ✅ native | ✅ Hermes, browser-use validated | Official Ollama + LM Studio GGUF | **16 GB default.** ScreenSpot **94.4%**. Top open-source on OSWorld for size class. Q8_0 also fits 16 GB. |
| 9 | B | gemma4:26b (MoE, 3.8B active) | 18 GB | 20-24 GB | 128K | ✅ | ✅ native | Official Ollama | Lower active params than 31B dense. Strong general agent, weaker dedicated UI grounding. |
| 10 | B | openbmb/minicpm-v4.5:8b | ~5.5 GB | 8 GB | 128K | ✅ (LLaVA-UHD, top OCR) | ⚠️ prompted only | Community Ollama (openbmb namespace) + LM Studio | Best-in-class OCR — beats GPT-4o on OCRBench. OpenCompass 77.2. Tool calling via prompt-engineering, no validated browser-use adapter. |
| 11 | B | InternVL 3.5-8B | ~5-6 GB | 8 GB | 32K | ✅ (paper claims top UI grounding) | ⚠️ prompted only | Community Ollama (`blaifa/InternVL3_5:8b`) + LM Studio GGUF | Strong on ScreenSpot/OSWorld-G per paper; no concrete 8B-vs-8B head-to-head against Qwen3-VL-8B. Shorter context. |
| 12 | B | **qwen3-vl:4b** ⭐ | **3.3 GB** | **8 GB ✅** | 256K | ✅ native | ✅ | Official Ollama + LM Studio GGUF | **8 GB default.** ScreenSpot **92.9%** — small drop from 8B for big VRAM saving. |
| 13 | B | Pixtral 12B (Mistral, Sep 2024) | ~14 GB | 16 GB | 128K | ✅ | ✅ | Official Mistral + LM Studio + community Ollama | Solid, but superseded by Mistral Small 4 (rank 6). Apache-2.0. |
| 14 | C | gemma4:e4b | 9.6 GB | 12-16 GB (tight) | 128K | ✅ | ✅ native | Official Ollama + LM Studio | 4.5B effective MoE for edge. Native FC. Weaker dedicated UI grounding than `qwen3-vl:8b`; tight on 16 GB once vision encoder + browser load. |
| 15 | C | InternVL 3.5-4B | ~3-4 GB | 8 GB | 32K | ✅ | ⚠️ prompted | Community Ollama (`blaifa/`) + LM Studio | Same caveats as 8B at smaller scale. |
| 16 | C | gemma4:e2b | 7.2 GB | 8-12 GB | 128K | ✅ | ✅ native | Official Ollama + LM Studio | 2.3B effective. Tight on 8 GB; weaker than `qwen3-vl:4b` on screenshot tasks. |
| 17 | C | qwen3-vl:2b | 1.9 GB | 4-6 GB | 256K | ✅ | ✅ | Official Ollama | Laptop / iGPU fallback for sub-6 GB hardware. |

**Tier definitions:**
- **S** — frontier-class agent quality. Choose if you have the hardware
  or a hosted endpoint and cost is acceptable.
- **A** — production-grade. Real differences in UI grounding /
  agentic behavior, but no embarrassing failures on common tasks.
- **B** — usable in production with caveats (weaker tool calling,
  shorter context, or weaker UI specifically).
- **C** — resource-constrained deployments only. Expect more retries
  and lower first-attempt success rate.

**What we trade away by picking `qwen3-vl:8b` (rank 8) for the 16 GB
default:** roughly 10-15 percentage points on OSWorld-Verified vs Holo3,
plus the long-context advantage of Llama 4 Scout. For a stealth-browser
scraper where captcha + page render is the bottleneck, that gap usually
just means more retries, not different outcomes.

## Decision

**16 GB VRAM (default): `qwen3-vl:8b`**

- Best published OSWorld + ScreenSpot scores in its size class
- Q4_K_M ~6.1 GB → ~7.5 GB with vision encoder → leaves ~6 GB for KV cache + browser
- Q8_0 still fits 16 GB if the user wants higher OCR fidelity
- Hermes-format tool calling works with browser-use's Ollama adapter
- 256K context handles the largest rendered DOMs we've seen
- Same family as the 8 GB pick → predictable behavior across tiers

**8 GB VRAM: `qwen3-vl:4b`**

- Q4_K_M ~3.3 GB → ~4.7 GB with vision encoder → fits with browser + cache
- ScreenSpot 92.9% vs 94.4% for the 8B — small quality drop, big VRAM saving
- Same tool-calling behavior as the default

### Why not Gemma 4 E4B?

It was the strongest near-miss. `gemma4:e4b` has native function calling,
multimodal input, and Google's day-one Ollama support. But for *this*
workload:

1. **9.6 GB Q4_K_M** is too tight on a 16 GB card once you add the vision
   encoder and Camoufox/Patchright RAM.
2. **No published UI-grounding benchmarks** on ScreenSpot / OSWorld.
   Designed for general on-device assistants, not specifically for
   recognising web UI affordances.
3. **128K context** is half of Qwen3-VL's 256K — hurts on long rendered
   pages.

It is, however, a strong *general-purpose* alternative if you'd rather
trade a bit of UI-grounding accuracy for stronger general reasoning and
coding. Document users can opt in via `SCRAPPER_TOOL_AGENT_MODEL=gemma4:e4b`.

### Why not MiniCPM-V 4.5?

Excellent OCR (built on Qwen3-8B + SigLIP2-400M, beats GPT-4o on
OCRBench), but its tool-calling story is weaker — no validated native
adapter in browser-use, requires prompt-engineered Hermes format. Use it
if the bottleneck is *reading* a page (receipts, scans, dense charts)
rather than *acting* on it.

### Why not InternVL 3.5-8B?

The strongest near-miss after the constraint relax. It has a community
Ollama tag (`blaifa/InternVL3_5:8b`) and clean LM Studio GGUF support, so
"runs on Ollama or LM Studio" is no longer the blocker.

It still loses to `qwen3-vl:8b` because:

1. **No concrete head-to-head 8B-vs-8B benchmark.** The InternVL 3.5
   paper claims strong ScreenSpot / OSWorld-G performance, but the
   number for the 8B variant on ScreenSpot specifically is not
   prominently published. Qwen3-VL-8B has a hard 94.4% ScreenSpot
   number that we can decision against.
2. **Tool calling is prompted, not native** — adds integration work
   in browser-use's adapter (where Qwen3-VL works out of the box).
3. **Shorter native context** (~32K vs Qwen3-VL's 256K) — hurts on
   long rendered DOMs.
4. **Community Ollama tag** means slower upgrade cadence and you'll
   occasionally need to fix the Modelfile TEMPLATE block by hand.

Promote it the moment OpenGVLab publishes an 8B-vs-8B ScreenSpot
number that beats 94.4%, or ships an official `ollama pull` tag.

### Why not Llama 4 / Mistral Small 4 / Holo3?

All three are SOTA-class but their smallest local variants need 20+ GB
VRAM at Q4. Out of budget for the 8/16 GB tiers this doc targets.
Revisit when a sub-12 GB Llama 4 dense variant or sub-16 GB Holo3
distillation ships.

## Cross-checks

This decision was corroborated against two independent search backends:

1. **Claude Code WebSearch + WebFetch** — pulled live tags + benchmarks
   from Ollama and the model cards (sources below).
2. **Perplexity Pro** (`perplexity_research`, `perplexity_ask`) — both
   modes declined to return live Q4_K_M sizes / benchmark numbers in
   the May-2026 session and instead returned an evaluation framework.
   Its surfaced web results corroborated the Qwen3-VL official Ollama
   presence, the Llama 4 Scout > Qwen3-VL on heavier tasks comparison,
   and the ScreenSpot leaderboard methodology — but did not produce
   any candidate that would displace `qwen3-vl:8b/4b` for our
   constraints.

> **Operational note for re-runs:** if Perplexity Research mode again
> refuses to fetch live data, fall back to direct WebFetch on
> `ollama.com/library/<tag>` for tag/size truth + WebSearch for
> benchmark blogs. Don't waste a Perplexity call asking for raw
> numbers — use it for *synthesis* across many sources or for the
> "have I missed any contender" sanity check.

## Sources

- [Qwen3-VL on Ollama (latest = 8b)](https://ollama.com/library/qwen3-vl)
- [Qwen3-VL-4B vs 8B benchmarks & VRAM guide (Codersera)](https://codersera.com/blog/qwen3-vl-4b-vs-qwen3-vl-8b-benchmarks-vram-guide)
- [Gemma 4 E4B on Ollama](https://ollama.com/library/gemma4:e4b)
- [Gemma 4 vision benchmark deep-dive](https://www.gemma4.wiki/benchmark/gemma-4-vision-benchmark)
- [MiniCPM-V 4.5 on Ollama (openbmb namespace)](https://ollama.com/openbmb/minicpm-v4.5)
- [Llama 4 Scout VRAM guide (apxml)](https://apxml.com/models/llama-4-scout)
- [Llama 4 Scout vs Qwen3-VL 235B (AnotherWrapper)](https://anotherwrapper.com/tools/llm-pricing/llama-4-scout/qwen3-vl-235b-a22b-thinking)
- [ScreenSpot leaderboard (llm-stats)](https://llm-stats.com/benchmarks/screenspot)
- [OSWorld-Verified leaderboard (BenchLM)](https://benchlm.ai/benchmarks/osWorldVerified)
- [Steel browser-agent leaderboard](https://leaderboard.steel.dev/)
- [Vision models on Ollama (catalog)](https://ollama.com/search?c=vision)
- [Ollama tool-calling docs](https://docs.ollama.com/capabilities/tool-calling)
- [Bartowski Qwen3-VL-4B GGUF quant sizes (HF)](https://huggingface.co/bartowski/Qwen_Qwen3-VL-4B-Instruct-GGUF)

## Re-running this research

Open-weight VLMs change every quarter. To refresh this doc, paste the
following prompt into a fresh agent session (Claude Code, ChatGPT,
Gemini, etc.) that has web-search access. The prompt is self-contained —
it does not assume the agent has read this repo.

> ⚠️ **Always verify hard claims before acting on them.** The agent will
> cite blog posts and benchmark sites; cross-check the model size, Ollama
> tag, and benchmark scores against the model's official Hugging Face
> card and `ollama.com/library/<name>` before changing the defaults.

### Prompt template

```
I maintain `scrapper-tool`, an open-source web-scraping library. Its
"Pattern E" agent uses a local VLM via Ollama to drive a stealth browser
(Camoufox / Patchright) for screenshot-based UI grounding and structured
JSON extraction. Two entry points:

  - agent_browse: multi-step browser-use loop (needs vision + tool/
    function calling)
  - agent_extract: single-call Crawl4AI extraction to a Pydantic / JSON
    schema (needs strict JSON output, decent OCR, long context)

I need to refresh the recommended local models for two VRAM tiers:
8 GB VRAM and 16 GB VRAM (the 16 GB pick is the default).

Hard constraints:
  1. Must be runnable on Ollama OR LM Studio. Acceptable forms:
     official Ollama tag, community Ollama tag (e.g. `blaifa/`),
     GGUF imported via Ollama Modelfile, or any GGUF that LM Studio
     loads directly. Official first-party tags are preferred (cleaner
     vision-projector wiring) but not required.
  2. Vision-capable (we send PNG screenshots).
  3. Tool / function calling must work with browser-use's adapter,
     either natively or via prompt-engineered Hermes-style JSON.
  4. Q4_K_M quantized weights must fit alongside ~1.4 GB vision encoder
     + ~4-8K KV cache + ~200 MB browser process within the VRAM tier.
  5. Context window ≥ 32K tokens (long rendered DOMs); ≥128K preferred.

Please:
  1. Survey open-weight VLMs released or updated in the last ~6 months.
     Include Qwen3-VL, Gemma 4 E2B/E4B, MiniCPM-V latest, InternVL
     latest, Llama 4 vision variants, Mistral / Pixtral, Holo, and any
     other strong open contenders.
  2. For each, report: Q4_K_M download size, context length, native
     vs prompted tool calling, vision quality, official Ollama tag (if
     any), and headline UI / web-agent benchmarks (ScreenSpot,
     OSWorld-Verified, VisualWebBench, MMMU-Pro).
  3. Pick ONE model for 8 GB VRAM and ONE for 16 GB VRAM that best
     satisfies all five constraints. Justify each pick by referencing
     the benchmark and the constraint, not vibes.
  4. List 2-3 honorable mentions per tier with the specific tradeoff
     (e.g. "stronger OCR but weaker tool calling").
  5. Flag anything I should re-check in 3 months (models on the
     roadmap, ones that almost made it, etc.).

Output format: a markdown decision matrix table + a short rationale per
pick + a "watchlist" section. Cite every benchmark claim with a URL.

Use BOTH backends when available:
  - Direct web search + page fetch (for `ollama.com/library/<tag>` truth
    on download size, latest tag, capabilities — these are the load-
    bearing facts).
  - Perplexity Research / Ask (use for the "did I miss any contender?"
    sanity check and for cross-citation. If Perplexity refuses to
    return live numbers, don't fight it — its synthesis-over-many-
    sources is what's valuable, not raw lookups).
```

### What to update when re-running

When the research output recommends different models, update **all** of:

1. [`src/scrapper_tool/agent/types.py`](../src/scrapper_tool/agent/types.py) — `model: str = "<new-default>"` (twice: dataclass default + `from_env` fallback)
2. [`.env.example`](../.env.example) — `SCRAPPER_TOOL_AGENT_MODEL=` line + the recommendations comment block
3. [`docker-compose.yml`](../docker-compose.yml) — three `SCRAPPER_TOOL_AGENT_MODEL` env defaults + the `ollama pull` example in the header comment
4. [`.github/workflows/live-agent.yml`](../.github/workflows/live-agent.yml) — workflow input `default:`
5. [`README.md`](../README.md) — install snippet, mermaid diagram label, MCP `.env` examples, code samples
6. [`docs/SETTINGS.md`](SETTINGS.md) — recommendations table + default in the env-var table
7. [`docs/patterns/e-llm-agent.md`](patterns/e-llm-agent.md) — quickstart pull command, model table, hardware-sizing table
8. [`docs/agent-integration.md`](agent-integration.md) — MCP `.env` example
9. [`tests/unit/test_agent_types.py`](../tests/unit/test_agent_types.py) — three default-asserting tests
10. [`tests/unit/test_agent_backends.py`](../tests/unit/test_agent_backends.py) — `test_ollama_is_default` assertion (line ~185)
11. Docstrings in [`browse.py`](../src/scrapper_tool/agent/browse.py), [`runner.py`](../src/scrapper_tool/agent/runner.py), [`__init__.py`](../src/scrapper_tool/agent/__init__.py), [`backends/llm.py`](../src/scrapper_tool/agent/backends/llm.py)
12. **This file** — bump the "Last updated" date and rewrite the candidates table

Run `uv run pytest tests/unit/test_agent_types.py tests/unit/test_agent_backends.py -q`
after editing — those two files alone catch ~90% of stale references.

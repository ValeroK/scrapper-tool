# E2 agent framework — keep browser-use, or migrate to Stagehand? (2026)

Spike output for the Stage 8 "E2 hybrid posture" work. **Recommendation: keep
browser-use for now, adopt a deterministic-first posture around it, and
re-evaluate Stagehand for a future dedicated migration.** No code migration in
this effort.

## The problem

E2 (`agent_browse`) hands the *entire* task to an LLM agent loop (browser-use).
Two issues:

1. **Reliability compounds.** AI browser agents run ~70–85% success per step and
   are resilient to UI change, but a 10-step flow at 95%/step is ≈ 60% overall.
   Deterministic Playwright is 99%+ but needs selector upkeep. 2026 best practice
   is **hybrid**: deterministic for the predictable ~80%, AI only for the
   ambiguous ~20%.
2. **browser-use churn.** The library moved 0.5 → 0.13 with breaking API changes
   (the `Browser`/`BrowserConfig` → `BrowserSession`/`BrowserProfile` refactor,
   the LLM-wrapper change that already bit this project). This project pins
   `browser-use>=0.5` and the E2 adapter is defensive (`getattr` everywhere,
   `try/except TypeError`) precisely because of that churn.

## Options considered

| Option | Model | Reliability | API stability | Fit with this stack |
|--------|-------|-------------|---------------|---------------------|
| **browser-use** (current) | Agent owns the whole loop | ~70–85%/step; poor multi-step | Churny (0.5→0.13) | Native Ollama; already integrated; drives our Playwright browser |
| **Stagehand** | AI primitives *on top of* Playwright (`act`/`extract`/`observe`) | Higher — deterministic Playwright for known steps, AI only where needed; auto-caching approaches native speed on repeat | More stable; "AI-on-Playwright" is the emerging template | Strong — this stack already speaks Playwright (Camoufox/Patchright/Obscura); Stagehand augments rather than replaces |
| **Deterministic Playwright only** | No LLM | 99%+ | Stable | Best where the flow is known; no generalization to novel UIs |

## Recommendation

1. **Now (this effort): deterministic-first guidance.** E2 is repositioned in
   docstrings/docs as last-resort/interactive-only. Prefer E1 or a direct
   Playwright script for known flows.
2. **Next (own PR): gate the E1→E2 auto-escalation.** Today the REST `/scrape`
   (`_do_scrape_e_tier`) and MCP (`_continue_to_e_tier`) cascades auto-escalate
   into the full browser-use loop whenever E1 comes back `blocked`. Change this
   so E2 is entered only when the caller flags the task as interactive
   (e.g. an `interactive: bool` request field / config gate) — otherwise stop at
   E1 and return the blocked result. This is a **default-behavior change** to the
   two public cascades, so it belongs in its own reviewed PR with a test:
   *a blocked E1 result on a non-interactive request stops at E1; an
   interactive-flagged request still escalates.* Deferred here to avoid
   destabilizing the cascade inside a large change.
3. **Later (own effort): Stagehand evaluation.** If E2 reliability/maintenance
   remains a pain point, prototype the interactive flows on `stagehand-py`
   against the existing Camoufox/Patchright/Obscura Playwright browsers and
   compare success rate + maintenance cost head-to-head with browser-use before
   committing to a migration.

## Sources

- Stagehand vs Browser Use vs Playwright comparisons (NxCode, Skyvern, Scrapfly), 2026.
- Reliability/compounding figures and the hybrid recommendation from the same 2026 surveys.

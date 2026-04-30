# scrapper-tool documentation

A reusable Python web-scraping toolkit. See the [README](../README.md) for the elevator pitch.

## Table of contents

- **[Quickstart](quickstart.md)** — 5-minute on-ramp.
- **[Recon playbook](recon.md)** — DevTools-driven reverse-engineering of a new vendor site (the methodology behind PartsPilot's `scraping-vendor-recon` skill, generalised).
- **Pattern guides**:
  - [Pattern A — JSON API](patterns/a-json-api.md)
  - [Pattern B — Embedded JSON](patterns/b-embedded-json.md)
  - [Pattern C — CSS / microdata](patterns/c-css-microdata.md)
  - [Pattern D — Hostile (Cloudflare Turnstile, Akamai EVA, …)](patterns/d-hostile.md)
- **Reference**:
  - [HTTP client](reference/http.md)
  - [Anti-bot ladder](reference/ladder.md)
  - [Test helpers](reference/testing.md)
- **Research**:
  - [2026-04-30 landscape snapshot](research/2026-04-30-landscape.md) — why these tools, sourced.
  - [Tool catalog](research/tool-catalog.md) — adopted / candidate / rejected matrix.
  - [Do-not-adopt list](research/do-not-adopt.md) — append-only rejects with dates + reasons.
- **[Agent integration](agent-integration.md)** — MCP wiring for LLM agents (v0.2.0+).

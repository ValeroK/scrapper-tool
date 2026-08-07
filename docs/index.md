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
- **[HTTP REST sidecar](http-sidecar.md)** — call scrapper-tool from any service over plain HTTP (v1.1.0+). OpenAPI spec at [`openapi/openapi.yaml`](openapi/openapi.yaml).
- **[Agent integration](agent-integration.md)** — MCP wiring for LLM agents (v0.2.0+).
- **[Cookbook](cookbook.md)** — task-shaped recipes.
- **[Settings](SETTINGS.md)** — every environment variable, plus:
  - [Scraping a logged-in page](SETTINGS.md#cookies-and-the-sidecar) — `cookies export`, the sidecar's 403 rule, and why wildcard CORS drops credentials.
  - [Installing `[cookies]` without a Rust toolchain](SETTINGS.md#installing-cookies-without-a-rust-toolchain).

## Command-line tools

The `scrapper-tool` command has three subcommands; run any of them with `--help`.

| Command | What it does |
|---------|--------------|
| `scrapper-tool doctor` | Preflight every cascade tier and print the command that fixes each one that isn't working. Exit `0` ready / `1` degraded / `2` not ready; `--json` and `--require-tier <name>` for CI and container healthchecks. |
| `scrapper-tool cookies export --domain <host>` | Read a session cookie out of your own browser profile so the cascade can scrape a logged-in page. Host-side only, never sees a password, absent from the MCP surface by design. |
| `scrapper-tool canary <url>` | Probe a URL through the impersonation ladder and report which profile won — a nightly fingerprint-health check. |

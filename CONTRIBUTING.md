# Contributing to `scrapper-tool`

Thanks for considering a contribution. This document is short on purpose — the heavy lifting lives in [`docs/recon.md`](docs/recon.md) (vendor reconnaissance methodology) and [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (why the tool selection looks the way it does).

## Living document — the maintenance contract

The web-scraping landscape moves fast. Anti-bot defences harden, browser-impersonation fingerprints get burned, vendors restructure their HTML. **The lib stays sharp only if every contributor pays it forward.**

The contract:

1. **PRs that change scraping behaviour update the relevant docs in the same PR.** New `curl_cffi` profile in `IMPERSONATE_LADDER` → update `docs/reference/ladder.md`. New pattern variant we discover → update `docs/patterns/<x>.md`. New anti-bot we get blocked by → document the failure mode + workaround in `docs/research/`. No exceptions. Reviewer-enforced.
2. **`CHANGELOG.md` is append-only.** Each PR adds a row to the `[Unreleased]` block. Don't rewrite history.
3. **`docs/research/do-not-adopt.md` is append-only with dates.** Overturning a reject (e.g. a previously-deprecated tool gets revived) requires a *new* dated entry, never editing the old one.
4. **The `live-canary.yml` workflow is the lib's heartbeat.** If it goes red, that's a P0 — fix the lib or update the canary URL set, but don't let it stay red.

## When to update the lib

Triggers for a new PR:

- A new `curl_cffi` impersonation profile ships and is more recent than our current `chrome133a` primary → bump.
- A new browser-fingerprint stealth tool surfaces and benchmarks better than Camoufox/Scrapling → propose addition (or "do not adopt" entry with rationale).
- A vendor site we know about (or a community report) shows a new pattern variant the §A-D tree doesn't cover → extend the tree.
- The MCP SDK ships a major version → migrate `mcp.py` and bump the `[agent]` extra version pin.

## Development setup

```bash
git clone https://github.com/ValeroK/scrapper-tool.git
cd scrapper-tool
uv sync --all-extras --dev      # full deps including [hostile] and [agent]
uv run pytest                    # core unit tests (skips live + integration)
uv run pytest -m live            # live-canary tests (requires internet)
uv run ruff check src/ tests/
uv run mypy src/
```

## PR checklist

- [ ] `uv run pytest --cov --cov-fail-under=85` green
- [ ] `uv run ruff check` and `uv run ruff format --check` clean
- [ ] `uv run mypy src/` clean (strict mode)
- [ ] `uv run pip-audit` clean (no new CVEs in transitive deps)
- [ ] `CHANGELOG.md` row added under `[Unreleased]`
- [ ] If the PR touches scraping behaviour: relevant `docs/` updated in the same PR
- [ ] If the PR adds a new tool/library: research entry added to `docs/research/2026-04-30-landscape.md` (or successor) **and** `docs/research/tool-catalog.md` row added

## Code of conduct

By participating you agree to abide by the [Contributor Covenant 2.1](CODE_OF_CONDUCT.md).

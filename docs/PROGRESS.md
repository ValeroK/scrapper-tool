# Progress and open points

Living status for the concept-adoption series planned in
[`research/2026-08-26-donsetch-concepts.md`](research/2026-08-26-donsetch-concepts.md).
Unlike the dated `research/` snapshots, this file is **edited in place** — it
describes the current state, not a moment in the past.

Last updated: 2026-08-27. Branch: `feat/url-guard` (unpushed).

---

## Shipped

| Increment | What landed |
|---|---|
| **2.2.1** | Target URL guard — four enforcement layers, `UrlNotAllowed`, doctor row, REST 403 / MCP envelope |
| **2.2.2** | Captcha grid tier gets its own vision model (`qwen3.8-27b-apex`) + a doctor probe for it |
| **2.2.3** | Render tier aborts page-initiated SSRF; opt-in per-hop redirect vetting for curl_cffi |
| — | Dependency tree upgraded; `mcp` capped `<2` |

Also merged from `main` (separate session): the unit-suite hermeticity fixes,
a hard-E1-failure handoff to E2, and a raise when Crawl4AI reports a page that
never loaded.

**Verification state:** 1371 passing, coverage 87.84%, ruff / `ruff format` /
mypy `--strict` clean, zero `docs/openapi/` drift, `pip-audit` clean.

---

## Decisions waiting on a human

### 1. The ladder still leads with `chrome146`; `chrome150` is now available

`curl_cffi` 0.16.2 (installed) ships a `chrome150` target. `IMPERSONATE_LADDER`
still leads with `chrome146`, two Chrome releases behind.

This matters on this project's own terms — `ladder.py`'s docstring already argues
that pinning a build no real user runs is *itself* a fingerprint, and the file
documents a "Bumping the primary" procedure. Deliberately not done as part of the
dependency bump: it changes anti-bot behaviour and wants its own canary run.

### 2. `SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS` is still off by default

The fingerprint question it was gated on has been answered. Measured on the
redirected hop itself across `chrome146`, `chrome142`, `firefox147` and
`safari260` — three browser families — JA4, peetprint, Akamai h2 and h2 header
order are **identical** with the flag on and off. (JA3's hash varies in *both*
modes: that is GREASE, which is what real Chrome does.)

The security difference is not marginal: with the flag **off**, a redirect to
`169.254.169.254` is genuinely connected to **three times** (once per retry,
visible in curl's own error) and only the response is withheld.

Remaining argument for leaving it off is volume, not correctness — one host, one
target pair. Flipping it is a one-line change in `_urlguard.strict_redirects_enabled`.

### 3. A page where every tier fails is still reported `ok`

`CrawlPage.ok` is `error is None and skipped_reason is None`, and the cascade
returns an error-envelope payload rather than raising when every tier fails. So a
50-page crawl with 10 dead pages reports `stats["failed"] == 0` and 50/50 visited.

That contradicts `CrawlPage`'s own docstring, which says failures are reported
precisely because *"40 of 50 pages worked and 50 of 50 worked call for different
follow-up"*. The hermeticity fix on `main` restored the affected test by pinning
Pattern D off for it, and deliberately left the product semantics alone.

Changing it moves `stats["failed"]` on a published response shape, so it belongs
in a numbered increment rather than a drive-by fix.

---

## Known gaps, stated rather than closed

### Blind SSRF is not fully closed

| Path | State |
|---|---|
| httpx (A/B/C plain, sitemap, robots) | **closed** — per-hop, before connect |
| curl_cffi ladder | **closed only when `..._STRICT_REDIRECTS=1`**; otherwise post-flight |
| render tier — page-initiated (`<img>`, `<iframe>`, `fetch`) | **closed** |
| render tier — navigation redirect hops | **open** — Playwright's `route` does not fire for them (measured) |
| Pattern D, E1, E2, obscura subprocess | **open** — pre-flight only |

Where it is open, the request *is issued* and only the body is withheld. That is
not a safe residual: a state-changing GET has already happened, and the distinct
error codes and timings make a serviceable internal port scanner.

### `SCRAPPER_TOOL_URL_GUARD_STRICT` was planned for 2.2.3 and not built

The mode that refuses to *run* a tier that cannot be intercepted — the only way
to offer a genuinely closed configuration, and the thing that would make the
table above honest for an operator who needs one. 2.2.3 was scoped down to the
two interception mechanisms and this was dropped without being flagged at the
time.

### `_VISION_MAX_TOKENS` is still 512

Calibrated on a 4B model. The 27B now serving the grid tier is a *reasoning*
model — it spent 75 reasoning tokens on a 1x1 pixel — so a real 3x3 grid may
exhaust the budget, and the solver reads an empty `content` as failure. The
number should come from `usage.completion_tokens_details.reasoning_tokens` on a
live solve, not from a guess. Deferred with the captcha benchmark.

### `qwen3-vl:8b` vs a ~27B has never been compared on one machine

The 0/5, 1/5 and 4-5/5 figures in `AgentConfig.captcha_vision_model`'s docstring
come from different hosts. `qwen3-vl` is a purpose-built vision-language model
and the 27B is a large general one, so specialisation could still beat size at
grid localisation. Deferred with the same benchmark.

---

## Not started

Later increments from the plan, in order: `_params.py` as one source of truth for
both surfaces (which also fixes the A/B/C leg inheriting a 10s timeout where REST
passes 30s), surface reconciliation, cookie-efficacy learning (`replay_ok` +
learned clearance TTL), a per-host pacing governor, page fingerprints and delta
crawls, a whole-cascade deadline with MCP timing, and property tests on the
parsers.

In flight: migrating the MCP server to the 2.x SDK (`FastMCP` → `MCPServer`).

---

## Things that look alarming and are not

### "Pattern D is turned off"

It is not. `src/` is untouched; `_hostile_available()` still reads the real probe
and the cascade still invokes D at `http_server.py:2367`. Verified in a normal
process: `_extras.hostile_available()`, `http_server._hostile_available()` and
`mcp._hostile_available_for_mcp()` all return `True`.

What changed is **test-only**: `tests/conftest.py` uses `monkeypatch` (per-test,
auto-reverted) to pin D off *inside the unit suite*, because D launches
Scrapling's real Playwright browser — so any unit test escalating past A/B/C was
doing live DNS and network, and the suite's result depended on which extras
happened to be installed. The 7 tests that exercise D turn it back on; 40
D-related tests still pass.

### The MCP tests "skipped" during the dependency upgrade

Under `mcp` 2.x all 68 skip rather than fail, because they guard on
`pytest.importorskip("mcp.server.fastmcp")` and 2.x renamed that module. The skip
reason then blames a missing `[agent]` extra that is in fact installed. This is
why `mcp` is capped `<2` until the migration lands — and why the migration has to
change those guards too, or it cannot be verified.

Note also that `[tool.uv] override-dependencies` **replaces** a package's
requirements rather than intersecting with them, so a bound that matters must be
written in both the extra and the override. A bare `mcp>=1.28.1` in the override
silently defeated the `<2` cap once already.

---

## Housekeeping

- Seven commits on `feat/url-guard`, **nothing pushed**.
- `backup/url-guard-pre-rebase` still exists; safe to delete now the rebase is
  validated.
- `C:\Users\kobiv\.env` was UTF-16 and crashed `python-dotenv` at import, which
  took `crawl4ai`, Pattern E1 and `scrapper-tool doctor` down with it. Converted
  to UTF-8; backup at `~/.env.utf16.bak`.

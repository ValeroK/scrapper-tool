# Progress and open points

Living status for the concept-adoption series planned in
[`research/2026-08-26-donsetch-concepts.md`](research/2026-08-26-donsetch-concepts.md).
Unlike the dated `research/` snapshots, this file is **edited in place** — it
describes the current state, not a moment in the past.

Last updated: 2026-08-29. Released as **v3.1.1**. The MCP 2.x SDK migration
shipped in 3.1.0; 3.1.1 is a CI/docs follow-up with no functional source change
(see CHANGELOG). Both are tagged and published to PyPI and GHCR.

---

## Shipped — v3.0.0

| Increment | What landed |
|---|---|
| guard | Target URL guard — four enforcement layers, `UrlNotAllowed`, doctor row, REST 403 / MCP envelope |
| captcha | Captcha grid tier gets its own vision model (`qwen3.8-27b-apex`) + a doctor probe for it |
| SSRF | Render tier aborts page-initiated SSRF; opt-in per-hop redirect vetting for curl_cffi |
| — | Dependency tree upgraded; `mcp` capped `<2` |
| — | Ladder refreshed to `chrome150 → chrome146 → safari2601 → firefox147 → chrome133a`, and the impersonated User-Agent is no longer overwritten |
| — | `SCRAPPER_TOOL_URL_GUARD_STRICT` — opt-in refusal of tiers whose requests cannot be vetted |

Also merged from `main` (separate session): the unit-suite hermeticity fixes,
a hard-E1-failure handoff to E2, and a raise when Crawl4AI reports a page that
never loaded.

**Verification state:** 1388 passing, coverage 87.88%, ruff / `ruff format` /
mypy `--strict` clean, zero `docs/openapi/` drift, `pip-audit` clean.

---

## Shipped — v3.1.0 (MCP 2.x SDK)

Plan: [`research/2026-08-28-mcp-2x-migration-plan.md`](research/2026-08-28-mcp-2x-migration-plan.md).
All twelve work items done.

| Item | What landed |
|---|---|
| 1-3 | `_build_server` builds `MCPServer` from `mcp.server.mcpserver`; `host`/`port` moved to `run()` for the HTTP transports only; the import error now distinguishes "extra missing" from "SDK API moved" instead of reporting both as the former |
| 4-5 | Test guards ask `importorskip("mcp")` — the package — so a moved API fails loudly; the `test_crawl_endpoints.py` guard is now class-scoped and no longer takes 13 unrelated REST tests with it |
| 6 | `TestMainTransportPlumbing` drives the real `_build_server` and mocks only the blocking `run()`; new tests pin the real import-error messages and the SDK contract `main()` depends on |
| 7 | Both docker-compose MCP services declare `entrypoint: ["scrapper-tool-mcp"]`; stdio disables the inherited REST healthcheck |
| 8 | e2e clients updated for `streamable_http_client` (renamed, and now a 2-tuple); `scripts/` added to the lint scope so they cannot rot silently again |
| 9 | `mcp>=2.1.1,<3` in **both** the `[agent]` extra and `[tool.uv] override-dependencies` |
| 10 | `docs/mcp-tools.json` + generator + drift test + `mcp-tool-surface-check` CI job, mirroring `openapi-spec-check` |
| 11 | Tool tables, the `instructions=` string, `docs/docker.md`, `docs/E2E_TEST_PLAN.md` and `CONTRIBUTING.md` corrected to nine tools and to the real entrypoint |
| 12 | Coverage claim reproduced and corrected — see below |

Two follow-ups landed after the plan, both found by testing the new guard
rather than trusting it:

- **The drift guard only proved the snapshot matched the code**, which is not
  the same as the surface being documented. Adding a tenth tool and
  regenerating the snapshot left the four docs that enumerate the surface and
  both e2e `EXPECTED_TOOLS` sets stale, with every test green. The guard now
  checks those six copies by name; re-ran the same simulation afterwards and
  all six fail, each naming its own file.
- **The Codecov upload was dead twice over** (it was on the plan's
  out-of-scope list as a single fault). The step was gated on
  `matrix.extras == 'dev,agent,http'`, which the matrix stopped producing, and
  the coverage run emitted only `term-missing`, so there was no file to upload
  even if the gate had fired. Fixed both; a gate that names a matrix value
  breaks the next time that value is edited, so it now keys on the Python
  version alone.

**Verification state:** 1398 passing, **1 skipped** (`rookiepy`, the `[cookies]`
extra, unrelated), coverage 87.91%, ruff / `ruff format` / mypy `--strict` clean,
zero `docs/openapi/` or `docs/mcp-tools.json` drift, `pip-audit` clean.

The decisive check is that the MCP tests **run**: `test_mcp.py`,
`test_agent_mcp.py` and `test_crawl_endpoints.py` collect 90 and skip **zero**.
A green run *with* skips is the failure this work existed to prevent.

Two breaks found by checking rather than assuming, neither in the plan:

- **`browser-use` imports `pydantic_settings` without declaring it.** It had
  been riding on mcp 1.x's dependency on that package; 2.x drops it, so
  `import browser_use` started failing. This surfaced *only* as three tests
  going from passed to skipped — the same disguise as the SDK break itself.
  Now declared explicitly in `[llm-agent]`.
- **The client SDK moved too.** `streamablehttp_client` is now
  `streamable_http_client` and yields a 2-tuple rather than 3. Only
  `scripts/e2e/` speaks the wire protocol, and it is neither collected by
  pytest nor (until now) linted.

**Wire protocol verified by hand on 2026-08-29**, since nothing in CI covers the
JSON-RPC handshake:

| Check | Result |
|---|---|
| `scripts/e2e/test_mcp_session.py` (stdio) | all steps pass, 9 tools, incl. E1 + E2 against the local LLM |
| `scrapper-tool-mcp --transport streamable-http` + `test_mcp_session_http.py` | all steps pass, 9 tools |
| `docker compose run --rm -T scrapper-tool` | speaks MCP for the first time; `tools/list` returns 9 |
| `docker compose --profile http up -d scrapper-tool-mcp-http` | reports **healthy**, was unhealthy by construction before |
| `test_mcp_session_http.py` against the container on `:8000` | all steps pass, 9 tools |
| `--transport sse` | binds and serves `text/event-stream` (previously signature-checked only) |

Re-verified on 2026-08-29 after adding the three missing tool calls: stdio,
local streamable-http, and **the container** all report `all 10 tool checks
passed`, with every one of the nine tools invoked rather than merely listed.

Two things worth knowing before re-running those:

- `agent_browse` (E2) needs a CDP-capable backend. The default is Camoufox,
  which is Firefox and has no CDP, so E2 raises a deliberate `ConfigurationError`
  rather than silently downgrading stealth. Set
  `SCRAPPER_TOOL_AGENT_BROWSER=patchright` **on the server process**, not the
  client script.
- `docker-compose.yml` substitutes `SCRAPPER_TOOL_AGENT_OLLAMA_URL` from the
  host shell. A host-local `127.0.0.1:PORT` becomes the *container* inside the
  container; use `host.docker.internal`.

Also fixed while there: `scripts/e2e/test_mcp_session.py` asserted
`winning_profile == "chrome146"`. 3.0.0 moved the head of the ladder to
`chrome150` and nothing reported it, because that file is neither collected nor
(until now) linted. It now asserts against `IMPERSONATE_LADDER[0]`.

**Released 2026-08-29 as `v3.1.0`.** CI green on all ten jobs, including the
new `mcp-tool-surface-check`. Published to PyPI (`scrapper_tool-3.1.0`, wheel +
sdist) and GHCR (`3.1.0`, `3.1`, `latest`). Verified from outside the repo by
installing `scrapper-tool[agent]==3.1.0` from PyPI into a clean venv: it
resolves `mcp` 2.1.1 and registers all nine tools.

Minor rather than major on purpose: no public Python API and no MCP tool
changed. The breaking part is the dependency floor on the `[agent]` extra,
which will not co-install with an `mcp` 1.x pin.

**One thing did not survive contact with CI**, and it is instructive. The
Codecov upload was reported here as fixed. The first real run showed it still
rejected: `Token required - not valid tokenless upload`. Both faults found
locally were real and fixed — the step ran, found and sent `coverage.xml`
— but a third existed only in CI.

Removed rather than repaired, after checking what depended on it: no badge, no
`codecov.yml`, nothing reading the data, and it had been silently dead since
the test matrix was trimmed. `--cov-fail-under=85` is and always was the real
gate, and it runs the same on a laptop as in CI. Fixing the upload would have
meant a Codecov account and a repo secret to feed a service nobody reads.

Post-release state: nothing outstanding. The remaining known gaps are the two
the plan deferred on purpose — `doctor` cannot tell whether the captcha
vision model can *see*, and `scripts/e2e/` stays in the manual tier, so its
nine-tool coverage only runs when a human runs it.

---

## Decisions waiting on a human

### 1. `SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS` is still off by default

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

### 2. A page where every tier fails is still reported `ok`

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

`SCRAPPER_TOOL_URL_GUARD_STRICT=1` closes every open row by refusing to run the
tiers in them — see below for what that costs.

### `SCRAPPER_TOOL_URL_GUARD_STRICT` — now built

Was planned for 2.2.3, dropped from it without being flagged, and has since
landed. Setting it to `1` refuses to *run* `d`, `render`, `e1`, `e2`, `obscura`,
and `ladder` (the last only while `..._STRICT_REDIRECTS` is off), which is the
one configuration where the table above has no open row.

It costs capability by design: on a hostile target A/B/C is all that is left and
it is the tier such sites wall, so the scrape fails. Off by default because that
trade is the operator's. `doctor` names the refused tiers.

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

The MCP 2.x SDK migration is no longer in flight — it is landed and awaiting
a release tag; see the 3.1 section above.

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

### The MCP tests "skipped" during the dependency upgrade — resolved in 3.1

Fixed. Recorded here because the *shape* of the failure is worth keeping.

Under `mcp` 2.x the MCP tests skipped rather than failed, because they guarded on
`pytest.importorskip("mcp.server.fastmcp")` and 2.x renamed that module. The skip
reason then blamed a missing `[agent]` extra that was installed the whole time.
The guards now ask for the `mcp` package and let the server-class import fail
loudly. Measured at `8441e11` with 2.x installed: three modules, `3 skipped`, no
failures.

**Two counts in the earlier version of this note were wrong**, which is its own
small lesson about numbers written from memory. It said "all 68 skip"; the plan
then corrected that to 80. Both were guesses. `importorskip` skips at *module*
scope, so the number was never a count of MCP tests — it included 13 REST
`/map` and `/crawl` tests that share a module with the MCP parity class and have
nothing to do with the SDK. Post-migration the three modules collect **90** and
skip **zero**.

**The coverage claim was also wrong, in the reassuring direction.** This file
recorded that the suite went green under 2.x despite the skips, which should not
have been possible with `--cov-fail-under=85` enforced and no `omit`. Reproduced
on 2026-08-29 by reverting `src/` and `tests/` to 3.0.0 while keeping mcp 2.x
installed:

```
src\scrapper_tool\mcp.py    517   468   146     0     7%
TOTAL                       6857  1108  2070   259    82%
FAIL Required test coverage of 85% not reached. Total coverage: 81.89%
```

So the floor **does** catch it, exactly as designed — `mcp.py` collapses to 7%
and the total falls 3 points below the gate. The floor was never the problem;
the "green" run simply never included `--cov`. Nothing to fix here, but the
correction matters: the previous wording implied the coverage gate could not be
trusted to catch a large block of skipped tests, and it can.

Note also that `[tool.uv] override-dependencies` **replaces** a package's
requirements rather than intersecting with them, so a bound that matters must be
written in both the extra and the override. A bare `mcp>=1.28.1` in the override
silently defeated the `<2` cap once already. `CONTRIBUTING.md` now says so at
both places that would need it.

---

## Housekeeping

- Released as v3.0.0. Developed as 2.2.x increments; SemVer made that the wrong
  framing since five defaults changed, so they shipped together as a major bump.
- `backup/url-guard-pre-rebase` still exists; safe to delete now the rebase is
  validated.
- `C:\Users\kobiv\.env` was UTF-16 and crashed `python-dotenv` at import, which
  took `crawl4ai`, Pattern E1 and `scrapper-tool doctor` down with it. Converted
  to UTF-8; backup at `~/.env.utf16.bak`.

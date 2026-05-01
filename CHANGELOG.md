# Changelog

All notable changes to `scrapper-tool` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Added
- M11.5 — Live-canary GitHub Actions workflow (`.github/workflows/live-canary.yml`). Daily cron at 04:17 UTC + `workflow_dispatch`. Three jobs (smoke / Pattern A ladder / Pattern B extraction) probe stable public URLs (`example.com`, `httpbin.org/anything`, `schema.org/Product`); on failure, a fourth job opens (or comments on) a `live-canary-failed` GitHub issue with dedup so we don't get one issue per failed run.
- M11.5 — `tests/integration/test_live_probes.py` — three opt-in tests gated by `@pytest.mark.live` + `SCRAPPER_TOOL_LIVE=1` env var. Default `pytest` invocation skips them; the live-canary workflow runs them with the env var set. CI matrix unaffected.
- M11.5 — `tests/canary_targets.yaml` — append-only, dated registry of canary URLs. Discipline: never edit historical URLs in place; add a new row above and leave the predecessor as audit trail.

## [0.1.0] - 2026-04-30

First public release. Covers Pattern A/B/C/D extraction primitives, the four-profile anti-bot impersonation ladder, deterministic fixture-replay testing, the generic `Adapter` Protocol, and a `scrapper-tool canary` CLI.

### Added
- M0 — repo bootstrap: `pyproject.toml`, MIT `LICENSE`, README, governance files (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`), CI workflow (`.github/workflows/ci.yml` — ruff + mypy --strict + pytest + pip-audit on py3.12/3.13/3.14 matrix), tag-triggered PyPI release workflow (`.github/workflows/release.yml`, OIDC trusted-publisher).
- `[project.optional-dependencies]` placeholders for `hostile` (Scrapling) and `agent` (MCP — populated in M13).
- M1 — HTTP core extracted from PartsPilot's `affiliate-service`: `scrapper_tool.http.vendor_client()` (httpx + curl_cffi backends, async context manager) and `scrapper_tool.http.request_with_retry()` (3 attempts, exponential backoff with ±25% jitter, retries 429/5xx/transport errors, no-retry on 4xx ≠ 429, X-Request-ID injection).
- M1 — Exception hierarchy: `ScrapingError` (base), `VendorHTTPError`, `VendorUnavailable` (alias), `BlockedError`, `ParseError`. `BlockedError` and `ParseError` deliberately do NOT inherit from `VendorHTTPError` — circuit breakers should catch one but not the others.
- M1 — Optional `structlog` integration via `scrapper_tool._logging.get_logger()`; falls back to a stdlib `logging` adapter that accepts the same `key=value` kwarg shape.
- M1 — Top-level re-exports: `scrapper_tool.{vendor_client, request_with_retry, VendorHTTPError, BlockedError, ParseError, ScrapingError, VendorUnavailable, VendorHTTPClient}`.
- M2 — Anti-bot impersonation ladder (`scrapper_tool.ladder`): `IMPERSONATE_LADDER = ("chrome133a", "chrome124", "safari18_0", "firefox135")` and `request_with_ladder(method, url, ...)` walking it top-to-bottom on 403/503. First profile to return ≠403/503 wins; all-403 raises `BlockedError` with a "escalate to Pattern D" message. Each ladder step opens a fresh `curl_cffi.AsyncSession` (one-shot per profile, sessions pinned to a single fingerprint). Logs winning profile via the structured logger (`ladder.profile_won` / `ladder.profile_blocked`).
- M2 — Re-exported at top level: `scrapper_tool.{IMPERSONATE_LADDER, request_with_ladder}`.
- M2 — 9 ladder unit tests (happy path, 403→200 fallback, 503 rotate-like-403, safari wins when chrome burns, all-403 raises, custom ladder, empty ladder ValueError, default-ladder shape, header propagation). Uses an inline `_FakeCurlSession` lifted to `scrapper_tool.testing` in M6.
- M3 — Pattern B helper (`scrapper_tool.patterns.b`): `extract_product_offer(html, base_url=None)` returns a normalised `ProductOffer` Pydantic model from any of JSON-LD / microdata / RDFa Product blocks. Handles top-level Products, Products nested inside `@graph`, multi-offer lists (takes first), price/currency nested inside `priceSpecification`, brand-as-dict-or-string, image-as-list-or-dict, all `gtin{,8,12,13,14}` variants. Powered by `extruct.extract(..., uniform=True)` so one walker covers all three syntaxes.
- M3 — `ProductOffer` model fields: `name`, `sku`, `mpn` (often the OEM in automotive use cases), `gtin`, `brand`, `description`, `image`, `price` (Decimal), `currency` (ISO 4217), `availability` (raw schema.org URI), `url`. `model_config = {"extra": "ignore"}` so vendors adding fields don't break parsing.
- M3 — 10 Pattern B unit tests (JSON-LD top-level, JSON-LD inside @graph, offers as list, priceSpecification fallback, microdata, brand-as-string, no-Product-block returns None, plain HTML returns None, base_url propagation, extra-keys ignored).
- M4 — Pattern C helper (`scrapper_tool.patterns.c`): `extract_microdata_price(html) -> tuple[Decimal, str] | None` for sites that ship `<meta itemprop="price"> + <meta itemprop="priceCurrency">` schema.org microdata anchors (preferred — stable across CSS reshuffles); `extract_via_selectors(html, *, price_selector, currency_selector=None, default_currency=None)` for last-resort bespoke CSS selectors. Backed by `selectolax` (lexbor backend; 30-40× faster than BeautifulSoup at our fetch volumes).
- M4 — Internal `_coerce_decimal` strips common currency glyphs (`$`, `€`, `£`, `₪`, `¥`) and US/UK thousands-separator commas before parsing. European decimal-comma is NOT supported by default — vendor-specific normalisation is the consumer's job.
- M4 — 21 Pattern C unit tests (microdata via `<meta>` content attribute, microdata via text fallback, price-without-currency returns None, missing microdata returns None, selector with default_currency, selector with currency_selector, selector with `data-price` attribute preferred, missing element returns None, ValueError on no-currency-source, glyph stripping for 6 currency symbols, thousands-separator stripping, unparseable input returns None).
- M5 — Pattern D helper (`scrapper_tool.patterns.d.hostile_client`): async context manager wrapping Scrapling's `StealthyFetcher` for Cloudflare Turnstile / Akamai EVA / Distil-class hostile sites. Lazy-imports `scrapling` so consumers without the `[hostile]` extra installed see a useful `ImportError` with install hint rather than `ModuleNotFoundError` at import time. Forwards `headless`, `block_resources`, `timeout`, and arbitrary `extra_kwargs` to the fetcher; supports both async (`aclose`) and sync (`close`) lifecycle on exit.
- M5 — 5 Pattern D unit tests (`ImportError` raised when `[hostile]` not installed, fetcher yielded + closed on exit, `extra_kwargs` propagate, sync-close fallback for older Scrapling versions, module docstring readable without scrapling installed). Real Scrapling integration deferred to live-probe tests (`tests/integration/test_live_probes.py`, `live` marker, opt-in).
- M6 — Test helpers (`scrapper_tool.testing`): `FakeCurlSession` (drop-in mock for `curl_cffi.AsyncSession` because `respx` doesn't intercept it), `FakeResponse` (minimal duck-typed response), `replay_fixture(path, parser)` (load fixture file from disk and feed to a parser), `assert_pydantic_snapshot(obj, path, *, write_if_missing=True)` (golden-snapshot diff for Pydantic models with first-run seeding).
- M6 — Refactored `tests/unit/test_ladder.py` to use the canonical `FakeCurlSession` (M2's inline mock removed; replaced with the import).
- M6 — 12 meta-tests in `tests/unit/test_testing_helpers.py` covering FakeResponse construction, FakeCurlSession reset/configuration/calls-tracking, replay_fixture text loading, snapshot first-run-write / pass-on-match / fail-on-drift / write_if_missing=False semantics. 100% coverage on `testing.py`.
- M5.5 — Filled `docs/research/2026-04-30-landscape.md` (~250 lines, 19 numbered sources). Eight sections: TLS-impersonation libraries, browser-stealth tools, anti-bot platforms in 2026, LLM-assisted scraping, HTML parsing libraries, structured-data extraction, what's deliberately missing from the lib, and a refresh policy that makes successor landscape docs append-only history rather than edits-in-place.

- M7 — Generic `Adapter[QueryT, ResultT]` Protocol (`scrapper_tool.adapter`). Structural typing with `runtime_checkable` so `isinstance(obj, Adapter)` works without inheritance. Required surface: `vendor_id: str` attribute + `async search(query)` + `async fetch_detail(url)`. Doc-strings codify the error-bubbling contract (VendorHTTPError → breaker trips; BlockedError → escalate to Pattern D; ParseError → don't trip breaker, parser drift bug). Re-exported as `scrapper_tool.Adapter`.
- M7 — 6 Protocol tests: complete impl satisfies isinstance, missing method fails, missing field fails, search round-trip, fetch_detail round-trip, fetch_detail returns None for missing URL.
- M8 — `scrapper-tool canary` CLI (`scrapper_tool.canary` module + `[project.scripts]` entry). Walks the impersonation ladder against a target URL, reports which profile won (or all-blocked). Designed for cron / GitHub Actions to surface "chrome133a is starting to 403" before any consumer adapter notices. Flags: `--profiles chrome133a,chrome124,...` (custom ladder), `--timeout` (per-request), `--proxy`, `--json` (machine-readable output). Exit codes: 0 success, 1 all-blocked, 2 error. Public API: `run_canary()` (programmatic) + `probe_profile()` (single-profile probe).
- M8 — 12 canary unit tests covering happy-path (first profile wins, others skipped), 403 fallback (rotates), all-blocked (exit_code=1), empty ladder ValueError, custom ladder, text mode, JSON mode parseable, --profiles override, exit codes, --help, no-subcommand argparse error, malformed --profiles flag.

### Fixed
- CI: `pip-audit --skip-editable` so the build doesn't try to look up `scrapper-tool` itself on PyPI before v0.1.0 ships.

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).

# Changelog

All notable changes to `scrapper-tool` are recorded here. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows [SemVer](https://semver.org/).

## [Unreleased]

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

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).

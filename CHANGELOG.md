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

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).

### Notes
- Initial milestone scope and decision log live in [`docs/research/2026-04-30-landscape.md`](docs/research/2026-04-30-landscape.md) (filled in during M5.5).

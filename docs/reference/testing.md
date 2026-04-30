# Test helpers reference

> *Stub — populated in M6.*

`scrapper_tool.testing` provides fixture-replay testing helpers reusable across any consuming project:

- **`FakeCurlSession`** — drop-in mock for `curl_cffi.requests.AsyncSession` (because `respx` does not intercept the curl_cffi transport). Use via `monkeypatch.setattr(scrapper_tool.http, "_CurlCffiAsyncSession", FakeCurlSession)`.
- **`respx_session_factory(...)`** — convenience wrapper around `respx.MockTransport` for the default `httpx` backend.
- **`replay_fixture(path, parser)`** — load HTML/JSON from disk + run it through `parser`. Returns the parsed result.
- **`assert_pydantic_snapshot(obj, snapshot_path)`** — golden-snapshot diff for Pydantic models. Writes the snapshot on first run; asserts equality thereafter.

Pattern: every adapter ships ≥5 fixture files (happy path / out-of-stock / multi-result / cross-make / garbage input) under `tests/fixtures/<vendor>/`, plus golden Pydantic snapshots. Replay tests catch parser regressions deterministically — no mocked HTTP, no flakes.

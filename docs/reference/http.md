# HTTP client reference

> *Stub — populated in M1.*

`scrapper_tool.http` provides:

- `vendor_client(timeout=10.0, use_curl_cffi=False, extra_headers=None, proxy=None)` — async context manager yielding either `httpx.AsyncClient` or `curl_cffi.requests.AsyncSession` (when `use_curl_cffi=True`).
- `request_with_retry(client, method, url, max_attempts=3, **kwargs)` — issues a request with exponential backoff + ±25% jitter on transient failures (5xx, 429, transport error). Does not retry 4xx ≠ 429.
- `VendorHTTPError` — raised when all retry attempts exhaust on a retriable failure.

See [`reference/ladder.md`](ladder.md) for the impersonation-profile fallback chain wired into `request_with_retry` when `use_curl_cffi=True`.

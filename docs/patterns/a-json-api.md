# Pattern A — JSON API

> *Stub — populated in M9.*

**Signals**: DevTools Network → Fetch/XHR shows a request returning JSON with the price/availability fields you need. Endpoint may be anonymous, OAuth-gated, or behind a session cookie. Anonymous + stable is best.

**Helper**: there isn't one — Pattern A is just `vendor_client()` + `request_with_retry()` + your own Pydantic response model. The lib's job is to provide a polite HTTP client; the response shape is yours.

**Cost**: lowest. JSON parsing is faster + more robust than HTML scraping.

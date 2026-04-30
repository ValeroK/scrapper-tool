# Anti-bot impersonation ladder

> *Stub — populated in M2.*

When `use_curl_cffi=True`, `vendor_client` walks `IMPERSONATE_LADDER` top-to-bottom on 403/503, stopping at the first profile that returns ≠ 403:

```python
IMPERSONATE_LADDER: tuple[BrowserTypeLiteral, ...] = (
    "chrome133a",   # primary — freshest in curl_cffi 0.x as of 2026-04-30
    "chrome124",    # fallback — validated against 5 production vendor adapters
    "safari18_0",   # diversification (chrome family disproportionately fingerprinted — see curl_cffi#500)
    "firefox135",   # last resort before Pattern D (Scrapling)
)
```

The walking is a one-shot fallback per request, not per attempt — no exponential explosion of profiles × retries. The first profile to return ≠ 403 wins for that request and is logged as the effective profile via the structured logger.

**Bumping the primary**: when `chrome133a` starts showing >5% 403 rate in the [`live-canary.yml`](../../.github/workflows/live-canary.yml) workflow, bump it to the next stable `curl_cffi` profile (currently `chrome142`/`chrome146` are the freshest available; promote whichever has stabilised).

**Source for the chrome116+ disproportionate fingerprinting note**: [`curl_cffi#500`](https://github.com/lexiforest/curl_cffi/issues/500) — Cloudflare reportedly identifies chrome116+ TLS profiles more reliably than safari/firefox, hence the diversification rows.

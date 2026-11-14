# Anti-bot impersonation ladder

> *Stub — populated in M2.*

When `use_curl_cffi=True`, `vendor_client` walks `IMPERSONATE_LADDER` top-to-bottom on 403/503, stopping at the first profile that returns ≠ 403:

```python
IMPERSONATE_LADDER: tuple[BrowserTypeLiteral, ...] = (
    "chrome152",   # primary — freshest in curl_cffi 0.17.0 (2026-11-14)
    "chrome150",   # fallback — the previous primary; a different JA4
    "safari270",   # diversification (chrome family disproportionately fingerprinted — see curl_cffi#500)
    "firefox155",  # freshest Firefox
    "chrome133a",  # tail rung, the originally validated primary; last stop before Pattern D (Scrapling)
)
```

The walking is a one-shot fallback per request, not per attempt — no exponential explosion of profiles × retries. The first profile to return ≠ 403 wins for that request and is logged as the effective profile via the structured logger.

**Bumping the primary**: two triggers — the leading profile showing a >5% 403 rate in the [`live-canary.yml`](../../.github/workflows/live-canary.yml) workflow, or `curl_cffi` shipping a fresher Chrome than the one we lead with (`test_ladder_leads_with_a_fresh_profile` fails on the second). Probe the candidate live before promoting: the numeric suffixes do not order themselves — `safari2601` is Version/26.0.1 against `safari260`'s 26.0.

**Source for the chrome116+ disproportionate fingerprinting note**: [`curl_cffi#500`](https://github.com/lexiforest/curl_cffi/issues/500) — Cloudflare reportedly identifies chrome116+ TLS profiles more reliably than safari/firefox, hence the diversification rows.

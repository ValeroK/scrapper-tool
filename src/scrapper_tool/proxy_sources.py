"""Load proxies from external lists (network I/O), keeping ``proxy.py`` pure.

Currently supports the **ProxyScrape free-proxy-list** mirror
(https://github.com/proxyscrape/free-proxy-list) — ~2k proxies across ~95
countries, refreshed every 5 minutes, served over jsDelivr, and — unusually for
a free list — carrying real metadata per entry (uptime %, anonymity level,
latency, ASN/ISP, SSL capability). That metadata is what makes it usable at all:
it lets us filter to the least-bad subset instead of hammering a raw ip:port dump.

READ THIS BEFORE USING IT
-------------------------
Free proxies are **not** a solution for protected targets, and this module will
not pretend otherwise:

1. **They are pre-flagged.** Every major anti-bot (Cloudflare, DataDome, Akamai,
   PerimeterX, Imperva) maintains datacenter IP-range reputation lists, and
   public proxy IPs are on them. Swapping a flagged residential IP for a
   pre-flagged datacenter IP makes a hard target *worse*, not better. Only
   residential/mobile IPs carry the trust score that moves the needle there.
2. **They are operated by strangers.** The upstream README says it plainly: a
   proxy operator "can log your traffic, inject content, or hijack sessions."
   Pools built here are therefore marked ``untrusted=True`` and will refuse
   credentialed use via :meth:`ProxyPool.assert_safe_for_credentials`.
3. **Most are dead, and almost none can do HTTPS.** Measured 2026-07 against this
   very list: of 80 entries passing the metadata filters (elite/anonymous, >=50%
   uptime, <=5 s latency), **3-5 answered a plaintext http:// probe and ZERO could
   CONNECT-tunnel TLS**. Since every real scraping target is https://, the usable
   yield was 0%. Always run :func:`validate_proxies` (which probes over https for
   exactly this reason) and expect to keep almost nothing.

Legitimate uses: exercising the rotation/cooldown machinery against real-world
flakiness (it found two genuine bugs in our ladder), and spreading load on
**unprotected** high-volume targets. For anything protected, buy residential or
mobile proxies — this list is not a substitute.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from scrapper_tool._logging import get_logger
from scrapper_tool.proxy import ProxyPool

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

_logger = get_logger(__name__)

_CDN_BASE = "https://cdn.jsdelivr.net/gh/proxyscrape/free-proxy-list@main/proxies"

# httpx speaks socks only with the [socks] extra (socksio). Default to the HTTP
# family so a liveness probe works on a stock install; callers can opt into socks.
_DEFAULT_PROTOCOLS: tuple[str, ...] = ("http", "https")

# A liveness probe needs a tiny, highly-available, anti-bot-free endpoint — and it
# MUST be https://. Many free "HTTP" proxies happily relay plaintext HTTP but
# cannot CONNECT-tunnel TLS, so probing over http:// marks them live and they then
# fail on every real (https) target with `curl (56) CONNECT tunnel failed`.
# Validating over TLS tests the capability we actually need.
_DEFAULT_TEST_URL = "https://httpbin.org/ip"

# Any non-error response proves the tunnel works; we don't care about the body.
_HTTP_ERROR_FLOOR = 400


def _proxyscrape_url(protocol: str | None, country: str | None) -> str:
    """Build a CDN path for the requested shard (protocol and/or country)."""
    if country and protocol:
        return f"{_CDN_BASE}/countries/{country.lower()}/{protocol}/data.json"
    if country:
        return f"{_CDN_BASE}/countries/{country.lower()}/data.json"
    if protocol:
        return f"{_CDN_BASE}/protocols/{protocol}/data.json"
    return f"{_CDN_BASE}/all/data.json"


def _first(record: dict[str, Any], *keys: str) -> Any:
    """Read the first present key — the upstream schema is not guaranteed stable."""
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _record_to_url(record: dict[str, Any]) -> str | None:
    protocol = _first(record, "protocol", "type")
    ip = _first(record, "ip", "host", "address")
    port = _first(record, "port")
    if not (protocol and ip and port):
        return None
    return f"{protocol}://{ip}:{port}"


def _passes_filters(
    record: dict[str, Any],
    *,
    min_uptime: float,
    anonymity: Sequence[str] | None,
    require_ssl: bool,
    max_latency_ms: float | None,
) -> bool:
    uptime = _first(record, "uptime", "uptime_percent")
    if uptime is not None and float(uptime) < min_uptime:
        return False
    if anonymity:
        level = str(_first(record, "anonymity", "anonymity_level") or "").lower()
        if level and level not in {a.lower() for a in anonymity}:
            return False
    if require_ssl:
        ssl_ok = _first(record, "ssl", "https", "supports_ssl")
        if ssl_ok is False:
            return False
    if max_latency_ms is not None:
        latency = _first(record, "latency", "latency_ms", "timeout")
        if latency is not None and float(latency) > max_latency_ms:
            return False
    return True


async def fetch_proxyscrape(
    *,
    protocols: Iterable[str] = _DEFAULT_PROTOCOLS,
    country: str | None = None,
    min_uptime: float = 50.0,
    anonymity: Sequence[str] | None = ("elite", "anonymous"),
    require_ssl: bool = False,
    max_latency_ms: float | None = 5_000.0,
    limit: int | None = 100,
    timeout_s: float = 20.0,
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Fetch and filter proxy URLs from the ProxyScrape mirror.

    Defaults are deliberately conservative (elite/anonymous, >=50% uptime,
    <=5 s latency, first 100) — a smaller good-ish set beats a huge dead one.
    """
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    urls: list[str] = []
    try:
        for protocol in protocols:
            endpoint = _proxyscrape_url(protocol, country)
            try:
                response = await http.get(endpoint)
                response.raise_for_status()
                records = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                _logger.warning(
                    "proxy.source.fetch_failed", endpoint=endpoint, error=str(exc)[:160]
                )
                continue
            if not isinstance(records, list):
                continue
            kept = 0
            for record in records:
                if not isinstance(record, dict):
                    continue
                if not _passes_filters(
                    record,
                    min_uptime=min_uptime,
                    anonymity=anonymity,
                    require_ssl=require_ssl,
                    max_latency_ms=max_latency_ms,
                ):
                    continue
                url = _record_to_url(record)
                if url and url not in urls:
                    urls.append(url)
                    kept += 1
            _logger.info(
                "proxy.source.fetched",
                protocol=protocol,
                total=len(records),
                kept=kept,
            )
    finally:
        if owns_client:
            await http.aclose()

    if limit is not None:
        # Log what we dropped rather than silently truncating.
        if len(urls) > limit:
            _logger.info("proxy.source.limited", kept=limit, dropped=len(urls) - limit)
        urls = urls[:limit]
    return urls


async def validate_proxies(
    urls: Iterable[str],
    *,
    test_url: str = _DEFAULT_TEST_URL,
    timeout_s: float = 8.0,
    concurrency: int = 20,
) -> list[str]:
    """Probe each proxy and return only the ones that actually work.

    Free-list uptime figures are optimistic; most entries are dead. This is a
    real request per proxy, bounded by ``concurrency``.
    """
    candidates = list(urls)
    if not candidates:
        return []
    semaphore = asyncio.Semaphore(concurrency)
    live: list[str] = []

    async def probe(proxy_url: str) -> None:
        async with semaphore:
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_url, timeout=timeout_s, follow_redirects=False
                ) as client:
                    response = await client.get(test_url)
                if response.status_code < _HTTP_ERROR_FLOOR:
                    live.append(proxy_url)
            except Exception:
                # Dead/slow/hostile proxy — that's the expected case here, and
                # the reason validation exists. Not worth logging each one.
                return

    await asyncio.gather(*(probe(u) for u in candidates))
    _logger.info("proxy.source.validated", tested=len(candidates), live=len(live))
    return live


async def load_proxyscrape_pool(
    *,
    validate: bool = True,
    cooldown_s: float | None = None,
    **fetch_kwargs: Any,
) -> ProxyPool | None:
    """Convenience: fetch (+ optionally validate) and build an **untrusted** pool.

    Returns ``None`` when nothing usable was found, matching
    :meth:`ProxyPool.from_env` so callers can fall back to a direct connection.
    """
    urls = await fetch_proxyscrape(**fetch_kwargs)
    if validate:
        urls = await validate_proxies(urls)
    if not urls:
        _logger.warning("proxy.source.empty", detail="no usable proxies after filtering")
        return None

    kwargs: dict[str, Any] = {"untrusted": True}
    if cooldown_s is not None:
        kwargs["cooldown_s"] = cooldown_s
    pool = ProxyPool.from_urls(urls, **kwargs)
    _logger.warning(
        "proxy.source.untrusted_pool_loaded",
        size=len(pool),
        detail="public/free proxies: pre-flagged by major anti-bot vendors and "
        "operated by unknown parties. Never use for credentialed traffic.",
    )
    return pool


__all__ = [
    "fetch_proxyscrape",
    "load_proxyscrape_pool",
    "validate_proxies",
]

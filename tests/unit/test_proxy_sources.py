"""Unit tests for external proxy-list loading. No live network.

The upstream JSON schema isn't contractually stable, so the parser is defensive —
these tests pin that behaviour (alternate key names, missing fields, non-list
payloads) as well as the metadata filtering and the untrusted-pool guarantee.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scrapper_tool import proxy_sources
from scrapper_tool.errors import ConfigurationError
from scrapper_tool.proxy import ProxyPool


def _record(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "protocol": "http",
        "ip": "1.2.3.4",
        "port": 8080,
        "uptime": 90.0,
        "anonymity": "elite",
        "ssl": True,
        "latency": 500,
    }
    base.update(over)
    return base


def _client(payload: Any, *, status: int = 200) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- parsing --------------------------------------------------------------


async def test_fetch_builds_protocol_ip_port_urls() -> None:
    async with _client([_record()]) as client:
        urls = await proxy_sources.fetch_proxyscrape(protocols=["http"], client=client)
    assert urls == ["http://1.2.3.4:8080"]


async def test_fetch_accepts_alternate_key_names() -> None:
    """Schema drift tolerance: host/type instead of ip/protocol."""
    rec = {"type": "socks5", "host": "9.9.9.9", "port": 1080, "uptime": 99}
    async with _client([rec]) as client:
        urls = await proxy_sources.fetch_proxyscrape(
            protocols=["socks5"], anonymity=None, client=client
        )
    assert urls == ["socks5://9.9.9.9:1080"]


async def test_fetch_skips_records_missing_required_fields() -> None:
    async with _client([{"protocol": "http", "port": 80}, _record()]) as client:
        urls = await proxy_sources.fetch_proxyscrape(protocols=["http"], client=client)
    assert urls == ["http://1.2.3.4:8080"]


async def test_fetch_tolerates_non_list_payload() -> None:
    async with _client({"unexpected": "shape"}) as client:
        urls = await proxy_sources.fetch_proxyscrape(protocols=["http"], client=client)
    assert urls == []


async def test_fetch_tolerates_http_error() -> None:
    async with _client([], status=503) as client:
        urls = await proxy_sources.fetch_proxyscrape(protocols=["http"], client=client)
    assert urls == []


async def test_fetch_dedupes_across_protocol_shards() -> None:
    async with _client([_record(), _record()]) as client:
        urls = await proxy_sources.fetch_proxyscrape(protocols=["http", "https"], client=client)
    assert urls == ["http://1.2.3.4:8080"]


# --- filtering ------------------------------------------------------------


async def test_min_uptime_filter() -> None:
    payload = [_record(uptime=10.0), _record(ip="5.5.5.5", uptime=95.0)]
    async with _client(payload) as client:
        urls = await proxy_sources.fetch_proxyscrape(
            protocols=["http"], min_uptime=50.0, client=client
        )
    assert urls == ["http://5.5.5.5:8080"]


async def test_anonymity_filter_drops_transparent() -> None:
    payload = [_record(anonymity="transparent"), _record(ip="5.5.5.5", anonymity="elite")]
    async with _client(payload) as client:
        urls = await proxy_sources.fetch_proxyscrape(
            protocols=["http"], anonymity=["elite"], client=client
        )
    assert urls == ["http://5.5.5.5:8080"]


async def test_require_ssl_filter() -> None:
    payload = [_record(ssl=False), _record(ip="5.5.5.5", ssl=True)]
    async with _client(payload) as client:
        urls = await proxy_sources.fetch_proxyscrape(
            protocols=["http"], require_ssl=True, client=client
        )
    assert urls == ["http://5.5.5.5:8080"]


async def test_max_latency_filter() -> None:
    payload = [_record(latency=9000), _record(ip="5.5.5.5", latency=100)]
    async with _client(payload) as client:
        urls = await proxy_sources.fetch_proxyscrape(
            protocols=["http"], max_latency_ms=1000, client=client
        )
    assert urls == ["http://5.5.5.5:8080"]


async def test_limit_caps_results() -> None:
    payload = [_record(ip=f"1.1.1.{i}") for i in range(10)]
    async with _client(payload) as client:
        urls = await proxy_sources.fetch_proxyscrape(protocols=["http"], limit=3, client=client)
    assert len(urls) == 3


# --- validation -----------------------------------------------------------


async def test_validate_keeps_only_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most free proxies are dead — validation must drop them."""

    class _FakeClient:
        def __init__(self, *, proxy: str, **_: Any) -> None:
            self.proxy = proxy

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, _url: str) -> Any:
            if "good" in self.proxy:
                return httpx.Response(200, json={})
            raise httpx.ConnectError("dead")

    monkeypatch.setattr(proxy_sources.httpx, "AsyncClient", _FakeClient)
    live = await proxy_sources.validate_proxies(
        ["http://good:1", "http://dead:2", "http://good2:3"]
    )
    assert sorted(live) == ["http://good2:3", "http://good:1"]


async def test_validate_empty_input_is_noop() -> None:
    assert await proxy_sources.validate_proxies([]) == []


# --- pool construction + the untrusted guarantee ---------------------------


async def test_loaded_pool_is_marked_untrusted(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(**_: Any) -> list[str]:
        return ["http://1.2.3.4:8080"]

    monkeypatch.setattr(proxy_sources, "fetch_proxyscrape", fake_fetch)
    pool = await proxy_sources.load_proxyscrape_pool(validate=False)
    assert pool is not None
    assert pool.untrusted is True
    assert len(pool) == 1


async def test_untrusted_pool_refuses_credentialed_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security boundary: strangers' proxies must never carry a session."""

    async def fake_fetch(**_: Any) -> list[str]:
        return ["http://1.2.3.4:8080"]

    monkeypatch.setattr(proxy_sources, "fetch_proxyscrape", fake_fetch)
    pool = await proxy_sources.load_proxyscrape_pool(validate=False)
    assert pool is not None
    with pytest.raises(ConfigurationError, match="untrusted"):
        pool.assert_safe_for_credentials()


def test_trusted_pool_allows_credentialed_use() -> None:
    ProxyPool.from_urls(["http://mine:1"]).assert_safe_for_credentials()  # no raise


async def test_load_returns_none_when_nothing_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_fetch(**_: Any) -> list[str]:
        return []

    monkeypatch.setattr(proxy_sources, "fetch_proxyscrape", fake_fetch)
    # None (not an empty pool) so callers fall back to a direct connection.
    assert await proxy_sources.load_proxyscrape_pool(validate=False) is None

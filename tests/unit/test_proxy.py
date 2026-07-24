"""Unit tests for the proxy pool (the IP-reputation dimension).

No live proxies are needed — the clock is injected so cooldowns can be tested
without sleeping, and rotation is pure bookkeeping.
"""

from __future__ import annotations

import pytest

from scrapper_tool.proxy import ProxyPool, resolve_proxy


class _Clock:
    """Manually-advanced monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _pool(*urls: str, cooldown_s: float = 300.0) -> tuple[ProxyPool, _Clock]:
    clock = _Clock()
    pool = ProxyPool.from_urls(urls, cooldown_s=cooldown_s, time_fn=clock)
    return pool, clock


# --- construction ---------------------------------------------------------


def test_from_urls_dedupes_and_skips_comments_and_blanks() -> None:
    pool = ProxyPool.from_urls(["http://a:1", "  ", "# comment", "http://a:1", "socks5://b:2"])
    assert [e.url for e in pool.entries] == ["http://a:1", "socks5://b:2"]


def test_from_env_returns_none_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRAPPER_TOOL_PROXIES", raising=False)
    monkeypatch.delenv("SCRAPPER_TOOL_PROXY_FILE", raising=False)
    # None (not an empty pool) so callers keep their previous direct-connection path.
    assert ProxyPool.from_env() is None


def test_from_env_parses_comma_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_PROXIES", "http://a:1,http://b:2")
    monkeypatch.delenv("SCRAPPER_TOOL_PROXY_FILE", raising=False)
    pool = ProxyPool.from_env()
    assert pool is not None
    assert len(pool) == 2


def test_from_env_reads_file(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from pathlib import Path

    f = Path(str(tmp_path)) / "proxies.txt"
    f.write_text("http://f1:1\n# skip me\nhttp://f2:2\n", encoding="utf-8")
    monkeypatch.delenv("SCRAPPER_TOOL_PROXIES", raising=False)
    monkeypatch.setenv("SCRAPPER_TOOL_PROXY_FILE", str(f))
    pool = ProxyPool.from_env()
    assert pool is not None
    assert [e.url for e in pool.entries] == ["http://f1:1", "http://f2:2"]


def test_from_env_tolerates_unreadable_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_PROXIES", "http://a:1")
    monkeypatch.setenv("SCRAPPER_TOOL_PROXY_FILE", "/nonexistent/nope.txt")
    pool = ProxyPool.from_env()  # must not raise
    assert pool is not None
    assert len(pool) == 1


# --- rotation -------------------------------------------------------------


def test_next_proxy_round_robins() -> None:
    pool, _ = _pool("http://a:1", "http://b:2", "http://c:3")
    assert [pool.next_proxy() for _ in range(4)] == [
        "http://a:1",
        "http://b:2",
        "http://c:3",
        "http://a:1",
    ]


def test_empty_pool_returns_none() -> None:
    pool, _ = _pool()
    assert pool.next_proxy() is None


# --- health accounting ----------------------------------------------------


def test_blocked_proxy_is_skipped_until_cooldown_expires() -> None:
    pool, clock = _pool("http://a:1", "http://b:2", cooldown_s=100.0)
    pool.mark_blocked("http://a:1")
    # a is cooling down -> only b is offered
    assert pool.next_proxy() == "http://b:2"
    assert pool.next_proxy() == "http://b:2"
    assert pool.available_count() == 1

    clock.advance(101.0)
    assert pool.available_count() == 2
    assert "http://a:1" in {pool.next_proxy(), pool.next_proxy()}


def test_all_blocked_returns_none_so_caller_can_fall_back() -> None:
    pool, _ = _pool("http://a:1", "http://b:2")
    pool.mark_blocked("http://a:1")
    pool.mark_blocked("http://b:2")
    assert pool.available_count() == 0
    # None is a meaningful answer: go direct or give up, caller's choice.
    assert pool.next_proxy() is None


def test_repeat_blocks_compound_the_cooldown() -> None:
    pool, clock = _pool("http://a:1", cooldown_s=100.0)
    for _ in range(5):
        pool.mark_blocked("http://a:1")
    entry = pool.entries[0]
    # Past max_failures the cooldown multiplies, so a burned proxy stops eating slots.
    assert entry.cooldown_until - clock.now > 100.0


def test_mark_ok_clears_failure_streak() -> None:
    pool, _ = _pool("http://a:1", cooldown_s=100.0)
    pool.mark_blocked("http://a:1")
    pool.mark_ok("http://a:1")
    assert pool.entries[0].failures == 0
    assert pool.available_count() == 1


def test_mark_helpers_ignore_unknown_and_none() -> None:
    pool, _ = _pool("http://a:1")
    pool.mark_blocked(None)
    pool.mark_ok(None)
    pool.mark_blocked("http://not-in-pool:9")
    assert pool.entries[0].failures == 0


def test_stats_redacts_credentials() -> None:
    pool, _ = _pool("http://user:secret@host:8080")
    rendered = str(pool.stats())
    assert "secret" not in rendered
    assert "***@host:8080" in rendered


# --- resolve_proxy --------------------------------------------------------


def test_resolve_proxy_prefers_explicit_and_leaves_pool_unmanaged() -> None:
    pool, _ = _pool("http://pool:1")
    chosen, managed = resolve_proxy(pool, "http://explicit:1")
    assert chosen == "http://explicit:1"
    # Explicit choice isn't pool-managed, so health must NOT be reported back.
    assert managed is None


def test_resolve_proxy_uses_pool_when_no_explicit() -> None:
    pool, _ = _pool("http://pool:1")
    chosen, managed = resolve_proxy(pool, None)
    assert chosen == "http://pool:1"
    assert managed is pool


def test_resolve_proxy_without_pool_is_direct() -> None:
    assert resolve_proxy(None, None) == (None, None)

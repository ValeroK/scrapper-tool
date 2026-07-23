"""Proxy pool with health accounting — the IP-reputation layer.

Why this exists: the impersonation ladder rotates *TLS fingerprints*, which is
the right first move (anti-bot edges read the ClientHello before any header).
But TLS rotation cannot recover a **burned IP**. Observed directly during
development: a stealth browser that cleared a Radware wall cleanly in the morning
was challenged on the same URL hours later, purely because repeated automated
requests had flagged the egress IP. No amount of fingerprint rotation fixes that.

So proxy rotation is a *complementary* dimension, not an alternative:

    TLS/JA3 fingerprint  ──┐
                           ├── both must look plausible
    IP reputation        ──┘

The pool is deliberately simple and dependency-free: round-robin over healthy
entries, exponential-ish cooldown on block, and a clear "everything is cooling
down" signal so the caller can decide whether to go direct or fail.

Configuration (either):
    SCRAPPER_TOOL_PROXIES=http://a:1,socks5://b:2      # comma/whitespace list
    SCRAPPER_TOOL_PROXY_FILE=/path/to/proxies.txt      # one per line, # comments
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from scrapper_tool._logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

_logger = get_logger(__name__)

# A blocked proxy rests this long before it's offered again. Long enough that a
# rate-limit window elapses, short enough that a small pool stays usable.
_DEFAULT_COOLDOWN_S = 300.0
# Consecutive blocks before the cooldown starts compounding.
_DEFAULT_MAX_FAILURES = 3


@dataclass
class ProxyEntry:
    """One proxy URL plus its health state."""

    url: str
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0

    def available(self, now: float) -> bool:
        return now >= self.cooldown_until


@dataclass
class ProxyPool:
    """Round-robin proxy pool with per-entry cooldown on block.

    ``time_fn`` is injectable so tests can advance the clock without sleeping.
    """

    entries: list[ProxyEntry] = field(default_factory=list)
    cooldown_s: float = _DEFAULT_COOLDOWN_S
    max_failures: int = _DEFAULT_MAX_FAILURES
    time_fn: Callable[[], float] = time.monotonic
    _cursor: int = 0

    # --- construction -----------------------------------------------------

    @classmethod
    def from_urls(cls, urls: Iterable[str], **kwargs: object) -> ProxyPool:
        seen: list[str] = []
        for raw in urls:
            candidate = raw.strip()
            if candidate and not candidate.startswith("#") and candidate not in seen:
                seen.append(candidate)
        return cls(entries=[ProxyEntry(url=u) for u in seen], **kwargs)  # type: ignore[arg-type]

    @classmethod
    def from_env(cls, **kwargs: object) -> ProxyPool | None:
        """Build from ``SCRAPPER_TOOL_PROXIES`` / ``SCRAPPER_TOOL_PROXY_FILE``.

        Returns ``None`` when neither is configured — callers then behave exactly
        as before (direct connection, or an explicitly passed single proxy).
        """
        raw = os.environ.get("SCRAPPER_TOOL_PROXIES", "").replace(",", "\n")
        path = os.environ.get("SCRAPPER_TOOL_PROXY_FILE")
        if path:
            try:
                raw += "\n" + Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                _logger.warning("proxy.pool.file_unreadable", path=path, error=str(exc))
        pool = cls.from_urls(raw.splitlines(), **kwargs)
        if not pool.entries:
            return None
        _logger.info("proxy.pool.loaded", size=len(pool.entries))
        return pool

    # --- rotation ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.entries)

    def available_count(self) -> int:
        now = self.time_fn()
        return sum(1 for e in self.entries if e.available(now))

    def next_proxy(self) -> str | None:
        """Return the next healthy proxy URL, or ``None`` if all are cooling down.

        ``None`` is a meaningful answer, not an error: the caller decides whether
        to fall back to a direct connection or give up.
        """
        if not self.entries:
            return None
        now = self.time_fn()
        count = len(self.entries)
        for offset in range(count):
            entry = self.entries[(self._cursor + offset) % count]
            if entry.available(now):
                self._cursor = (self._cursor + offset + 1) % count
                return entry.url
        _logger.warning("proxy.pool.exhausted", size=count)
        return None

    # --- health accounting -------------------------------------------------

    def _find(self, url: str) -> ProxyEntry | None:
        return next((e for e in self.entries if e.url == url), None)

    def mark_blocked(self, url: str | None) -> None:
        """Record that ``url`` was blocked and put it on cooldown.

        Repeat offenders cool down progressively longer, so a permanently-burned
        proxy stops eating rotation slots without being removed outright.
        """
        if url is None:
            return
        entry = self._find(url)
        if entry is None:
            return
        entry.failures += 1
        multiplier = max(1, entry.failures - self.max_failures + 1)
        entry.cooldown_until = self.time_fn() + self.cooldown_s * multiplier
        _logger.warning(
            "proxy.pool.blocked",
            proxy=_redact(url),
            failures=entry.failures,
            cooldown_s=self.cooldown_s * multiplier,
        )

    def mark_ok(self, url: str | None) -> None:
        """Record a success — clears the failure streak so the entry stays hot."""
        if url is None:
            return
        entry = self._find(url)
        if entry is None:
            return
        entry.successes += 1
        entry.failures = 0
        entry.cooldown_until = 0.0

    def stats(self) -> list[dict[str, object]]:
        now = self.time_fn()
        return [
            {
                "proxy": _redact(e.url),
                "failures": e.failures,
                "successes": e.successes,
                "available": e.available(now),
            }
            for e in self.entries
        ]


def _redact(url: str) -> str:
    """Strip credentials before logging — proxy URLs often embed user:pass."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def resolve_proxy(
    pool: ProxyPool | None, explicit: str | None
) -> tuple[str | None, ProxyPool | None]:
    """Pick a proxy for one attempt.

    An explicitly-passed proxy always wins (callers pinning a proxy mean it); the
    pool is only consulted when none was given. Returns the chosen proxy plus the
    pool it came from (``None`` when the choice wasn't pool-managed), so the
    caller knows whether to report health back.
    """
    if explicit:
        return explicit, None
    if pool is None:
        return None, None
    return pool.next_proxy(), pool


__all__ = [
    "ProxyEntry",
    "ProxyPool",
    "resolve_proxy",
]

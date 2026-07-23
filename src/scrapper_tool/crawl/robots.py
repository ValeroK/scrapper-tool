"""robots.txt fetching, caching, and enforcement — opt-in.

``AgentConfig.respect_robots`` defaults to **False**: this toolkit's job is to
retrieve pages its operator asks for, and robots.txt is a crawling convention
rather than an access control. This module is what makes the setting mean
something when a deployment does turn it on, rather than the startup log line it
used to be.

One piece is worth reading even when enforcement is off: ``Crawl-delay`` protects
the *caller* as much as the site. Hammering a host is how an IP earns a reputation
score, and that is the one form of blocking no amount of TLS impersonation or
fingerprint work recovers from — measured on this project, an address that passed
a hard target in the morning was challenged on the identical URL hours later.

Two deliberate choices in the implementation:

**Fetched with httpx, parsed with the stdlib.** ``RobotFileParser.read()`` calls
``urlopen`` synchronously, which would block the event loop for every new host in
a concurrent crawl. Fetching separately and handing the text to ``parse()`` keeps
the well-tested matching logic without the blocking I/O.

**Status codes follow RFC 9309.** 4xx (including the 403 that anti-bot systems
often serve for robots.txt) means "no rules published" and everything is allowed;
5xx means the site is telling us it can't answer, which the RFC says to treat as
a full disallow. Guessing "allow" on a 5xx would be the one case where being
wrong means hammering a struggling server — and a struggling server is one that
starts returning 403s to everyone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from scrapper_tool._logging import get_logger

_logger = get_logger(__name__)

_DEFAULT_TTL_S = 3600.0
_FETCH_TIMEOUT_S = 10.0
_CLIENT_ERROR_FLOOR = 400
_SERVER_ERROR_FLOOR = 500
# robots.txt is meant to be small; a multi-MB response is a misconfigured server
# or a trap, and parsing it would cost more than the crawl it governs.
_MAX_ROBOTS_BYTES = 512 * 1024

# What we identify as when asking robots.txt about ourselves. Matching on "*" as
# well is what `RobotFileParser` does anyway; this is the token a site would use
# to single us out.
DEFAULT_USER_AGENT = "scrapper-tool"


@dataclass
class _Entry:
    parser: RobotFileParser | None  # None => unreachable, treat as disallow-all
    fetched_at: float
    reachable: bool
    crawl_delay_s: float = 0.0


def parse_crawl_delay(body: str, user_agent: str) -> float:
    """Extract ``Crawl-delay`` for ``user_agent``, honouring fractional values.

    Needed because ``RobotFileParser`` parses this directive with ``int()`` and
    silently discards anything that isn't a whole number — so a site asking for
    ``Crawl-delay: 0.5`` gets treated as asking for nothing. Fractional delays are
    common, and ignoring them makes us less polite than we claim to be.

    An exact user-agent match wins over ``*``, matching robots.txt group
    semantics: a rule written for us specifically is the one that applies.
    """
    wanted = user_agent.lower()
    groups: list[tuple[bool, float]] = []  # (is_exact_match, delay)
    agents: list[str] = []
    pending_delay: float | None = None

    def flush() -> None:
        if pending_delay is None or not agents:
            return
        groups.append((wanted in agents, pending_delay))

    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if pending_delay is not None:
                flush()
                agents = []
                pending_delay = None
            agents.append(value.lower())
        elif field == "crawl-delay" and agents:
            try:
                pending_delay = max(0.0, float(value))
            except ValueError:
                continue
    flush()

    if not groups:
        return 0.0
    exact = [delay for is_exact, delay in groups if is_exact]
    return exact[0] if exact else groups[0][1]


@dataclass
class RobotsCache:
    """Per-origin robots.txt cache with TTL.

    One fetch per origin per hour, not one per URL — a crawl over 500 pages of
    one site must not make 500 robots.txt requests.
    """

    ttl_s: float = _DEFAULT_TTL_S
    user_agent: str = DEFAULT_USER_AGENT
    timeout_s: float = _FETCH_TIMEOUT_S
    _entries: dict[str, _Entry] = field(default_factory=dict, repr=False)

    async def _entry_for(self, url: str, client: httpx.AsyncClient | None = None) -> _Entry:
        origin = _origin(url)
        cached = self._entries.get(origin)
        if cached is not None and (time.monotonic() - cached.fetched_at) < self.ttl_s:
            return cached
        entry = await self._fetch(origin, client)
        self._entries[origin] = entry
        return entry

    async def _fetch(self, origin: str, client: httpx.AsyncClient | None) -> _Entry:
        robots_url = f"{origin}/robots.txt"
        owns_client = client is None
        http = client or httpx.AsyncClient(timeout=self.timeout_s, follow_redirects=True)
        try:
            response = await http.get(robots_url, headers={"User-Agent": self.user_agent})
        except httpx.HTTPError as exc:
            # Couldn't reach it at all. Same posture as a 5xx: the site hasn't
            # told us we may crawl, so we don't.
            _logger.info("crawl.robots.unreachable", origin=origin, error=str(exc)[:120])
            return _Entry(parser=None, fetched_at=time.monotonic(), reachable=False)
        finally:
            if owns_client:
                await http.aclose()

        if response.status_code >= _SERVER_ERROR_FLOOR:
            _logger.info("crawl.robots.server_error", origin=origin, status=response.status_code)
            return _Entry(parser=None, fetched_at=time.monotonic(), reachable=False)

        parser = RobotFileParser()
        parser.set_url(robots_url)
        delay = 0.0
        if response.status_code >= _CLIENT_ERROR_FLOOR:
            # No rules published (or the anti-bot 403'd us). RFC 9309: allow.
            parser.parse([])
        else:
            body = response.text[:_MAX_ROBOTS_BYTES]
            parser.parse(body.splitlines())
            delay = parse_crawl_delay(body, self.user_agent)
        return _Entry(
            parser=parser, fetched_at=time.monotonic(), reachable=True, crawl_delay_s=delay
        )

    async def allowed(self, url: str, *, client: httpx.AsyncClient | None = None) -> bool:
        """Whether ``url`` may be fetched under this origin's robots.txt."""
        entry = await self._entry_for(url, client)
        if entry.parser is None:
            return False  # unreachable robots.txt => don't crawl
        return bool(entry.parser.can_fetch(self.user_agent, url))

    async def crawl_delay(self, url: str, *, client: httpx.AsyncClient | None = None) -> float:
        """Seconds a site asked us to wait between requests (0 when unset).

        Honouring this is most of the difference between a crawler and a
        nuisance, and it costs nothing since robots.txt is already parsed.
        """
        entry = await self._entry_for(url, client)
        return entry.crawl_delay_s if entry.parser is not None else 0.0

    async def sitemaps(self, url: str, *, client: httpx.AsyncClient | None = None) -> list[str]:
        """``Sitemap:`` URLs declared in robots.txt.

        The canonical way to find a site's sitemaps — more reliable than guessing
        ``/sitemap.xml``, and it's where large sites point at their real indexes.
        """
        entry = await self._entry_for(url, client)
        if entry.parser is None:
            return []
        return list(entry.parser.site_maps() or [])

    def clear(self) -> None:
        self._entries.clear()


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc, "", "", ""))


__all__ = [
    "DEFAULT_USER_AGENT",
    "RobotsCache",
    "parse_crawl_delay",
]

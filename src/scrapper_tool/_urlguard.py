"""Pre-flight URL vetting — refuse a target before a byte leaves the process.

Nothing in this library validated a URL's host before v2.2.1. Every public
surface takes a caller-supplied URL and fetches it, and the REST sidecar's
API key is optional, so a sidecar reachable on a network was a general-purpose
request forwarder pointed at whatever the caller named: cloud metadata
(``169.254.169.254``), any RFC1918 host, anything listening on loopback.

Two questions, deliberately separated, mirroring :mod:`scrapper_tool._challenge`:

1. **Is this URL allowed?** :func:`check_url` — synchronous, no I/O, pure
   classification of the literal URL. Cheap enough to run on every link a
   crawler discovers.
2. **Where does this host actually point?** :func:`resolve_and_check` — adds
   DNS resolution, because ``evil.example.com`` resolving to ``10.0.0.1`` is
   the whole attack and no amount of string inspection finds it.

**Probes return, they never raise.** ``check_*`` and ``resolve_and_check``
hand back a :class:`GuardVerdict`; only :func:`assert_url_allowed` raises.
That split is lifted from :mod:`scrapper_tool._extras` ("every probe returns a
value; none raise") and it is what makes the classifier a table of cases
rather than a pile of try/except.

Import discipline
-----------------
**Stdlib only, plus this package's own ``errors`` and ``_logging``.** No
httpx, no pydantic. ``doctor``, ``cli``, ``mcp``, ``http_server``, ``crawl.*``,
``ladder`` and ``http`` all import this module, and ``mcp`` must never
transitively pull in FastAPI. The httpx-aware transport wrapper therefore
lives in :mod:`scrapper_tool.http`, which already owns httpx, and this module
stays importable on a bare install.

What this module does not close
-------------------------------
A pre-flight check cannot see the hops a redirect-following client makes on
its own; ``follow_redirects`` is set on every client we build. Per-hop
enforcement is the caller's job — see ``http.guarded_transport`` — and this
module only supplies the verdict. DNS-pinning (resolve once, then connect to
the pinned IP with the hostname preserved in ``Host:``) would close the
remaining resolve-then-connect race, and is deliberately **not** done: it
breaks TLS SNI, which breaks the impersonation fingerprint that Pattern A/B/C
exists to protect. That race is accepted and documented rather than traded
for the thing this library is for.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, get_args
from urllib.parse import urlsplit

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import UrlNotAllowed

if TYPE_CHECKING:
    from collections.abc import Iterable

_logger = get_logger(__name__)

#: Why a URL was refused. ``"ok"`` is the only allowing value; every other
#: member has an entry in :data:`REFUSAL_REMEDIES`, asserted by a test.
Reason = Literal[
    "ok",
    "scheme",
    "no_host",
    "userinfo",
    "idna",
    "loopback",
    "private_ip",
    "link_local",
    "metadata",
    "cgnat",
    "reserved",
    "multicast",
    "unspecified",
    "benchmark",
    "special_tld",
    "unresolvable",
]

#: Operator-facing remediation, keyed by :data:`Reason`. Same role — and same
#: reason for existing — as ``_extras.INSTALL_HINTS``: one dict, so the strings
#: cannot drift across the sidecar, the MCP server and ``doctor``.
REFUSAL_REMEDIES: Final[dict[str, str]] = {
    "scheme": "use an http:// or https:// URL — other schemes are never fetched",
    "no_host": "the URL has no host; pass an absolute URL like https://example.com/path",
    "userinfo": (
        "remove the user:pass@ prefix — credentials in a URL are a common way to "
        "disguise the real host, so the whole URL is refused rather than parsed"
    ),
    "idna": "the hostname is not valid IDNA; check for mixed-script or invisible characters",
    "loopback": (
        "loopback targets are blocked by design — use a public URL, or add this host to "
        "SCRAPPER_TOOL_URL_GUARD_ALLOW if you are deliberately scraping a local fixture server"
    ),
    "private_ip": (
        "private-network targets are blocked by design — use a public URL, or allowlist the "
        "range with SCRAPPER_TOOL_URL_GUARD_ALLOW=10.0.0.0/8 if this is an authorised "
        "internal scrape"
    ),
    "link_local": "link-local targets are blocked by design — use a routable public address",
    "metadata": (
        "cloud instance-metadata endpoints are blocked by design; they hand out credentials "
        "and nothing legitimately scrapes them through this library"
    ),
    "cgnat": "carrier-grade NAT space is blocked by design — use a routable public address",
    "reserved": "reserved address space is blocked by design — use a routable public address",
    "multicast": "multicast addresses are not fetchable — use a unicast public address",
    "unspecified": "the unspecified address (0.0.0.0 / ::) is not a target — name a real host",
    "benchmark": "benchmark/test address space is blocked by design — use a real public address",
    "special_tld": (
        "special-use domain suffixes (.local, .internal, .onion, ...) resolve only inside a "
        "private network and are blocked by design"
    ),
    "unresolvable": "",  # allowed; see _resolve_all
}

# --- Address space the stdlib's own properties do not cover -----------------

# ``ipaddress`` flags loopback/private/link-local/reserved/multicast for us.
# These are the ranges it does not, each of which routes somewhere we should
# never send a caller's URL.
_EXTRA_NETWORKS: Final[tuple[tuple[str, str], ...]] = (
    ("100.64.0.0/10", "cgnat"),  # RFC 6598 carrier-grade NAT
    ("192.0.0.0/24", "reserved"),  # RFC 6890 IETF protocol assignments
    ("198.18.0.0/15", "benchmark"),  # RFC 2544 benchmarking
    ("240.0.0.0/4", "reserved"),  # RFC 1112 future use
    ("255.255.255.255/32", "reserved"),  # limited broadcast
    ("64:ff9b::/96", "reserved"),  # RFC 6052 NAT64 — embeds an IPv4 target
    ("64:ff9b:1::/48", "reserved"),  # RFC 8215 local-use NAT64
    ("2002::/16", "reserved"),  # 6to4 — embeds an IPv4 target
    ("2001:db8::/32", "reserved"),  # documentation
)

_PARSED_EXTRA_NETWORKS: Final[
    tuple[tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, str], ...]
] = tuple((ipaddress.ip_network(cidr), reason) for cidr, reason in _EXTRA_NETWORKS)

#: Instance-metadata endpoints. ``169.254.169.254`` is already link-local, but
#: it earns its own reason: the remedy differs from a generic link-local hit,
#: and this is the address an operator greps the logs for after an incident.
_METADATA_IPS: Final[frozenset[str]] = frozenset(
    {
        "169.254.169.254",  # AWS IMDSv1/v2, GCP, Azure, DigitalOcean, Oracle
        "fd00:ec2::254",  # AWS IMDS over IPv6
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud legacy
    }
)

#: Metadata endpoints reachable by name. Checked before DNS, because the point
#: is to refuse without emitting a resolver query at all.
_METADATA_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

#: Suffixes reserved for *private use* — a name under one of these resolves
#: only inside somebody's network (RFC 6761/7686 and the de-facto ones).
#: ``.local`` matters most: mDNS can answer with something that looks
#: perfectly routable.
#:
#: Deliberately absent: ``.test``, ``.invalid`` and ``.example``. Those are
#: reserved for the opposite purpose — they are guaranteed *never* to resolve,
#: so they cannot reach private infrastructure and refusing them protects
#: nothing. ``.test`` in particular is the TLD RFC 6761 sets aside for testing,
#: which is exactly what a hermetic test suite should be using. And if someone
#: does run a local resolver mapping ``*.test`` at 127.0.0.1, the DNS check
#: catches it on the address, which is the honest place to catch it.
_SPECIAL_SUFFIXES: Final[tuple[str, ...]] = (
    ".local",
    ".localhost",
    ".internal",
    ".intranet",
    ".corp",
    ".home",
    ".home.arpa",
    ".lan",
    ".onion",
    ".i2p",
)

_SPECIAL_EXACT: Final[frozenset[str]] = frozenset({"localhost", "local", "internal"})

_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})

# Bare-integer and dotted-short-form IPv4. ``IPv4Address`` rejects all of
# these, so without normalisation they fall through to the hostname branch —
# where the platform resolver happily turns 2130706433 into 127.0.0.1.
_NUMERIC_HOST_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)"
    r"(?:\.(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)){0,3}$"
)

_MAX_IPV4: Final[int] = 0xFFFF_FFFF
_MAX_OCTET: Final[int] = 0xFF


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    """Resolved guard configuration for one check."""

    enabled: bool = True
    allow_hosts: frozenset[str] = field(default_factory=frozenset)
    allow_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()
    resolve_dns: bool = True


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """The answer, plus enough context to explain it to a human."""

    allowed: bool
    reason: Reason
    host: str = ""
    resolved: tuple[str, ...] = ()

    @property
    def remedy(self) -> str:
        """Operator-facing fix for this refusal, or ``""`` when allowed."""
        if self.allowed:
            return ""
        return REFUSAL_REMEDIES.get(self.reason, "")

    def message(self) -> str:
        """One-line description suitable for an exception or an error payload."""
        where = f" ({self.host})" if self.host else ""
        return f"refused target URL{where}: {self.reason}"


_ALLOWED: Final[GuardVerdict] = GuardVerdict(allowed=True, reason="ok")

# Set once the "guard is off" warning has been emitted, so a disabled guard
# says so exactly once per process instead of once per fetch.
_warned_disabled = False


def url_guard_enabled() -> bool:
    """Guard is on by default; ``SCRAPPER_TOOL_URL_GUARD=0`` disables it.

    On-by-default is deliberate, and is the one behavioural change v2.2.1
    makes: a security control that has to be switched on is not a control.
    The escape hatch for legitimate private targets is
    ``SCRAPPER_TOOL_URL_GUARD_ALLOW``, which keeps the guard running.
    """
    raw = os.environ.get("SCRAPPER_TOOL_URL_GUARD")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in _TRUTHY


def _dns_enabled() -> bool:
    raw = os.environ.get("SCRAPPER_TOOL_URL_GUARD_DNS")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in _TRUTHY


def _parse_allowlist(
    raw: str,
) -> tuple[frozenset[str], tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]]:
    hosts: set[str] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for chunk in raw.replace("\n", ",").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        hosts.add(entry.lower().rstrip("."))
    return frozenset(hosts), tuple(networks)


def guard_policy() -> GuardPolicy:
    """Build the active policy from the environment.

    Read at call time, never cached at import, so tests and long-lived
    sidecars both see ``monkeypatch.setenv`` take effect — the same
    discipline every other setting in this package follows.
    """
    enabled = url_guard_enabled()
    raw_allow = os.environ.get("SCRAPPER_TOOL_URL_GUARD_ALLOW", "")
    hosts, networks = _parse_allowlist(raw_allow)
    return GuardPolicy(
        enabled=enabled,
        allow_hosts=hosts,
        allow_networks=networks,
        resolve_dns=_dns_enabled(),
    )


def _warn_disabled_once() -> None:
    global _warned_disabled  # noqa: PLW0603 — one-shot process-lifetime latch
    if _warned_disabled:
        return
    _warned_disabled = True
    _logger.warning(
        "urlguard.disabled",
        detail="SCRAPPER_TOOL_URL_GUARD=0 — target URLs are no longer checked, so this "
        "process will fetch loopback, private and cloud-metadata addresses on request",
        remedy="prefer SCRAPPER_TOOL_URL_GUARD_ALLOW=<host-or-cidr> to permit specific "
        "internal targets while keeping the guard on",
    )


def _parse_ipv4_part(part: str) -> int | None:
    """Parse one dotted-quad component in C ``inet_aton`` spellings.

    Note ``int(part, 0)`` is not usable here: Python 3 rejects legacy octal
    (``0177``), which is exactly the spelling an attacker reaches for.
    """
    if not part:
        return None
    try:
        if part[:2].lower() == "0x":
            return int(part, 16)
        if part[0] == "0" and len(part) > 1:
            return int(part, 8)
        return int(part, 10)
    except ValueError:
        return None


def _normalise_numeric_host(host: str) -> ipaddress.IPv4Address | None:
    """Canonicalise the IPv4 spellings ``IPv4Address`` refuses.

    ``2130706433``, ``0x7f000001``, ``0177.0.0.1`` and ``127.1`` are all
    loopback to the platform resolver but all raise in :mod:`ipaddress`. Left
    alone they would be classified as *hostnames*, skip every IP rule below,
    and be handed straight to ``getaddrinfo``. DNS resolution (see
    :func:`resolve_and_check`) catches them too; this is the belt to that
    pair of braces, and it works even with resolution disabled.
    """
    if not _NUMERIC_HOST_RE.match(host):
        return None
    values: list[int] = []
    for part in host.split("."):
        value = _parse_ipv4_part(part)
        if value is None:
            return None
        values.append(value)
    # a.b.c.d, a.b.c (d 16-bit), a.b (b 24-bit), a (32-bit) — inet_aton rules.
    packed = 0
    count = len(values)
    if count == 1:
        packed = values[0]
    else:
        head, tail = values[:-1], values[-1]
        if any(octet > _MAX_OCTET for octet in head):
            return None
        width = 8 * (5 - count)
        if tail >= (1 << width):
            return None
        for octet in head:
            packed = (packed << 8) | octet
        packed = (packed << width) | tail
    if packed > _MAX_IPV4:
        return None
    return ipaddress.IPv4Address(packed)


def _unwrap_ipv6(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """Extract the IPv4 address an IPv6 address embeds, if any.

    ``IPv6Address("::ffff:127.0.0.1").is_loopback`` is **False** — the
    loopback test looks for ``::1`` and nothing else. So a mapped, 6to4 or
    Teredo address carrying a private IPv4 payload passes every stdlib
    property. Unwrap and re-classify the payload, or these are a free bypass.
    """
    for attr in ("ipv4_mapped", "sixtofour", "teredo"):
        value = getattr(ip, attr, None)
        if value is None:
            continue
        # ``teredo`` is a (server, client) pair; the client is the interesting half.
        if isinstance(value, tuple):
            value = value[1] if len(value) > 1 else value[0]
        if isinstance(value, ipaddress.IPv4Address):
            return value
    return None


def _classify_ip(  # noqa: PLR0911, PLR0912 — one branch per refusal reason, which is the point
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address, policy: GuardPolicy
) -> Reason:
    """Return ``"ok"`` or the reason this address must not be fetched."""
    if any(ip in network for network in policy.allow_networks):
        return "ok"
    if str(ip) in _METADATA_IPS:
        return "metadata"
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _unwrap_ipv6(ip)
        if embedded is not None:
            embedded_reason = _classify_ip(embedded, policy)
            # Report the embedded verdict when it refuses — "loopback" tells an
            # operator what ``::ffff:7f00:1`` actually points at, where
            # "reserved" would not. When the payload is a public address, fall
            # through: 6to4 and NAT64 wrappers are themselves not fetchable, so
            # the range rules below still have to run.
            if embedded_reason != "ok":
                return embedded_reason
    for network, reason in _PARSED_EXTRA_NETWORKS:
        if ip.version == network.version and ip in network:
            return reason  # type: ignore[return-value]
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private:
        return "private_ip"
    if ip.is_reserved:
        return "reserved"
    return "ok"


def _normalise_host(host: str) -> str | None:
    """Casefold, strip one trailing dot, and IDNA-encode. ``None`` if invalid."""
    cleaned = unicodedata.normalize("NFKC", host).strip().rstrip(".").lower()
    if not cleaned:
        return None
    if cleaned.isascii():
        return cleaned
    try:
        return cleaned.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return None


def check_host(  # noqa: PLR0911 — one return per refusal reason
    host: str, *, policy: GuardPolicy | None = None
) -> GuardVerdict:
    """Classify a bare hostname or IP literal. Never raises, never resolves."""
    active = policy if policy is not None else guard_policy()
    if not active.enabled:
        _warn_disabled_once()
        return _ALLOWED

    normalised = _normalise_host(host)
    if normalised is None:
        return GuardVerdict(allowed=False, reason="idna", host=host)

    if normalised in active.allow_hosts:
        return GuardVerdict(allowed=True, reason="ok", host=normalised)

    if normalised in _METADATA_HOSTS:
        return GuardVerdict(allowed=False, reason="metadata", host=normalised)

    # Bracketed IPv6 arrives here only when a caller passes netloc directly;
    # urlsplit().hostname has already stripped them.
    unbracketed = normalised.removeprefix("[").removesuffix("]")
    try:
        literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(
            unbracketed
        )
    except ValueError:
        literal = _normalise_numeric_host(unbracketed)

    if literal is not None:
        reason = _classify_ip(literal, active)
        if reason != "ok":
            return GuardVerdict(allowed=False, reason=reason, host=normalised)
        return GuardVerdict(allowed=True, reason="ok", host=normalised)

    if normalised in _SPECIAL_EXACT or normalised.endswith(_SPECIAL_SUFFIXES):
        return GuardVerdict(allowed=False, reason="special_tld", host=normalised)

    return GuardVerdict(allowed=True, reason="ok", host=normalised)


def check_url(  # noqa: PLR0911 — one return per refusal reason
    url: str, *, policy: GuardPolicy | None = None
) -> GuardVerdict:
    """Classify a URL without resolving it. Never raises.

    This is the cheap check — safe to run on every link a crawler discovers,
    where thousands of DNS lookups would cost more than the crawl.
    """
    active = policy if policy is not None else guard_policy()
    if not active.enabled:
        _warn_disabled_once()
        return _ALLOWED

    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return GuardVerdict(allowed=False, reason="no_host")

    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return GuardVerdict(allowed=False, reason="scheme", host=parts.netloc)

    # Credentials in the authority are refused outright rather than ignored.
    # ``https://expected.com@169.254.169.254/`` is the canonical way to make a
    # URL read as one host and fetch another: urlsplit gets it right, but the
    # logs, allowlists and humans reading them frequently do not.
    if "@" in parts.netloc:
        return GuardVerdict(allowed=False, reason="userinfo", host=parts.netloc)

    try:
        host = parts.hostname
    except ValueError:
        return GuardVerdict(allowed=False, reason="no_host", host=parts.netloc)
    if not host:
        return GuardVerdict(allowed=False, reason="no_host", host=parts.netloc)

    return check_host(host, policy=active)


def _resolve_all(host: str) -> tuple[str, ...]:
    """Every address ``host`` resolves to, or ``()`` when it does not resolve."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return ()
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if isinstance(address, str) and address not in seen:
            seen.append(address)
    return tuple(seen)


async def resolve_and_check(url: str, *, policy: GuardPolicy | None = None) -> GuardVerdict:
    """Full check: classify the literal URL, then classify what its host resolves to.

    Every returned address is classified and **any** disallowed answer refuses
    the URL. A name that resolves to one public and one private address is a
    rebinding attack, not a misconfiguration, so "some answers were fine" is
    not good enough.

    A resolver failure is **allowed**, with ``reason="unresolvable"``. A
    transient DNS blip must not surface as a 403 that reads like a caller
    error: the fetch will fail on its own with a transport error that callers
    already handle, and the per-hop transport check re-runs this at connect
    time. A guard that invents its own flakiness is a guard that gets
    switched off.
    """
    active = policy if policy is not None else guard_policy()
    verdict = check_url(url, policy=active)
    if not verdict.allowed or not active.enabled or not active.resolve_dns:
        return verdict

    host = verdict.host
    if not host:
        return verdict
    # An IP literal was already classified exactly; resolving it adds nothing.
    try:
        ipaddress.ip_address(host.removeprefix("[").removesuffix("]"))
    except ValueError:
        pass
    else:
        return verdict

    addresses = await asyncio.to_thread(_resolve_all, host)
    if not addresses:
        _logger.debug("urlguard.unresolvable", host=host)
        return GuardVerdict(allowed=True, reason="unresolvable", host=host)

    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover — getaddrinfo returns valid literals
            continue
        reason = _classify_ip(parsed, active)
        if reason != "ok":
            return GuardVerdict(allowed=False, reason=reason, host=host, resolved=addresses)
    return GuardVerdict(allowed=True, reason="ok", host=host, resolved=addresses)


def raise_if_refused(verdict: GuardVerdict) -> None:
    """Log and raise :class:`UrlNotAllowed` when ``verdict`` refuses; else return.

    Shared by every enforcement point so the refusal log line has exactly one
    shape, whether the refusal came from the pre-flight check at a public
    surface or from the per-hop transport check mid-redirect.
    """
    if verdict.allowed:
        return
    _logger.warning(
        "urlguard.refused",
        host=verdict.host,
        reason=verdict.reason,
        resolved=list(verdict.resolved),
    )
    error = UrlNotAllowed(verdict.message())
    # Carried on the exception so a surface can emit a stable code and a fix
    # without re-parsing the message. errors.py declares both attributes.
    error.reason = verdict.reason
    error.remedy = verdict.remedy
    raise error


async def assert_url_allowed(url: str, *, policy: GuardPolicy | None = None) -> None:
    """Raise :class:`UrlNotAllowed` unless ``url`` passes the full check.

    The surface-level entry point: call it where a caller's URL first arrives,
    before any tier runs. Includes DNS resolution, so it catches a hostname
    that points into private space.
    """
    raise_if_refused(await resolve_and_check(url, policy=policy))


def assert_url_allowed_nodns(url: str, *, policy: GuardPolicy | None = None) -> None:
    """Synchronous, no-DNS variant of :func:`assert_url_allowed`.

    For enforcement points that run per network hop, where a resolver query
    per redirect would be both slow and — in a test suite that mocks at the
    transport layer — a surprise trip to the real network. Catches every
    refusal that is visible in the URL itself, which is what a redirect into
    private space almost always is (``Location: http://169.254.169.254/...``).
    """
    raise_if_refused(check_url(url, policy=policy))


def all_reasons() -> Iterable[str]:
    """Every :data:`Reason` member — used by the remedy-coverage test."""
    return get_args(Reason)


__all__ = [
    "REFUSAL_REMEDIES",
    "GuardPolicy",
    "GuardVerdict",
    "Reason",
    "all_reasons",
    "assert_url_allowed",
    "assert_url_allowed_nodns",
    "check_host",
    "check_url",
    "guard_policy",
    "raise_if_refused",
    "resolve_and_check",
    "url_guard_enabled",
]

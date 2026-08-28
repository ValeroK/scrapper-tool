"""Anti-bot impersonation ladder for curl_cffi-backed requests.

When a TLS-fingerprint-sensitive vendor blocks the default ``httpx``
stack, the lib offers ``vendor_client(use_curl_cffi=True)`` which pins
one Chrome profile (``chrome124`` baseline). That worked through 2026-Q1
but Cloudflare started reliably fingerprinting the chrome116-124 family
in early 2026 (see `curl_cffi#500
<https://github.com/lexiforest/curl_cffi/issues/500>`_), which dropped
PartsPilot's Amayama adapter on 2026-03.

This module ships the **fallback ladder** — an ordered tuple of
impersonation profiles tried top-to-bottom until one returns ≠403/503.
Diversification (safari + firefox after the chrome family) is
deliberate: when chrome fingerprints get burned, browser-family rotation
buys breathing room until ``curl_cffi`` ships a fresh chrome target.

Ladder rules (codified in :func:`request_with_ladder`):

1. **One-shot per profile.** Each ladder entry gets a fresh curl_cffi
   session; the inner ``request_with_retry`` handles transport-error +
   5xx retries within that profile's session, but does NOT cycle to the
   next profile on 5xx (the inner retry already covers that). Profile
   rotation triggers only on **403** (the canonical "fingerprint
   identified" signal).
2. **First profile to return ≠403 wins.** Its name is logged as the
   effective profile.
3. **All-403 → raise :class:`BlockedError`.** Distinct from
   :class:`VendorHTTPError` so circuit breakers don't trip
   (the vendor is up, just fingerprinting us — Pattern D / Scrapling
   is the next escalation, not "vendor down").

Bumping the primary
-------------------

Two triggers. Either the leading profile starts showing a >5% 403 rate in the
live-canary workflow, or ``curl_cffi`` ships a fresher Chrome than the one we
lead with — ``test_ladder_leads_with_a_fresh_profile`` fails on the second, and
is a prompt to re-benchmark rather than a bug in itself.

Promote by probing the candidate live first (a 200 from a TLS-reporting endpoint,
and the reported UA version, since the numeric suffixes do not order themselves —
``safari2601`` is Version/26.0.1 while ``safari260`` is 26.0). Then update
:data:`IMPERSONATE_LADDER` and add a CHANGELOG row with the evidence.

Note that the impersonated header set — User-Agent included — comes from
``curl_cffi`` and must not be overridden here. Setting our own UA on top of a
Chrome handshake advertises a bot to anything comparing the two; see the comment
in :func:`_curl_cffi_session`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from curl_cffi.requests import AsyncSession as _CurlCffiAsyncSession

from scrapper_tool._logging import get_logger
from scrapper_tool._urlguard import assert_url_allowed_nodns
from scrapper_tool.errors import BlockedError, VendorHTTPError
from scrapper_tool.http import request_with_retry
from scrapper_tool.proxy import resolve_proxy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

    from scrapper_tool.cookies import CookieIn
    from scrapper_tool.proxy import ProxyPool

# The fallback ladder. Walked top-to-bottom on 403; first ≠403 wins.
#
# Profiles chosen 2026-04-30 from curl_cffi's supported targets:
# - chrome133a — primary; freshest stable Chrome target.
# - chrome124 — kept as a "validated baseline" against PartsPilot's 5
#               shipped adapters in the affiliate-service repo.
# - safari18_0 — diversification when the chrome family is burned
#               (chrome116+ disproportionately fingerprinted; see
#               curl_cffi#500).
# - firefox135 — last resort before Pattern D (Scrapling).
#
# When promoting/demoting a profile, update the CHANGELOG with the
# evidence (canary 403/200 rates, vendor probe results).
#
# Refreshed 2026-07 for curl_cffi 0.15. A stale ladder is itself a detection
# signal — impersonating a Chrome build that no real user runs any more is a
# fingerprint, so the freshest target of each family goes first.
#
# Refreshed again 2026-08-27 for curl_cffi 0.16.2, which added ``chrome150`` and
# ``safari2601``. Each rung below was probed live before promotion (200 from
# tls.peet.ws, with the reported UA version confirming which target is actually
# newer — ``safari2601`` is Version/26.0.1 against ``safari260``'s 26.0, and the
# numeric suffixes are not otherwise self-explaining):
#
# - chrome150 — freshest stable Chrome in curl_cffi 0.16.2. Distinguishable from
#   chrome146 at the TLS layer, not just in the UA: JA4 extension hashes are
#   ``806a8c22fdea`` vs ``d8a2da3f94cd``, so this is a real rotation rather than
#   a cosmetic version bump.
# - chrome146 — the previous primary; recent but settled, diversity inside the
#   Chrome family.
# - safari2601 — freshest Safari; the escape hatch when Chrome is burned.
# - firefox147 — freshest Firefox (unchanged; still the newest target available).
# - chrome133a — the originally-validated primary, kept as the tail rung. Its
#   only cost is one extra request on a path where all four fresher profiles
#   already 403'd (i.e. we were heading to Pattern D regardless), and it keeps a
#   known-good profile reachable for consumers whose adapters were shipped
#   against it.
IMPERSONATE_LADDER: tuple[str, ...] = (
    "chrome150",
    "chrome146",
    "safari2601",
    "firefox147",
    "chrome133a",
)

_logger = get_logger(__name__)

# Status codes that trigger profile rotation. 403 is the canonical
# anti-bot block; 503 is sometimes Cloudflare's challenge interstitial
# (although 5xx normally retries within the same profile via
# request_with_retry — we treat 503 as ambiguous and rotate too).
_ROTATE_STATUS_CODES: frozenset[int] = frozenset({403, 503})


@asynccontextmanager
async def _curl_cffi_session(
    impersonate: str,
    *,
    timeout: float,  # noqa: ASYNC109 — passed straight to curl_cffi, not asyncio.timeout
    proxy: str | None,
    extra_headers: dict[str, str] | None,
    cookies: list[CookieIn] | None = None,
) -> AsyncIterator[_CurlCffiAsyncSession[Any]]:
    """Yield a one-shot curl_cffi session pinned to ``impersonate``.

    Mirrors ``vendor_client(use_curl_cffi=True)`` in defaults but is
    parameterised on the impersonation profile, so the ladder walker
    can rotate without rebuilding the kwargs dict per profile.

    Caller cookies are loaded into the session's **jar**, never rendered into a
    static ``Cookie:`` header. This session sets ``allow_redirects=True``, and a
    static header is re-sent verbatim across a cross-domain redirect — which
    hands the user's session cookie to whatever third-party host the redirect
    points at. Putting them in the jar makes libcurl apply domain and path
    scoping on every hop, which is the whole point.
    """
    # No default User-Agent here, deliberately. ``impersonate`` supplies a full
    # browser header set — including the UA matching the profile's Chrome/Safari
    # build — and setting our own on top replaced it, so every ladder request
    # went out with a Chrome TLS handshake and a User-Agent reading
    # ``scrapper-tool/0.1``. Measured against tls.peet.ws: native chrome150
    # reports ``Chrome/150.0.0.0``; with the old override the same request
    # reported ``scrapper-tool/0.1``. A vendor cross-checking TLS against UA sees
    # a self-identifying bot, which defeats the point of impersonating at all.
    #
    # Accept-Language is dropped for the same reason: the impersonated set
    # already carries one appropriate to the profile.
    #
    # ``httpx``'s polite default UA (``http._DEFAULT_USER_AGENT``) stays where it
    # belongs — on the non-impersonating path, where being honest about who we
    # are is the intent rather than a leak.
    headers: dict[str, str] = dict(extra_headers) if extra_headers else {}

    session: _CurlCffiAsyncSession[Any] = _CurlCffiAsyncSession(
        timeout=timeout,
        headers=headers,
        proxy=proxy,
        allow_redirects=True,
        impersonate=cast("Any", impersonate),
    )
    if cookies:
        _load_cookie_jar(session, cookies)
    try:
        yield session
    finally:
        await session.close()


def _load_cookie_jar(session: Any, cookies: list[CookieIn]) -> None:
    """Set ``cookies`` on a curl_cffi session's jar, domain-scoped.

    Sessions here are one-shot (a fresh one per ladder rung), so this runs once
    per rung rather than once per walk. Failures are logged and swallowed: a jar
    that won't load is a lost login, not a reason to fail a fetch that might
    still succeed against public content.
    """
    jar = getattr(session, "cookies", None)
    if jar is None:  # pragma: no cover — every real curl_cffi session has one
        return
    for cookie in cookies:
        try:
            jar.set(
                cookie.name,
                cookie.value.get_secret_value(),
                domain=cookie.domain,
                path=cookie.path,
            )
        except Exception as exc:
            _logger.debug(
                "ladder.cookie_jar.set_failed",
                name=cookie.name,
                domain=cookie.domain,
                error=str(exc)[:120],
            )


async def request_with_ladder(
    method: str,
    url: str,
    *,
    ladder: tuple[str, ...] = IMPERSONATE_LADDER,
    timeout: float = 10.0,  # noqa: ASYNC109 — passed through to curl_cffi, not asyncio.timeout
    proxy: str | None = None,
    proxy_pool: ProxyPool | None = None,
    extra_headers: dict[str, str] | None = None,
    max_attempts_per_profile: int = 3,
    cookies: list[CookieIn] | None = None,
    **kwargs: Any,
) -> tuple[httpx.Response, str]:
    """Issue ``method`` to ``url``, walking the impersonation ``ladder``.

    For each profile in ``ladder`` (top-to-bottom):
      1. Pick a proxy — the explicit ``proxy`` argument if given, else the next
         healthy entry from ``proxy_pool`` (a fresh IP per rung).
      2. Open a fresh curl_cffi session with that ``impersonate`` value.
      3. Call :func:`request_with_retry` (handles transport + 5xx retries
         within the profile).
      4. If response status ∈ ``{403, 503}``, mark the proxy blocked, close the
         session, and advance to the next profile.
      5. Otherwise: mark the proxy healthy, log the winner, return
         ``(response, profile)``.

    Rotating the **proxy alongside the profile** matters: TLS-fingerprint
    rotation cannot recover a burned IP, so walking all four profiles from one
    flagged egress address is wasted work. With a pool configured, each rung
    varies both dimensions at once.

    If every profile returns 403/503, raises :class:`BlockedError`. The
    caller should escalate to Pattern D (Scrapling) at that point.

    Returns
    -------
    ``(response, winning_profile_name)`` — the response object and the
    name of the impersonation profile that produced it. The caller can
    log the winning profile for trend analysis or pin to it on follow-up
    requests in the same session.

    Notes
    -----
    Each ladder step opens + closes a session. For a small number of
    requests this is negligible; if you're hitting the same URL many
    times and a particular profile is winning consistently, consider
    using :func:`scrapper_tool.http.vendor_client` directly with a
    single profile pin (not exposed yet — the lib's surface in v0.1
    keeps the ladder behind one entrypoint).
    """
    if not ladder:
        msg = "ladder must contain at least one impersonation profile"
        raise ValueError(msg)

    last_status: int | None = None
    last_error: VendorHTTPError | None = None
    for profile in ladder:
        # Fresh IP per rung when a pool is configured; an explicit proxy wins.
        attempt_proxy, managed_pool = resolve_proxy(proxy_pool, proxy)
        try:
            async with _curl_cffi_session(
                profile,
                timeout=timeout,
                proxy=attempt_proxy,
                extra_headers=extra_headers,
                cookies=cookies,
            ) as session:
                resp = await request_with_retry(
                    cast("httpx.AsyncClient", session),
                    method,
                    url,
                    max_attempts=max_attempts_per_profile,
                    **kwargs,
                )
        except VendorHTTPError as exc:
            # Transport exhaustion. When we're going through a pooled proxy this is
            # almost always the *proxy* being dead/unable to CONNECT-tunnel, not the
            # target being down — so penalise that proxy and try the next rung
            # instead of aborting the whole walk. One bad proxy must not kill the
            # ladder. Without a pool, preserve the original behaviour and propagate.
            if managed_pool is None:
                raise
            managed_pool.mark_blocked(attempt_proxy)
            last_error = exc
            _logger.warning(
                "ladder.proxy_transport_failed",
                profile=profile,
                method=method,
                url=url,
                error=str(exc)[:160],
            )
            continue
        last_status = resp.status_code
        if resp.status_code in _ROTATE_STATUS_CODES:
            # The block could be the fingerprint OR the IP — we can't tell which,
            # so penalise the proxy and move on. Cooldown keeps a burned IP out
            # of rotation instead of re-burning it on the next rung.
            if managed_pool is not None:
                managed_pool.mark_blocked(attempt_proxy)
            _logger.warning(
                "ladder.profile_blocked",
                profile=profile,
                method=method,
                url=url,
                status_code=resp.status_code,
                proxied=attempt_proxy is not None,
            )
            continue

        # Post-flight check on where we actually ended up. libcurl follows
        # redirects inside a single ``request()`` call and exposes no per-hop
        # hook, so unlike the httpx path we cannot *prevent* a hop into private
        # space here — only refuse to hand the body back. That is a real
        # limitation, not a complete control: the request was issued, and a
        # state-changing GET has already happened. Closing it properly needs
        # ``allow_redirects=False`` plus a hop loop of our own, which has to be
        # proven not to disturb the impersonation fingerprint first.
        assert_url_allowed_nodns(str(resp.url))

        if managed_pool is not None:
            managed_pool.mark_ok(attempt_proxy)
        _logger.info(
            "ladder.profile_won",
            profile=profile,
            method=method,
            url=url,
            status_code=resp.status_code,
            proxied=attempt_proxy is not None,
        )
        return resp, profile

    # Every rung failed. If they failed at the transport layer through pooled
    # proxies, say so — "all profiles were blocked" would be misleading when the
    # real problem is that the proxy pool is dead.
    if last_status is None and last_error is not None:
        msg = (
            f"All {len(ladder)} ladder rungs failed at the transport layer for "
            f"{method} {url} — every pooled proxy was unusable "
            f"(last error: {last_error}). Check proxy health/credentials; free "
            f"proxies commonly cannot CONNECT-tunnel TLS."
        )
        raise BlockedError(msg) from last_error

    raise BlockedError(
        f"All {len(ladder)} ladder profiles returned 403/503 for "
        f"{method} {url} (last status: {last_status}). "
        f"Escalate to Pattern D (Scrapling) — see docs/patterns/d-hostile.md."
    )


__all__ = [
    "IMPERSONATE_LADDER",
    "request_with_ladder",
]

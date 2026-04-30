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

When ``chrome133a`` starts showing >5% 403 rate in the live-canary
workflow, promote whichever ``curl_cffi`` profile has stabilised —
``chrome142`` and ``chrome146`` are the freshest available as of
2026-04-30. Update :data:`IMPERSONATE_LADDER` and add a CHANGELOG row.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from curl_cffi.requests import AsyncSession as _CurlCffiAsyncSession

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import BlockedError
from scrapper_tool.http import _DEFAULT_USER_AGENT, request_with_retry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx

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
IMPERSONATE_LADDER: tuple[str, ...] = (
    "chrome133a",
    "chrome124",
    "safari18_0",
    "firefox135",
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
) -> AsyncIterator[_CurlCffiAsyncSession[Any]]:
    """Yield a one-shot curl_cffi session pinned to ``impersonate``.

    Mirrors ``vendor_client(use_curl_cffi=True)`` in defaults but is
    parameterised on the impersonation profile, so the ladder walker
    can rotate without rebuilding the kwargs dict per profile.
    """
    headers: dict[str, str] = {
        "User-Agent": _DEFAULT_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)

    session: _CurlCffiAsyncSession[Any] = _CurlCffiAsyncSession(
        timeout=timeout,
        headers=headers,
        proxy=proxy,
        allow_redirects=True,
        impersonate=cast("Any", impersonate),
    )
    try:
        yield session
    finally:
        await session.close()


async def request_with_ladder(
    method: str,
    url: str,
    *,
    ladder: tuple[str, ...] = IMPERSONATE_LADDER,
    timeout: float = 10.0,  # noqa: ASYNC109 — passed through to curl_cffi, not asyncio.timeout
    proxy: str | None = None,
    extra_headers: dict[str, str] | None = None,
    max_attempts_per_profile: int = 3,
    **kwargs: Any,
) -> tuple[httpx.Response, str]:
    """Issue ``method`` to ``url``, walking the impersonation ``ladder``.

    For each profile in ``ladder`` (top-to-bottom):
      1. Open a fresh curl_cffi session with that ``impersonate`` value.
      2. Call :func:`request_with_retry` (handles transport + 5xx retries
         within the profile).
      3. If response status ∈ ``{403, 503}``, close session, advance to
         the next profile.
      4. Otherwise: log the winning profile, return ``(response, profile)``.

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
    for profile in ladder:
        async with _curl_cffi_session(
            profile,
            timeout=timeout,
            proxy=proxy,
            extra_headers=extra_headers,
        ) as session:
            resp = await request_with_retry(
                cast("httpx.AsyncClient", session),
                method,
                url,
                max_attempts=max_attempts_per_profile,
                **kwargs,
            )
        last_status = resp.status_code
        if resp.status_code in _ROTATE_STATUS_CODES:
            _logger.warning(
                "ladder.profile_blocked",
                profile=profile,
                method=method,
                url=url,
                status_code=resp.status_code,
            )
            continue

        _logger.info(
            "ladder.profile_won",
            profile=profile,
            method=method,
            url=url,
            status_code=resp.status_code,
        )
        return resp, profile

    raise BlockedError(
        f"All {len(ladder)} ladder profiles returned 403/503 for "
        f"{method} {url} (last status: {last_status}). "
        f"Escalate to Pattern D (Scrapling) — see docs/patterns/d-hostile.md."
    )


__all__ = [
    "IMPERSONATE_LADDER",
    "request_with_ladder",
]

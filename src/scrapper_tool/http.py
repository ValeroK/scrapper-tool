"""Shared async HTTP client primitives for scrapper-tool consumers.

Thin wrapper around :class:`httpx.AsyncClient` (plus an optional
:class:`curl_cffi.requests.AsyncSession` backend for TLS-sensitive
targets) that bakes in the four cross-cutting concerns every adapter
would otherwise re-implement:

1. **Default headers.** A realistic ``User-Agent`` (some vendors
   400/403 on the default httpx UA); a ``X-Request-ID`` correlation
   header on every call so log entries are traceable end-to-end.
2. **Retry + exponential backoff** on transient failures (5xx / 429 /
   transport error). Three attempts total with ±25 % jitter.
   4xx (except 429) is *not* retried — client-side misconfiguration
   won't fix itself. Transport errors are recognised uniformly across
   both backends.
3. **Proxy support** via the ``proxy`` kwarg on
   :func:`vendor_client`. Both backends accept the same shape.
4. **TLS fingerprinting via curl_cffi.** Setting ``use_curl_cffi=True``
   swaps the httpx backend for :class:`curl_cffi.requests.AsyncSession`
   with Chrome impersonation enabled. M2 promotes the single-profile
   pin into a fallback ladder; M1 ships with the ``chrome124`` baseline
   that affiliate-service shipped against, to keep migration trivial.

Both backends expose a duck-typed ``.request(method, url, headers=,
**kwargs)`` coroutine returning a response object with ``.status_code``
/ ``.text`` / ``.json()`` — :func:`request_with_retry` is backend-
agnostic. The :class:`curl_cffi` session's lifecycle differs from
httpx (async ``close()`` instead of ``aclose()``);
:func:`vendor_client` hides that asymmetry.

Usage::

    async with vendor_client(timeout=10.0) as client:
        resp = await request_with_retry(client, "GET", url)
        resp.raise_for_status()

Consumers wrap call sites in their own circuit breaker — this module
is breaker-agnostic.
"""

from __future__ import annotations

import asyncio
import random
import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from curl_cffi.requests import AsyncSession as _CurlCffiAsyncSession
from curl_cffi.requests.exceptions import (
    RequestException as _CurlCffiRequestException,
)

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import VendorHTTPError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


# Client union — :func:`request_with_retry` accepts either backend.
# Adapters typically annotate the ``client`` parameter as
# :class:`httpx.AsyncClient` for readability; the union is mypy-visible
# at the retry helper so the curl_cffi branch typechecks.
#
# ``Any`` parameterises curl_cffi's :class:`AsyncSession` (it's generic in
# its impersonation-target type but we don't care about the parameter at
# the union level — duck-typed ``.request()`` is what we actually use).
type VendorHTTPClient = httpx.AsyncClient | _CurlCffiAsyncSession[Any]

_logger = get_logger(__name__)

# Realistic-enough desktop UA. Some vendors 403 on the default httpx UA;
# we set one for everyone for consistency. Not a cloaking attempt — just
# polite. Override via ``extra_headers={"User-Agent": ...}`` per call.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; scrapper-tool/0.1; +https://github.com/ValeroK/scrapper-tool)"
)

# Retry policy per call (3 attempts total = initial + 2 retries).
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_JITTER = 0.25
_RETRIABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# Transport-error umbrella spanning both backends. ``httpx.TransportError``
# covers connection/timeout/read failures on the default backend;
# :class:`curl_cffi.requests.exceptions.RequestException` is the curl_cffi
# equivalent. Caught identically — both map to "retry".
_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
    httpx.TransportError,
    _CurlCffiRequestException,
)

# Chrome build we impersonate when ``use_curl_cffi=True``. M1 baseline;
# M2 promotes this to a fallback ladder with safari/firefox diversification.
# Picked for broad JA3/Akamai-H2 coverage at the time of writing.
_CURL_CFFI_IMPERSONATE: Literal["chrome124"] = "chrome124"


def _compute_backoff(attempt: int) -> float:
    """Exponential backoff with ±25% jitter.

    ``attempt`` is 1-indexed (first retry after the initial call).
    Returns seconds to sleep before the next attempt.
    """
    base = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    jitter = base * _BACKOFF_JITTER * (2 * random.random() - 1)  # noqa: S311
    return float(max(0.0, base + jitter))


def _make_request_id() -> str:
    """Short, URL-safe correlation id for the X-Request-ID header."""
    return secrets.token_urlsafe(12)


@asynccontextmanager
async def vendor_client(
    *,
    timeout: float = 10.0,  # noqa: ASYNC109 — passed through to httpx, not asyncio.timeout
    use_curl_cffi: bool = False,
    extra_headers: dict[str, str] | None = None,
    proxy: str | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client with scrapper-tool defaults.

    Parameters
    ----------
    timeout:
        Per-request connect+read+write timeout in seconds. Default 10 s.
    use_curl_cffi:
        When ``True``, back the client with
        :class:`curl_cffi.requests.AsyncSession` for Chrome TLS-
        fingerprint mimicry (``impersonate="chrome124"`` in M1; M2 wires
        the chrome133a → chrome124 → safari → firefox fallback ladder).
        Use only for vendors that reject the default httpx stack
        (hard JA3 / Akamai H2 checks).
    extra_headers:
        Merged on top of the default headers. Per-request
        ``Authorization`` / vendor-specific tokens belong on the
        individual call, not here — use this only for client-wide
        overrides.
    proxy:
        Optional proxy URL (e.g. ``"http://user:pass@host:port"``).
        Both backends accept the same shape. ``None`` (default) disables
        proxying.

    The return type is annotated as :class:`httpx.AsyncClient` for
    call-site readability; when ``use_curl_cffi=True`` the yielded
    object is a :class:`curl_cffi.requests.AsyncSession` which exposes
    the same duck-typed ``.request()`` surface (adapters treat it
    identically). :func:`request_with_retry` accepts both backends.

    The returned client does NOT retry automatically; callers use
    :func:`request_with_retry`. The split lets adapters make multiple
    calls against one client without paying the handshake cost
    repeatedly.
    """

    headers: dict[str, str] = {
        "User-Agent": _DEFAULT_USER_AGENT,
        "Accept-Language": "en-US,en;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)

    client: VendorHTTPClient
    if use_curl_cffi:
        # curl_cffi shares httpx's ``proxy`` / ``timeout`` / ``headers``
        # kwarg shape; the session-level ``impersonate`` propagates to
        # every request issued through this session. ``allow_redirects``
        # is the curl_cffi spelling of httpx's ``follow_redirects``.
        client = _CurlCffiAsyncSession(
            timeout=timeout,
            headers=headers,
            proxy=proxy,
            allow_redirects=True,
            impersonate=_CURL_CFFI_IMPERSONATE,
        )
    else:
        client = httpx.AsyncClient(
            timeout=timeout,
            headers=headers,
            proxy=proxy,
            follow_redirects=True,
        )
    try:
        # Typed as ``httpx.AsyncClient`` for call-site ergonomics — in
        # the curl_cffi branch the yielded object is structurally
        # compatible (shares ``.request()``) but not nominally an
        # httpx client. ``request_with_retry`` handles both.
        yield cast("httpx.AsyncClient", client)
    finally:
        # httpx uses ``aclose()``; curl_cffi's ``AsyncSession`` uses
        # an async ``close()``. Branch on backend (instance check is
        # cheap + avoids relying on attribute-probing order).
        if isinstance(client, _CurlCffiAsyncSession):
            await client.close()
        else:
            await client.aclose()


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
    **kwargs: object,
) -> httpx.Response:
    """Issue ``method`` to ``url``, retrying on transient failure.

    Retries on:

    * Transport error — either :class:`httpx.TransportError` or
      :class:`curl_cffi.requests.exceptions.RequestException`
      (connection refused / timeout / DNS / TLS handshake). Both
      backends are caught uniformly.
    * HTTP responses with ``status_code`` in ``{429, 500, 502, 503, 504}``.

    Does NOT retry on 4xx (except 429). Auth failures bubble
    immediately — no point hammering an expired token.

    Adds a per-call ``X-Request-ID`` header if the caller hasn't already.

    ``client`` is typed as :class:`httpx.AsyncClient` for call-site
    readability; a :class:`curl_cffi.requests.AsyncSession` yielded by
    :func:`vendor_client` is structurally compatible and accepted here.
    """

    # Inject correlation id unless the caller provided one.
    raw_headers = kwargs.pop("headers", None)
    headers: dict[str, str] = (
        dict(raw_headers)  # type: ignore[call-overload]
        if raw_headers
        else {}
    )
    headers.setdefault("X-Request-ID", _make_request_id())

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
        except _TRANSPORT_ERRORS as exc:
            last_exc = exc
            _logger.warning(
                "vendor_http.transport_error",
                method=method,
                url=url,
                attempt=attempt,
                error=str(exc),
            )
            if attempt >= max_attempts:
                break
            await asyncio.sleep(_compute_backoff(attempt))
            continue

        if resp.status_code in _RETRIABLE_STATUS_CODES:
            _logger.warning(
                "vendor_http.retriable_status",
                method=method,
                url=url,
                attempt=attempt,
                status_code=resp.status_code,
            )
            if attempt >= max_attempts:
                # Exhausted — return the last response so the caller
                # can raise_for_status() themselves and surface the code.
                return resp
            await asyncio.sleep(_compute_backoff(attempt))
            continue

        # Non-retriable — success or 4xx-not-429. Return as-is.
        return resp

    # Only reachable via transport-error exhaustion.
    assert last_exc is not None
    raise VendorHTTPError(
        f"{method} {url} failed after {max_attempts} attempts: {last_exc}"
    ) from last_exc


__all__ = [
    "VendorHTTPClient",
    "request_with_retry",
    "vendor_client",
]

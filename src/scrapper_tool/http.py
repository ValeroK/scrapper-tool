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
   pin into a fallback ladder; the single-shot path impersonates the
   ladder's leading profile
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
from urllib.parse import urljoin, urlsplit

import httpx
from curl_cffi.requests import AsyncSession as _CurlCffiAsyncSession
from curl_cffi.requests.exceptions import (
    RequestException as _CurlCffiRequestException,
)

from scrapper_tool._logging import get_logger
from scrapper_tool._urlguard import assert_url_allowed_nodns, strict_redirects_enabled
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

# Chrome build impersonated on the single-shot path (``use_curl_cffi=True``
# without the ladder). Kept in step with the ladder's leading profile: this used
# to sit on chrome124 long after the ladder moved on, which meant every
# non-ladder request advertised a Chrome build no real user runs — a fingerprint
# in itself. See IMPERSONATE_LADDER in ladder.py for the full chain.
_CURL_CFFI_IMPERSONATE: Literal["chrome150"] = "chrome150"


class _GuardedTransport(httpx.AsyncBaseTransport):
    """httpx transport that vets every hop's URL before it goes on the wire.

    A pre-flight check at the call site sees only the URL the caller passed.
    It cannot see where a ``302`` sends us, and every client this module builds
    follows redirects — so ``https://harmless.example/r?to=169.254.169.254``
    defeats a call-site check entirely.

    httpx resolves redirects in the *client*, re-entering the transport once
    per hop, which makes this the one place on the httpx path where every hop
    is visible.

    The check here is the synchronous, no-DNS one, deliberately. Redirects into
    private space name their target in the ``Location`` header, so the URL
    itself carries the evidence; and a resolver query per hop would both slow
    every redirect chain and — because the test suite mocks at the transport
    layer, underneath this class — turn hermetic unit tests into ones that
    quietly hit the network. Full DNS vetting happens once, up front, at the
    public surfaces.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert_url_allowed_nodns(str(request.url))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


#: Redirect statuses we follow by hand under strict mode. Matches what libcurl
#: follows, so turning the flag on does not change *which* chains complete.
_REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

#: Hop ceiling for the manual loop. libcurl's own default is 30; 20 is the same
#: order of magnitude and no real chain approaches it.
_MAX_REDIRECT_HOPS = 20

#: Headers dropped when a redirect crosses to another origin. Sending a bearer
#: token to whatever host a ``Location`` names is the credential-leak half of an
#: open-redirect bug, and libcurl strips these for the same reason.
_CROSS_ORIGIN_STRIP: frozenset[str] = frozenset({"authorization", "proxy-authorization", "cookie"})


def _origin_of(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


async def _request_guarding_each_hop(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    **kwargs: object,
) -> httpx.Response:
    """Follow redirects ourselves so every hop is vetted before it is issued.

    curl_cffi hands redirect following to libcurl, which resolves the whole
    chain inside one ``request()`` call and offers no per-hop hook. That leaves
    only a post-flight check on the final URL — enough to refuse to *return* a
    body from private space, but not to stop the request, so a state-changing
    GET behind a redirect still happens. This closes that by driving the chain
    from Python.

    What libcurl gives free and this has to reproduce:

    * **Method rewriting.** 301/302/303 downgrade a non-GET/HEAD to GET and drop
      the body (303 always does); 307/308 exist precisely to preserve both.
    * **Cross-origin credential stripping.** ``Authorization`` and friends do not
      follow a redirect to another origin.
    * **Cookie continuity.** Not reimplemented, and deliberately so: every hop
      reuses the *same* session, so libcurl's own jar keeps applying its
      domain-scoping rules across hops exactly as before.

    The session's impersonation profile and connection pool are likewise per-
    session, so header order and TLS reuse are unchanged by looping here — that
    is the claim the soak flag exists to verify before this becomes the default.
    """
    current_url = url
    current_method = method.upper()
    hop_headers = dict(headers)
    # Kept separate from the rest because a 301/302/303 to GET has to drop them.
    body_kwargs: dict[str, Any] = {
        key: kwargs.pop(key) for key in ("data", "json", "content", "files") if key in kwargs
    }

    for _hop in range(_MAX_REDIRECT_HOPS):
        # ``allow_redirects`` is curl_cffi's spelling and is what turns libcurl's
        # own following off so this loop can do it instead. Collapsed into one
        # dict because a multi-line ``**splat`` puts mypy's complaint on a
        # different line than the call it belongs to.
        call_kwargs: dict[str, Any] = {
            "headers": hop_headers,
            "allow_redirects": False,
            **body_kwargs,
            **kwargs,
        }
        response = await client.request(current_method, current_url, **call_kwargs)
        if response.status_code not in _REDIRECT_STATUSES:
            return response
        location = response.headers.get("location") or response.headers.get("Location")
        if not location:
            # A 3xx with nowhere to go is the server's problem, not a redirect.
            return response

        target = urljoin(current_url, location)
        # The whole point: refuse before issuing, not after receiving.
        assert_url_allowed_nodns(target)

        if _origin_of(target) != _origin_of(current_url):
            hop_headers = {
                k: v for k, v in hop_headers.items() if k.lower() not in _CROSS_ORIGIN_STRIP
            }
        if response.status_code in {301, 302, 303} and current_method not in {"GET", "HEAD"}:
            current_method = "GET"
            body_kwargs = {}
        current_url = target

    msg = f"exceeded {_MAX_REDIRECT_HOPS} redirects starting at {url}"
    raise VendorHTTPError(msg)


def guard_client(client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Wrap an already-built client's transports so every hop is vetted.

    Deliberately *not* a ``transport=`` factory. Supplying ``transport=`` at
    construction makes httpx skip building its proxy ``mounts`` altogether,
    which silently disables the standard ``HTTP_PROXY`` / ``HTTPS_PROXY`` /
    ``ALL_PROXY`` / ``NO_PROXY`` environment variables that ``trust_env``
    honours by default — traffic that used to go through a proxy would quietly
    start going direct.

    So the client is built normally, httpx resolves its own routing, and then
    every transport it resolved gets wrapped: the default one plus each mount,
    since a proxied mount is exactly the path we most want vetted. A ``None``
    mount means "use the default transport for this pattern" (how ``NO_PROXY``
    is expressed), and is left alone because the default is already wrapped.

    Touches ``_transport`` and ``_mounts``, which are private. They are stable
    across the httpx 0.27+ line this project pins, and the alternative —
    reimplementing httpx's env-proxy parsing — is the more fragile of the two.
    """
    client._transport = _GuardedTransport(client._transport)
    client._mounts = {
        pattern: (None if transport is None else _GuardedTransport(transport))
        for pattern, transport in client._mounts.items()
    }
    return client


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
        fingerprint mimicry. The single-shot path pins one profile
        (:data:`_CURL_CFFI_IMPERSONATE`); ``request_with_ladder`` walks the
        full chrome → safari → firefox chain.
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
        #
        # Only the caller's own headers go in. ``impersonate`` supplies a full
        # browser set including a matching User-Agent, and layering our polite
        # ``scrapper-tool/0.1`` UA on top replaced it — advertising a Chrome TLS
        # handshake alongside a UA that names the scraper. See the note in
        # ``ladder._curl_cffi_session``; same defect, same fix.
        client = _CurlCffiAsyncSession(
            timeout=timeout,
            headers=dict(extra_headers) if extra_headers else {},
            proxy=proxy,
            allow_redirects=True,
            impersonate=_CURL_CFFI_IMPERSONATE,
        )
    else:
        client = guard_client(
            httpx.AsyncClient(
                timeout=timeout,
                headers=headers,
                proxy=proxy,
                follow_redirects=True,
            )
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

    # The httpx path is already vetted per hop by ``guard_client``'s transport,
    # underneath the redirect loop. curl_cffi has no such hook, so under strict
    # mode we drive its chain ourselves; otherwise libcurl follows as before and
    # the ladder's post-flight check is the (weaker) net.
    hop_guarded = strict_redirects_enabled() and isinstance(client, _CurlCffiAsyncSession)

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = (
                await _request_guarding_each_hop(client, method, url, headers=headers, **kwargs)
                if hop_guarded
                else await client.request(method, url, headers=headers, **kwargs)  # type: ignore[arg-type]
            )
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
    "guard_client",
    "request_with_retry",
    "vendor_client",
]

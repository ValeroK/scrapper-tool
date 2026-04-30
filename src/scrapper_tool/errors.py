"""Exception hierarchy for scrapper-tool.

::

    ScrapingError                 (base — all lib-specific exceptions inherit)
    ├── VendorHTTPError           (transport-error or 5xx/429 retry-exhaustion)
    ├── BlockedError              (403 / Cloudflare challenge / Akamai EVA / Distil — anti-bot)
    ├── ParseError                (extractor couldn't find expected fields in the response)
    └── VendorUnavailable         (alias for VendorHTTPError; intended for circuit-breaker
                                   feeders to map "their fault" failures cleanly)

Consumers wrapping the lib in their own circuit breaker typically
trigger on ``VendorHTTPError`` / ``VendorUnavailable`` / ``BlockedError``
but *not* on ``ParseError`` — the latter is "our bug" (parser drift),
not "vendor down". See ``scrapper_tool.adapter`` (M7) for the generic
Adapter Protocol that codifies this distinction.
"""

from __future__ import annotations


class ScrapingError(Exception):
    """Base for all scrapper-tool exceptions."""


class VendorHTTPError(ScrapingError):
    """Raised when all retry attempts exhaust on a retriable failure.

    Non-retriable HTTP 4xx responses (other than 429) are NOT wrapped —
    the caller sees a plain response object via the underlying client.
    The distinction matters for circuit breakers: 4xx is "our bug",
    5xx/429/transport is "their fault".
    """


class VendorUnavailable(VendorHTTPError):
    """Alias of :class:`VendorHTTPError` for circuit-breaker call-sites.

    Subclasses :class:`VendorHTTPError` so existing handlers keep
    working; the dedicated name reads better at the breaker boundary.
    """


class BlockedError(ScrapingError):
    """Raised when an anti-bot platform blocks the request.

    Distinct from :class:`VendorHTTPError` because the remediation
    differs: the breaker should NOT trip (the vendor is up, just
    fingerprinting us). Consumer should escalate to the next ladder
    profile or to Pattern D (Scrapling).
    """


class ParseError(ScrapingError):
    """Raised when the extractor cannot find expected fields.

    Indicates parser drift (vendor changed markup) or a fixture-vs-live
    mismatch. NOT a circuit-breaker signal — re-fetching won't help.
    """


__all__ = [
    "BlockedError",
    "ParseError",
    "ScrapingError",
    "VendorHTTPError",
    "VendorUnavailable",
]

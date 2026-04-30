"""Unit tests for ``scrapper_tool.errors``.

Verifies the inheritance graph is what consumers expect — the
``ScrapingError`` base catches everything; ``VendorUnavailable``
is-a ``VendorHTTPError`` for handler back-compat.
"""

from __future__ import annotations

import pytest

from scrapper_tool.errors import (
    BlockedError,
    ParseError,
    ScrapingError,
    VendorHTTPError,
    VendorUnavailable,
)


class TestExceptionHierarchy:
    def test_all_inherit_from_scraping_error(self) -> None:
        for exc in (VendorHTTPError, VendorUnavailable, BlockedError, ParseError):
            assert issubclass(exc, ScrapingError)

    def test_vendor_unavailable_is_a_vendor_http_error(self) -> None:
        assert issubclass(VendorUnavailable, VendorHTTPError)
        # Both names should catch a VendorUnavailable instance —
        # important for circuit-breaker handlers written before the alias.
        with pytest.raises(VendorHTTPError):
            raise VendorUnavailable("x")

    def test_parse_error_is_not_a_vendor_http_error(self) -> None:
        # ParseError is "our bug" — must not trip a circuit breaker that
        # only handles VendorHTTPError.
        assert not issubclass(ParseError, VendorHTTPError)

    def test_blocked_error_is_not_a_vendor_http_error(self) -> None:
        # BlockedError signals "fingerprint burned" — escalate to the
        # next ladder profile, not "vendor down". Distinct branch.
        assert not issubclass(BlockedError, VendorHTTPError)

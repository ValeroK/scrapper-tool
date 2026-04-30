"""Smoke tests — package importability + top-level re-exports.

Replaced piecemeal by milestone-specific test modules. Until M9
release, the smoke checks here keep the broader CI surface honest.
"""

from __future__ import annotations

import scrapper_tool
import scrapper_tool.patterns


def test_version_is_set() -> None:
    """``scrapper_tool.__version__`` is a non-empty string."""
    assert isinstance(scrapper_tool.__version__, str)
    assert scrapper_tool.__version__


def test_patterns_subpackage_importable() -> None:
    """The ``patterns`` subpackage imports without error.

    Submodules (``a``, ``b``, ``c``, ``d``) are populated by milestones
    M3-M5; only the subpackage namespace is asserted here.
    """
    assert scrapper_tool.patterns.__name__ == "scrapper_tool.patterns"


def test_top_level_reexports() -> None:
    """The most commonly used symbols are reachable from ``scrapper_tool``."""
    assert hasattr(scrapper_tool, "vendor_client")
    assert hasattr(scrapper_tool, "request_with_retry")
    assert hasattr(scrapper_tool, "VendorHTTPError")
    assert hasattr(scrapper_tool, "ScrapingError")
    assert hasattr(scrapper_tool, "BlockedError")
    assert hasattr(scrapper_tool, "ParseError")

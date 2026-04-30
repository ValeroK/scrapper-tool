"""scrapper-tool — reusable web-scraping toolkit.

Pattern A/B/C/D ladder, TLS-impersonation fallback chain, deterministic
fixture-replay testing, and (v0.2.0+) an optional MCP server for LLM agents.

MCP integration is available via ``pip install scrapper-tool[agent]`` —
see ``docs/agent-integration.md`` (M13).

Public API
----------

The top-level ``scrapper_tool`` namespace re-exports the most commonly used
symbols. Submodules expose the rest:

- ``scrapper_tool.http`` — :func:`vendor_client`, :func:`request_with_retry`
- ``scrapper_tool.errors`` — :class:`VendorHTTPError`, :class:`BlockedError`,
  :class:`ParseError`, :class:`VendorUnavailable`, :class:`ScrapingError`
- ``scrapper_tool.ladder`` *(M2)* — ``IMPERSONATE_LADDER`` and the walk logic
- ``scrapper_tool.patterns.{a,b,c,d}`` *(M3-M5)* — extraction helpers per pattern
- ``scrapper_tool.testing`` *(M6)* — fixture-replay test helpers
- ``scrapper_tool.canary`` *(M8)* — CLI for fingerprint-health probes
- ``scrapper_tool.adapter`` *(M7)* — generic ``Adapter[QueryT, ResultT]`` Protocol

Stability
---------

This is alpha software (v0.x). The public API may change between minor
versions until v1.0.0; pin a tilde or caret range when consuming.
"""

from __future__ import annotations

from scrapper_tool.adapter import Adapter
from scrapper_tool.errors import (
    BlockedError,
    ParseError,
    ScrapingError,
    VendorHTTPError,
    VendorUnavailable,
)
from scrapper_tool.http import (
    VendorHTTPClient,
    request_with_retry,
    vendor_client,
)
from scrapper_tool.ladder import (
    IMPERSONATE_LADDER,
    request_with_ladder,
)

__version__ = "0.1.0"

__all__ = [
    "IMPERSONATE_LADDER",
    "Adapter",
    "BlockedError",
    "ParseError",
    "ScrapingError",
    "VendorHTTPClient",
    "VendorHTTPError",
    "VendorUnavailable",
    "__version__",
    "request_with_ladder",
    "request_with_retry",
    "vendor_client",
]

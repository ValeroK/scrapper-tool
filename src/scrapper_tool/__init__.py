"""scrapper-tool — reusable web-scraping toolkit.

Pattern A/B/C/D ladder, TLS-impersonation fallback chain, deterministic
fixture-replay testing, and (v0.2.0+) an optional MCP server for LLM agents.

MCP integration is available via ``pip install scrapper-tool[agent]`` —
see ``docs/agent-integration.md`` (M13).

Public API
----------

The top-level ``scrapper_tool`` namespace re-exports the most commonly used
symbols. Submodules expose the rest:

- ``scrapper_tool.http`` — ``vendor_client``, ``request_with_retry``, ``VendorHTTPError``
- ``scrapper_tool.ladder`` — ``IMPERSONATE_LADDER`` and the walk logic
- ``scrapper_tool.patterns.{a,b,c,d}`` — extraction helpers per pattern
- ``scrapper_tool.testing`` — fixture-replay test helpers
- ``scrapper_tool.canary`` — CLI for fingerprint-health probes
- ``scrapper_tool.adapter`` — generic ``Adapter[QueryT, ResultT]`` Protocol
- ``scrapper_tool.errors`` — exception hierarchy

Stability
---------

This is alpha software (v0.x). The public API may change between minor
versions until v1.0.0; pin a tilde or caret range when consuming.
"""

from __future__ import annotations

__version__ = "0.0.0.dev0"

__all__ = [
    "__version__",
]

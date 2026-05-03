"""scrapper-tool — reusable web-scraping toolkit.

Pattern A/B/C/D ladder, TLS-impersonation fallback chain, deterministic
fixture-replay testing, and (v0.2.0+) an optional MCP server for LLM agents.
v1.0.0+ adds Pattern E — local-LLM-driven scraping for any protected site.

MCP integration is available via ``pip install scrapper-tool[agent]`` —
see ``docs/agent-integration.md`` (M13).

Pattern E (LLM-agent layer) is available via
``pip install scrapper-tool[llm-agent]`` and is documented in
``docs/patterns/e-llm-agent.md``.

Public API
----------

The top-level ``scrapper_tool`` namespace re-exports the most commonly used
symbols. Submodules expose the rest:

- ``scrapper_tool.http`` — :func:`vendor_client`, :func:`request_with_retry`
- ``scrapper_tool.errors`` — :class:`VendorHTTPError`, :class:`BlockedError`,
  :class:`ParseError`, :class:`VendorUnavailable`, :class:`ScrapingError`,
  + Pattern E exceptions (:class:`AgentError` family)
- ``scrapper_tool.ladder`` *(M2)* — ``IMPERSONATE_LADDER`` and the walk logic
- ``scrapper_tool.patterns.{a,b,c,d,e}`` *(M3-M5, M14)* — extraction helpers per pattern
- ``scrapper_tool.testing`` *(M6)* — fixture-replay test helpers
- ``scrapper_tool.canary`` *(M8)* — CLI for fingerprint-health probes
- ``scrapper_tool.adapter`` *(M7)* — generic ``Adapter[QueryT, ResultT]`` Protocol
- ``scrapper_tool.agent`` *(M14)* — Pattern E LLM-agent layer
  (:func:`agent_extract`, :func:`agent_browse`, :func:`agent_session`)

Stability
---------

Stable as of v1.0.0. The public API listed above and the MCP tool surface
follow SemVer: breaking changes will only land in a new major version.
"""

from __future__ import annotations

from scrapper_tool.adapter import Adapter
from scrapper_tool.errors import (
    AgentBlockedError,
    AgentError,
    AgentLLMError,
    AgentSchemaError,
    AgentTimeoutError,
    BlockedError,
    CaptchaSolveError,
    ConfigurationError,
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

__version__ = "1.1.2"


def _agent_lazy(name: str) -> object:
    """Lazy-load ``scrapper_tool.agent`` symbols.

    The agent layer's heavy deps (Camoufox, browser-use, Crawl4AI) are
    optional. We re-export the public symbols at top level for ergonomics
    but only import them on first attribute access — so a plain
    ``import scrapper_tool`` stays light.
    """
    from scrapper_tool import agent  # noqa: PLC0415

    return getattr(agent, name)


def __getattr__(name: str) -> object:  # PEP 562
    if name in {
        "agent_extract",
        "agent_browse",
        "agent_session",
        "AgentConfig",
        "AgentResult",
        "ActionTrace",
        "AgentSession",
    }:
        return _agent_lazy(name)
    msg = f"module 'scrapper_tool' has no attribute {name!r}"
    raise AttributeError(msg)


__all__ = [
    "IMPERSONATE_LADDER",
    "ActionTrace",
    "Adapter",
    "AgentBlockedError",
    "AgentConfig",
    "AgentError",
    "AgentLLMError",
    "AgentResult",
    "AgentSchemaError",
    "AgentSession",
    "AgentTimeoutError",
    "BlockedError",
    "CaptchaSolveError",
    "ConfigurationError",
    "ParseError",
    "ScrapingError",
    "VendorHTTPClient",
    "VendorHTTPError",
    "VendorUnavailable",
    "__version__",
    "agent_browse",
    "agent_extract",
    "agent_session",
    "request_with_ladder",
    "request_with_retry",
    "vendor_client",
]

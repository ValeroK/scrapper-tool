"""MCP server — exposes scrapper-tool helpers as LLM-agent tools.

Available via ``pip install scrapper-tool[agent]`` and the
``scrapper-tool-mcp`` console script. Compatible with any
Model-Context-Protocol consumer:

- **Claude Desktop / Claude Code** — wire via ``.mcp.json``::

    {
      "mcpServers": {
        "scrapper-tool": {
          "command": "scrapper-tool-mcp",
          "args": [],
          "env": {}
        }
      }
    }

- **Anthropic Python SDK + ``mcp-use``** — register the stdio server
  as a toolset and pass to ``client.messages.create(..., tools=...)``.
- **OpenClaw / Hermes Agent / AutoGen / LangChain** — see
  ``docs/agent-integration.md`` for per-framework wiring.

Tools exposed
-------------

- ``fetch_with_ladder(url, *, method, use_curl_cffi)`` — issue an HTTP
  request through the impersonation ladder; returns status, body
  truncated to 64 KB, and the winning profile name.
- ``extract_product(html, *, base_url)`` — parse a schema.org
  Product+Offer block from HTML (Pattern B); returns a normalised
  ``ProductOffer`` dict or ``null``.
- ``extract_microdata_price(html)`` — parse ``<meta itemprop="price">``
  + ``priceCurrency`` schema.org microdata (Pattern C); returns
  ``{price, currency}`` or ``null``.
- ``canary(url, *, profiles)`` — walk the impersonation ladder and
  report which profile won; returns the same JSON shape as the CLI's
  ``--json`` mode.

Security
--------

The MCP server runs in the agent's trust boundary. The
``fetch_with_ladder`` tool can fetch arbitrary URLs — the consuming
agent (Claude, OpenClaw, etc.) is responsible for confirming with the
end user before fetching user-data-bearing URLs. This server does NOT
itself prompt for confirmation; it's the agent's permission model that
gates the call. See ``docs/agent-integration.md § Security``.

Body truncation: we cap response bodies returned to the agent at 64 KB
so a single fetch can't exhaust the agent's context window.
"""

from __future__ import annotations

import sys
from typing import Any

from scrapper_tool import __version__
from scrapper_tool.canary import run_canary
from scrapper_tool.errors import BlockedError, VendorHTTPError
from scrapper_tool.http import request_with_retry, vendor_client
from scrapper_tool.ladder import IMPERSONATE_LADDER, request_with_ladder
from scrapper_tool.patterns.b import extract_product_offer
from scrapper_tool.patterns.c import extract_microdata_price as _extract_microdata_price

_BODY_TRUNCATION_BYTES = 64 * 1024


def _truncate(text: str, limit: int = _BODY_TRUNCATION_BYTES) -> tuple[str, bool]:
    """Cap text to ``limit`` bytes; report whether truncation occurred."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="replace"), True


def _build_server() -> Any:
    """Lazy-construct the FastMCP server.

    Lazy because the ``mcp`` SDK is an optional extra
    (``pip install scrapper-tool[agent]``); importing at module top
    would break ``import scrapper_tool.mcp`` for consumers without the
    extra. The unit tests mock this function to avoid a real SDK
    dependency in the default test profile.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
    except ImportError as exc:
        msg = (
            "scrapper-tool MCP server requires the [agent] extra.\n"
            "Install with: pip install scrapper-tool[agent]"
        )
        raise ImportError(msg) from exc

    server = FastMCP(
        name="scrapper-tool",
        instructions=(
            "Reusable web-scraping toolkit. Use fetch_with_ladder for "
            "TLS-sensitive fetches, extract_product for schema.org "
            "Product+Offer parsing, extract_microdata_price for "
            "<meta itemprop='price'> anchors, canary for fingerprint-"
            "health probes. See https://github.com/ValeroK/scrapper-tool"
        ),
    )

    # ---- Tool: fetch_with_ladder ------------------------------------------

    @server.tool(
        name="fetch_with_ladder",
        description=(
            "Issue an HTTP request through the four-profile TLS-impersonation "
            "ladder (chrome133a -> chrome124 -> safari18_0 -> firefox135) "
            "until a profile returns non-403/503. Returns status, body "
            "(truncated to 64 KB), and which profile won. Use for sites "
            "that fingerprint the default httpx stack."
        ),
    )
    async def fetch_with_ladder(
        url: str,
        method: str = "GET",
        use_curl_cffi: bool = True,
    ) -> dict[str, Any]:
        """Fetch ``url`` through the ladder; return structured result.

        When ``use_curl_cffi=False`` this falls back to the plain httpx
        client (no ladder), useful for sites that don't fingerprint.
        """
        if use_curl_cffi:
            try:
                resp, profile = await request_with_ladder(method, url)
            except BlockedError as exc:
                return {
                    "url": url,
                    "blocked": True,
                    "winning_profile": None,
                    "status": None,
                    "body": None,
                    "truncated": False,
                    "error": str(exc),
                }
            text, truncated = _truncate(resp.text)
            return {
                "url": url,
                "blocked": False,
                "winning_profile": profile,
                "status": int(resp.status_code),
                "body": text,
                "truncated": truncated,
                "error": None,
            }

        # Plain httpx path.
        try:
            async with vendor_client() as client:
                resp = await request_with_retry(client, method, url)
        except VendorHTTPError as exc:
            return {
                "url": url,
                "blocked": False,
                "winning_profile": "httpx",
                "status": None,
                "body": None,
                "truncated": False,
                "error": str(exc),
            }
        text, truncated = _truncate(resp.text)
        return {
            "url": url,
            "blocked": False,
            "winning_profile": "httpx",
            "status": int(resp.status_code),
            "body": text,
            "truncated": truncated,
            "error": None,
        }

    # ---- Tool: extract_product --------------------------------------------

    @server.tool(
        name="extract_product",
        description=(
            "Parse a schema.org Product+Offer block from HTML. Handles "
            "JSON-LD, microdata, and RDFa via extruct. Returns a "
            "ProductOffer dict (name, sku, mpn, gtin, brand, "
            "description, image, price, currency, availability, url) "
            "or null if no Product block is present."
        ),
    )
    async def extract_product(
        html: str,
        base_url: str | None = None,
    ) -> dict[str, Any] | None:
        product = extract_product_offer(html, base_url=base_url)
        if product is None:
            return None
        return product.model_dump(mode="json")

    # ---- Tool: extract_microdata_price ------------------------------------

    @server.tool(
        name="extract_microdata_price",
        description=(
            "Parse <meta itemprop='price'> + <meta itemprop='priceCurrency'> "
            "schema.org microdata anchors from HTML (Pattern C). Returns "
            "{price, currency} or null if either anchor is absent."
        ),
    )
    async def extract_microdata_price(html: str) -> dict[str, Any] | None:
        result = _extract_microdata_price(html)
        if result is None:
            return None
        price, currency = result
        return {"price": str(price), "currency": currency}

    # ---- Tool: canary -----------------------------------------------------

    @server.tool(
        name="canary",
        description=(
            "Walk the impersonation ladder against url and report which "
            "profile won (or all-blocked). Same as the scrapper-tool "
            "canary CLI but accessible to LLM agents. Useful for "
            "diagnosing which TLS fingerprint a vendor is rejecting."
        ),
    )
    async def canary_tool(
        url: str,
        profiles: list[str] | None = None,
    ) -> dict[str, Any]:
        ladder: tuple[str, ...] = tuple(profiles) if profiles else IMPERSONATE_LADDER
        return await run_canary(url, ladder=ladder)

    return server


def main() -> int:
    """Entry point for the ``scrapper-tool-mcp`` console script.

    Starts a stdio MCP server. Exits with code 0 on clean shutdown,
    1 on the ``[agent]`` extra not installed, 2 on argv error.
    """
    if len(sys.argv) > 1 and sys.argv[1] in {"-h", "--help"}:
        print(
            f"scrapper-tool-mcp {__version__}\n"
            "MCP server exposing scrapper-tool helpers as LLM-agent tools.\n"
            "Wire into Claude Desktop / Claude Code / OpenClaw / Hermes "
            "Agent / AutoGen / LangChain via .mcp.json or mcp-use.\n"
            "See docs/agent-integration.md."
        )
        return 0

    try:
        server = _build_server()
    except ImportError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    server.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "main",
]

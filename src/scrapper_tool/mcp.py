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

- ``auto_scrape(url, schema_json, *, instruction, model, browser, timeout_s)`` —
  PRIMARY tool (NEW v1.1.0+). Auto-escalates Pattern A/B/C → E1 → E2 in
  a single call and returns ``pattern_used`` so the agent can see what
  worked. Use this instead of fetch_with_ladder + agent_extract when
  you just want data and don't care which pattern produced it.
- ``fetch_with_ladder(url, *, method, use_curl_cffi, extract_structured)`` —
  Issue an HTTP request through the impersonation ladder; returns
  status, body truncated to 64 KB, and the winning profile name. With
  ``extract_structured=True`` (NEW v1.1.0+) also runs Pattern B + C and
  includes ``product`` and ``microdata_price`` fields — eliminates the
  common two-tool pattern (fetch then extract_product).
- ``extract_product(html, *, base_url)`` — parse a schema.org
  Product+Offer block from HTML (Pattern B); returns a normalised
  ``ProductOffer`` dict or ``null``.
- ``extract_microdata_price(html)`` — parse ``<meta itemprop="price">``
  + ``priceCurrency`` schema.org microdata (Pattern C); returns
  ``{price, currency}`` or ``null``.
- ``canary(url, *, profiles)`` — walk the impersonation ladder and
  report which profile won; returns the same JSON shape as the CLI's
  ``--json`` mode.
- ``agent_extract(url, schema_json, *, instruction, model, timeout_s, headful)`` —
  Pattern E1 (v1.0.0+): render with a stealth browser (Camoufox by
  default) and run a single local-LLM call to extract structured JSON.
  Requires the ``[llm-agent]`` extra and a running local LLM (Ollama).
- ``agent_browse(url, instruction, *, model, max_steps, timeout_s, headful)`` —
  Pattern E2 (v1.0.0+): multi-step LLM-driven browser-use agent for
  interactive tasks (login, multi-step nav, dynamic forms). Same extras
  required as ``agent_extract``.

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

import base64
import sys
from typing import Any

from scrapper_tool import __version__
from scrapper_tool.canary import run_canary
from scrapper_tool.errors import (
    AgentBlockedError,
    AgentError,
    BlockedError,
    VendorHTTPError,
)
from scrapper_tool.http import request_with_retry, vendor_client
from scrapper_tool.ladder import IMPERSONATE_LADDER, request_with_ladder
from scrapper_tool.patterns.b import extract_product_offer
from scrapper_tool.patterns.c import extract_microdata_price as _extract_microdata_price

_BODY_TRUNCATION_BYTES = 64 * 1024
_MAX_AGENT_SCREENSHOTS = 3
_MAX_DOM_SNIPPET_STEPS = 5
_AGENT_NOT_INSTALLED = (
    "scrapper-tool[llm-agent] extra not installed. "
    "Install with: pip install scrapper-tool[llm-agent]"
)


def _truncate(text: str, limit: int = _BODY_TRUNCATION_BYTES) -> tuple[str, bool]:
    """Cap text to ``limit`` bytes; report whether truncation occurred."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="replace"), True


def _structured_product(html: str, base_url: str | None) -> dict[str, Any] | None:
    """Pattern B helper — return ProductOffer dict or None."""
    product = extract_product_offer(html, base_url=base_url)
    return product.model_dump(mode="json") if product is not None else None


def _structured_price(html: str) -> dict[str, Any] | None:
    """Pattern C helper — return ``{price, currency}`` or None."""
    result = _extract_microdata_price(html)
    if result is None:
        return None
    price, currency = result
    return {"price": str(price), "currency": currency}


def _agent_error_payload(
    message: str,
    *,
    blocked: bool = False,
    original: str | None = None,
) -> dict[str, Any]:
    """Uniform error envelope returned to MCP clients on agent failure."""
    payload: dict[str, Any] = {
        "blocked": blocked,
        "data": None,
        "error": message,
        "final_url": None,
        "screenshots": None,
        "actions": [],
        "rendered_markdown": None,
        "duration_s": 0.0,
        "steps_used": 0,
    }
    if original is not None:
        payload["error_detail"] = original
    return payload


def _agent_result_payload(result: Any) -> dict[str, Any]:
    """Serialize an :class:`AgentResult` for MCP transport.

    - Body / markdown truncated to 64 KB.
    - Screenshots base64-encoded, capped at :data:`_MAX_AGENT_SCREENSHOTS`.
    - DOM snippets dropped after :data:`_MAX_DOM_SNIPPET_STEPS` steps.
    """
    markdown_raw = result.rendered_markdown
    markdown, markdown_trunc = _truncate(markdown_raw) if markdown_raw else (None, False)

    actions: list[dict[str, Any]] = []
    for trace in result.actions or []:
        keep_dom = trace.step <= _MAX_DOM_SNIPPET_STEPS
        actions.append(
            {
                "step": trace.step,
                "action": trace.action,
                "target": trace.target,
                "screenshot_idx": trace.screenshot_idx,
                "dom_snippet": trace.dom_snippet if keep_dom else None,
                "latency_ms": trace.latency_ms,
            }
        )

    screenshots: list[str] | None = None
    if result.screenshots:
        screenshots = [
            base64.b64encode(s).decode("ascii") for s in result.screenshots[:_MAX_AGENT_SCREENSHOTS]
        ]

    return {
        "mode": result.mode,
        "data": result.data,
        "final_url": result.final_url,
        "rendered_markdown": markdown,
        "rendered_markdown_truncated": markdown_trunc,
        "screenshots": screenshots,
        "actions": actions,
        "tokens_used": result.tokens_used,
        "blocked": result.blocked,
        "error": result.error,
        "duration_s": result.duration_s,
        "steps_used": result.steps_used,
    }


def _build_server(  # noqa: PLR0915 — single-place tool registration
    *, host: str = "127.0.0.1", port: int = 8000
) -> Any:
    """Lazy-construct the FastMCP server.

    Lazy because the ``mcp`` SDK is an optional extra
    (``pip install scrapper-tool[agent]``); importing at module top
    would break ``import scrapper_tool.mcp`` for consumers without the
    extra. The unit tests mock this function to avoid a real SDK
    dependency in the default test profile.

    Parameters
    ----------
    host
        Network bind address used by the SSE / streamable-HTTP
        transports. Default ``127.0.0.1`` (localhost-only). Set to
        ``0.0.0.0`` to expose the server on a published Docker port or
        to a LAN.
    port
        TCP port for SSE / streamable-HTTP. Default 8000. Ignored for
        the stdio transport.
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
            "Reusable web-scraping toolkit. RECOMMENDED first tool: "
            "auto_scrape (auto-escalates A/B/C -> E1 -> E2 in one call). "
            "Power tools: fetch_with_ladder for TLS-sensitive fetches "
            "(pass extract_structured=True to also parse JSON-LD), "
            "extract_product for schema.org Product+Offer parsing on "
            "raw HTML, extract_microdata_price for <meta itemprop='price'> "
            "anchors, agent_extract / agent_browse for Pattern E direct, "
            "canary for fingerprint-health probes. "
            "See https://github.com/ValeroK/scrapper-tool"
        ),
        host=host,
        port=port,
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
        extract_structured: bool = False,
    ) -> dict[str, Any]:
        """Fetch ``url`` through the ladder; return structured result.

        When ``use_curl_cffi=False`` this falls back to the plain httpx
        client (no ladder), useful for sites that don't fingerprint.

        When ``extract_structured=True`` (v1.1.0+), also runs Pattern B
        (extruct JSON-LD/microdata → ProductOffer) and Pattern C (CSS
        microdata price) on the response body and includes ``product``
        and ``microdata_price`` fields in the result. Eliminates the
        common two-tool pattern (fetch then extract_product).
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
            payload: dict[str, Any] = {
                "url": url,
                "blocked": False,
                "winning_profile": profile,
                "status": int(resp.status_code),
                "body": text,
                "truncated": truncated,
                "error": None,
            }
            if extract_structured and resp.text:
                payload["product"] = _structured_product(resp.text, str(resp.url))
                payload["microdata_price"] = _structured_price(resp.text)
            return payload

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
        payload = {
            "url": url,
            "blocked": False,
            "winning_profile": "httpx",
            "status": int(resp.status_code),
            "body": text,
            "truncated": truncated,
            "error": None,
        }
        if extract_structured and resp.text:
            payload["product"] = _structured_product(resp.text, str(resp.url))
            payload["microdata_price"] = _structured_price(resp.text)
        return payload

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

    # ---- Tool: agent_extract (Pattern E1) ---------------------------------

    @server.tool(
        name="agent_extract",
        description=(
            "Pattern E1 — render a page with a stealth browser (Camoufox by "
            "default) and run a SINGLE local-LLM call to extract structured "
            "JSON matching the supplied schema. Fast path for protected "
            "sites — escalate here only when the TLS-impersonation ladder "
            "AND Pattern D have failed. Requires the [llm-agent] extra and a "
            "running local LLM server (Ollama by default; "
            "set SCRAPPER_TOOL_AGENT_LLM and SCRAPPER_TOOL_AGENT_MODEL to "
            "configure). Returns {data, blocked, error, final_url, "
            "rendered_markdown, actions, duration_s, steps_used}."
        ),
    )
    async def agent_extract(
        url: str,
        schema_json: dict[str, Any] | None = None,
        instruction: str | None = None,
        model: str | None = None,
        browser: str | None = None,
        headful: bool = False,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Run Pattern E1 (Crawl4AI extraction) and return a serializable dict."""
        try:
            from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
            from scrapper_tool.agent import agent_extract as _agent_extract  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — covered by mock
            return _agent_error_payload(_AGENT_NOT_INSTALLED, original=str(exc))

        cfg = AgentConfig.from_env()
        overrides: dict[str, Any] = {
            "headful": headful,
            "timeout_s": timeout_s,
        }
        if model:
            overrides["model"] = model
        if browser:
            overrides["browser"] = browser

        schema = schema_json or "Extract the page's salient data into a JSON object."

        try:
            result = await _agent_extract(
                url,
                schema,
                instruction=instruction,
                config=cfg,
                **overrides,
            )
        except AgentBlockedError as exc:
            return _agent_error_payload(str(exc), blocked=True)
        except AgentError as exc:
            return _agent_error_payload(str(exc))

        return _agent_result_payload(result)

    # ---- Tool: agent_browse (Pattern E2) ----------------------------------

    @server.tool(
        name="agent_browse",
        description=(
            "Pattern E2 — multi-step LLM-driven agent loop for interactive "
            "tasks (login, multi-step navigation, dynamic forms, 'click "
            "load more' pagination). Higher latency than agent_extract — "
            "use only when the page requires interaction. Requires the "
            "[llm-agent] extra and a local LLM. Returns {data, blocked, "
            "error, final_url, screenshots (base64 PNG), actions, "
            "duration_s, steps_used}."
        ),
    )
    async def agent_browse(
        url: str,
        instruction: str,
        schema_json: dict[str, Any] | None = None,
        model: str | None = None,
        browser: str | None = None,
        max_steps: int = 50,
        headful: bool = False,
        timeout_s: float = 180.0,
    ) -> dict[str, Any]:
        """Run Pattern E2 (browser-use agent) and return a serializable dict."""
        try:
            from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
            from scrapper_tool.agent import agent_browse as _agent_browse  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            return _agent_error_payload(_AGENT_NOT_INSTALLED, original=str(exc))

        cfg = AgentConfig.from_env()
        overrides: dict[str, Any] = {
            "headful": headful,
            "timeout_s": timeout_s,
            "max_steps": max_steps,
        }
        if model:
            overrides["model"] = model
        if browser:
            overrides["browser"] = browser

        try:
            result = await _agent_browse(
                url,
                instruction,
                schema=schema_json,
                config=cfg,
                **overrides,
            )
        except AgentBlockedError as exc:
            return _agent_error_payload(str(exc), blocked=True)
        except AgentError as exc:
            return _agent_error_payload(str(exc))

        return _agent_result_payload(result)

    # ---- Tool: auto_scrape (NEW v1.1.0) -----------------------------------

    @server.tool(
        name="auto_scrape",
        description=(
            "PRIMARY scraping tool (v1.1.0+). Auto-escalating ladder: "
            "tries Pattern A/B/C (TLS impersonation + JSON-LD/microdata "
            "extraction) first; if blocked or schema not satisfied, "
            "escalates to Pattern E1 (Crawl4AI + LLM); if still blocked, "
            "escalates to Pattern E2 (browser-use multi-step agent). "
            "Returns pattern_used + pattern_attempts so the agent can "
            "see which pattern produced the data. Use this instead of "
            "fetch_with_ladder + agent_extract when you just want data "
            "and don't care which pattern produced it."
        ),
    )
    async def auto_scrape(
        url: str,
        schema_json: dict[str, Any] | None = None,
        instruction: str | None = None,
        model: str | None = None,
        browser: str | None = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Run the full A/B/C → E1 → E2 escalation ladder."""
        attempts: list[str] = []
        last_error: str | None = None

        # ----- Pattern A/B/C -----
        attempts.append("a_b_c")
        try:
            resp, profile = await request_with_ladder("GET", url)
            text = resp.text or ""
            product = _structured_product(text, str(resp.url))
            price = _structured_price(text)
            success = schema_json is None and (product is not None or price is not None)
            if success:
                truncated_text, truncated = _truncate(text)
                return {
                    "pattern_used": "a_b_c",
                    "pattern_attempts": attempts,
                    "url": str(resp.url),
                    "winning_profile": profile,
                    "product": product,
                    "microdata_price": price,
                    "data": None,
                    "rendered_markdown": None,
                    "body": truncated_text,
                    "truncated": truncated,
                    "blocked": False,
                    "error": None,
                }
        except BlockedError as exc:
            last_error = f"a_b_c: {exc}"

        # ----- Pattern E1 -----
        try:
            from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
            from scrapper_tool.agent import agent_browse as _agent_browse  # noqa: PLC0415
            from scrapper_tool.agent import agent_extract as _agent_extract  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            return {
                "pattern_used": None,
                "pattern_attempts": attempts,
                "url": url,
                "blocked": True,
                "error": _AGENT_NOT_INSTALLED,
                "error_detail": str(exc),
                "data": None,
                "product": None,
                "rendered_markdown": None,
            }

        cfg = AgentConfig.from_env()
        overrides: dict[str, Any] = {"timeout_s": timeout_s}
        if model:
            overrides["model"] = model
        if browser:
            overrides["browser"] = browser

        attempts.append("e1")
        schema_for_e1 = schema_json or {"type": "object", "additionalProperties": True}
        try:
            result = await _agent_extract(
                url, schema_for_e1, instruction=instruction, config=cfg, **overrides
            )
            if not result.blocked:
                payload = _agent_result_payload(result)
                payload["pattern_used"] = "e1"
                payload["pattern_attempts"] = attempts
                payload["product"] = None
                return payload
            last_error = f"e1: {result.error or 'blocked'}"
        except AgentBlockedError as exc:
            last_error = f"e1: {exc}"

        # ----- Pattern E2 -----
        attempts.append("e2")
        e2_instruction = instruction or (
            f"Extract structured data matching: {schema_json}"
            if schema_json
            else "Extract the main content of this page"
        )
        try:
            result = await _agent_browse(
                url, e2_instruction, schema=schema_json, config=cfg, **overrides
            )
            payload = _agent_result_payload(result)
            payload["pattern_used"] = "e2"
            payload["pattern_attempts"] = attempts
            payload["product"] = None
            return payload
        except AgentBlockedError as exc:
            return _agent_error_payload(
                f"All patterns blocked: {', '.join(attempts)}. Last: {last_error or exc}",
                blocked=True,
            ) | {"pattern_used": None, "pattern_attempts": attempts, "product": None}

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


_VALID_TRANSPORTS = {"stdio", "sse", "streamable-http"}

_HELP_TEXT = """\
scrapper-tool-mcp {version}
MCP server exposing scrapper-tool helpers (fetch_with_ladder,
extract_product, extract_microdata_price, canary, agent_extract,
agent_browse) as tools any MCP-aware LLM agent can call.

USAGE:
  scrapper-tool-mcp [--transport stdio|sse|streamable-http]
                    [--host HOST] [--port PORT]

TRANSPORTS:
  stdio (default)    JSON-RPC over stdin/stdout. Used by clients that
                     spawn the server as a subprocess (Claude Desktop,
                     Claude Code's local MCP wiring).
  sse                Server-Sent Events over HTTP. Mount /sse on the
                     given host:port. Older but widely-supported.
  streamable-http    Streamable HTTP (the modern MCP transport). Mount
                     /mcp on the given host:port. Recommended for
                     Cursor, Claude Code remote, and most 2026 clients.

ENVIRONMENT (override flags):
  SCRAPPER_TOOL_MCP_TRANSPORT  Same as --transport.
  SCRAPPER_TOOL_MCP_HOST       Same as --host. Default 127.0.0.1.
                               Use 0.0.0.0 inside Docker.
  SCRAPPER_TOOL_MCP_PORT       Same as --port. Default 8000.

EXAMPLES:
  # Local stdio (Claude Desktop / Claude Code spawn pattern)
  scrapper-tool-mcp

  # HTTP service for Cursor / Claude Code remote / mcp-use:
  scrapper-tool-mcp --transport streamable-http --host 0.0.0.0 --port 8000

See docs/agent-integration.md for client wiring patterns.
"""


def _parse_args(argv: list[str]) -> tuple[str, str, int] | int:
    """Parse argv → (transport, host, port). Returns int exit code on --help.

    Pure parsing; mocked easily in tests.
    """
    import os  # noqa: PLC0415

    transport = os.environ.get("SCRAPPER_TOOL_MCP_TRANSPORT", "stdio")
    host = os.environ.get("SCRAPPER_TOOL_MCP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("SCRAPPER_TOOL_MCP_PORT", "8000"))
    except ValueError:
        sys.stderr.write("SCRAPPER_TOOL_MCP_PORT must be an integer\n")
        return 2

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {"-h", "--help"}:
            print(_HELP_TEXT.format(version=__version__))
            return 0
        if arg == "--transport" and i + 1 < len(argv):
            transport = argv[i + 1]
            i += 2
            continue
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
            continue
        if arg == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                sys.stderr.write(f"--port must be an integer, got {argv[i + 1]!r}\n")
                return 2
            i += 2
            continue
        sys.stderr.write(f"unknown argument: {arg!r}\n")
        sys.stderr.write("Run with --help for usage.\n")
        return 2

    if transport not in _VALID_TRANSPORTS:
        sys.stderr.write(
            f"invalid --transport {transport!r}. Choose from: {sorted(_VALID_TRANSPORTS)}\n"
        )
        return 2

    return transport, host, port


def main() -> int:
    """Entry point for the ``scrapper-tool-mcp`` console script.

    Supports three transports — stdio (default, used by Claude
    Desktop's spawn pattern) and the HTTP-based SSE / streamable-http
    transports (used when the server runs as a long-lived service in
    Docker and external clients connect via URL).

    Exits with code 0 on clean shutdown, 1 on the ``[agent]`` extra not
    installed, 2 on argv error.
    """
    parsed = _parse_args(sys.argv[1:])
    if isinstance(parsed, int):
        return parsed
    transport, host, port = parsed

    try:
        server = _build_server(host=host, port=port)
    except ImportError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    if transport != "stdio":
        sys.stderr.write(f"scrapper-tool-mcp listening on {transport} at {host}:{port}\n")
    server.run(transport=transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "main",
]

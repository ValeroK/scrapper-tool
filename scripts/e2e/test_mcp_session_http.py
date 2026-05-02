"""E2E test §5 (HTTP variant) - external MCP client driving the HTTP server.

Connects to a long-running ``scrapper-tool-mcp`` Docker service over
**streamable HTTP** and exercises every tool. This is the wiring
pattern an external client (Cursor, Claude Code remote, mcp-use, any
2026 MCP-aware app) uses when the server is deployed as a
network-accessible service rather than spawned as a subprocess.

Prereq:

    docker compose --profile http up -d scrapper-tool-mcp-http
    # ... with SCRAPPER_TOOL_MCP_PORT=8765 if 8000 is taken on your host

Run:

    SCRAPPER_TOOL_MCP_URL=http://localhost:8765/mcp \\
        uv run python scripts/e2e/test_mcp_session_http.py
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("SCRAPPER_TOOL_MCP_URL", "http://localhost:8765/mcp")


def _payload(result: Any) -> dict[str, Any] | list[Any] | str:
    if not result.content:
        return "(empty content)"
    first = result.content[0]
    text = getattr(first, "text", str(first))
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


async def main() -> None:  # noqa: PLR0915 - sequential narrative
    print(f"=== MCP-over-HTTP E2E (server: {URL}) ===")
    print()

    async with streamablehttp_client(URL) as (read, write, _meta):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            print(f"[5.0] tools advertised: {tool_names}")
            expected = {
                "fetch_with_ladder",
                "extract_product",
                "extract_microdata_price",
                "canary",
                "agent_extract",
                "agent_browse",
            }
            missing = expected - set(tool_names)
            assert not missing, f"missing tools: {missing}"
            print(f"[5.0] [OK] all {len(expected)} tools present")
            print()

            # 5.A canary
            r = await session.call_tool("canary", {"url": "https://example.com"})
            data = _payload(r)
            assert isinstance(data, dict) and data.get("exit_code") == 0, data
            print(f"[5.A] [OK] canary winning_profile={data['winning_profile']}")

            # 5.B fetch
            r = await session.call_tool(
                "fetch_with_ladder",
                {"url": "https://httpbin.org/anything?msg=hello", "method": "GET"},
            )
            data = _payload(r)
            assert isinstance(data, dict) and data["status"] == 200, data
            assert "hello" in data["body"]
            print(f"[5.B] [OK] fetch_with_ladder status={data['status']}")

            # 5.C extract_product
            r = await session.call_tool(
                "extract_product",
                {
                    "html": (
                        '<script type="application/ld+json">'
                        '{"@context":"https://schema.org","@type":"Product",'
                        '"name":"Pen","offers":{"@type":"Offer",'
                        '"price":"3.99","priceCurrency":"USD"}}</script>'
                    )
                },
            )
            data = _payload(r)
            assert isinstance(data, dict) and data["name"] == "Pen", data
            print(f"[5.C] [OK] extract_product name={data['name']!r} price={data['price']}")

            # 5.D extract_microdata_price
            r = await session.call_tool(
                "extract_microdata_price",
                {
                    "html": (
                        '<span itemtype="http://schema.org/Offer">'
                        '<meta itemprop="price" content="19.99">'
                        '<meta itemprop="priceCurrency" content="USD"></span>'
                    )
                },
            )
            data = _payload(r)
            assert data == {"price": "19.99", "currency": "USD"}, data
            print(f"[5.D] [OK] extract_microdata_price {data}")

            # 5.4 truncation
            r = await session.call_tool(
                "fetch_with_ladder",
                {"url": "https://en.wikipedia.org/wiki/Web_scraping"},
            )
            data = _payload(r)
            assert isinstance(data, dict) and data["truncated"] is True, data
            print(f"[5.4] [OK] truncated={data['truncated']} bytes={len(data['body'])}")

            # 5.E agent_extract via HTTP
            r = await session.call_tool(
                "agent_extract",
                {
                    "url": "https://quotes.toscrape.com/",
                    "schema_json": {
                        "type": "object",
                        "properties": {
                            "quotes": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "author": {"type": "string"},
                                    },
                                    "required": ["text", "author"],
                                },
                            }
                        },
                        "required": ["quotes"],
                    },
                    "instruction": "Extract every quote on the page.",
                    "timeout_s": 240,
                },
            )
            data = _payload(r)
            assert isinstance(data, dict) and data.get("mode") == "extract", data
            quotes = data["data"]["quotes"] if isinstance(data["data"], dict) else data["data"]
            print(
                f"[5.E] [OK] agent_extract quotes={len(quotes)} duration={data['duration_s']:.1f}s"
            )

            # 5.F agent_browse via HTTP
            r = await session.call_tool(
                "agent_browse",
                {
                    "url": "https://quotes.toscrape.com/",
                    "instruction": (
                        "Click the 'Next' button to go to page 2, then return "
                        '{"page": 2, "count": <quote count>}.'
                    ),
                    "max_steps": 8,
                    "timeout_s": 240,
                },
            )
            data = _payload(r)
            assert isinstance(data, dict) and data.get("mode") == "browse", data
            print(f"[5.F] [OK] agent_browse steps={data['steps_used']} data={data['data']}")

            print()
            print("=== MCP-over-HTTP E2E COMPLETE - all 7 checks passed ===")


if __name__ == "__main__":
    asyncio.run(main())

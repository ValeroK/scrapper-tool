"""E2E test §5 - MCP server driven by an agent (this script simulates the agent).

This script is intended to run **inside the scrapper-tool Docker image**.
It spawns ``scrapper-tool-mcp`` as a sibling subprocess in the same
container and talks to it over stdio JSON-RPC using the official
``mcp`` Python client SDK - the same wire format Claude Desktop /
Claude Code use.

How to run (the canonical operator simulation):

    docker compose run --rm \\
      -e SCRAPPER_TOOL_AGENT_LLM=openai_compat \\
      -e SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:6543 \\
      -e SCRAPPER_TOOL_AGENT_MODEL=google/gemma-4-e4b \\
      -e SCRAPPER_TOOL_AGENT_BROWSER=patchright \\
      -v "$(pwd)/scripts:/work/scripts" \\
      --entrypoint python \\
      scrapper-tool /work/scripts/e2e/test_mcp_session.py

For each prompt below, the script issues the tool call an agent would
issue, then verifies the response shape.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# All settings come from env. The Docker image has scrapper-tool-mcp on
# PATH (default entrypoint is ``scrapper-tool-mcp``) so we just spawn it.
SERVER = StdioServerParameters(
    command="scrapper-tool-mcp",
    args=[],
    env={
        **os.environ,
    },
)


def _payload(result: Any) -> dict[str, Any] | list[Any] | str:
    """FastMCP wraps tool results in a CallToolResult with content blocks.

    Each block has ``.type == 'text'`` and a ``.text`` field carrying
    the JSON the tool returned. Unwrap the first text block and parse.
    """
    if not result.content:
        return "(empty content)"
    first = result.content[0]
    text = getattr(first, "text", str(first))
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text


async def main() -> None:  # noqa: PLR0915 - sequential narrative, intentional
    print("=== MCP session E2E (server: scrapper-tool-mcp via stdio) ===")
    print(f"LM Studio URL: {os.environ.get('SCRAPPER_TOOL_AGENT_OLLAMA_URL', 'unset')}")
    print(f"Model: {os.environ.get('SCRAPPER_TOOL_AGENT_MODEL', 'unset')}")
    print(f"Browser: {os.environ.get('SCRAPPER_TOOL_AGENT_BROWSER', 'unset')}")
    print()

    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 5.0 - tool catalogue
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

            # 5.A - canary
            print("[5.A] Prompt: 'Walk the impersonation ladder against example.com'")
            r = await session.call_tool("canary", {"url": "https://example.com"})
            data = _payload(r)
            assert isinstance(data, dict), data
            assert data.get("winning_profile") == "chrome133a", data
            assert data.get("exit_code") == 0, data
            print(f"[5.A] [OK] winning_profile={data['winning_profile']}")
            print()

            # 5.B - fetch_with_ladder
            print("[5.B] Prompt: 'Fetch httpbin.org/anything?msg=hello via the ladder'")
            r = await session.call_tool(
                "fetch_with_ladder",
                {"url": "https://httpbin.org/anything?msg=hello", "method": "GET"},
            )
            data = _payload(r)
            assert isinstance(data, dict), data
            assert data["status"] == 200, data
            assert "hello" in data["body"], data["body"][:200]
            print(
                f"[5.B] [OK] status={data['status']} "
                f"winning_profile={data['winning_profile']} "
                f"truncated={data['truncated']}"
            )
            print()

            # 5.C - extract_product (in-memory HTML, no network)
            print("[5.C] Prompt: 'Parse this Product+Offer JSON-LD HTML'")
            html = (
                '<script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Product",'
                '"name":"Pen","offers":{"@type":"Offer",'
                '"price":"3.99","priceCurrency":"USD"}}</script>'
            )
            r = await session.call_tool("extract_product", {"html": html})
            data = _payload(r)
            assert isinstance(data, dict), data
            assert data.get("name") == "Pen", data
            assert data.get("price") == "3.99", data
            assert data.get("currency") == "USD", data
            print(
                f"[5.C] [OK] name={data['name']!r} "
                f"price={data['price']} currency={data['currency']}"
            )
            print()

            # 5.D - extract_microdata_price
            print("[5.D] Prompt: 'Pull microdata price from this snippet'")
            html = (
                '<span itemtype="http://schema.org/Offer">'
                '<meta itemprop="price" content="19.99">'
                '<meta itemprop="priceCurrency" content="USD"></span>'
            )
            r = await session.call_tool("extract_microdata_price", {"html": html})
            data = _payload(r)
            assert isinstance(data, dict), data
            assert data == {"price": "19.99", "currency": "USD"}, data
            print(f"[5.D] [OK] {data}")
            print()

            # 5.4 - body truncation flag (>64 KB site)
            print("[5.4] Prompt: 'Fetch en.wikipedia.org/wiki/Web_scraping (large body)'")
            r = await session.call_tool(
                "fetch_with_ladder",
                {"url": "https://en.wikipedia.org/wiki/Web_scraping"},
            )
            data = _payload(r)
            assert isinstance(data, dict), data
            assert data["status"] == 200, data
            assert data["truncated"] is True, "expected truncated=true on >64KB"
            assert len(data["body"]) <= 64 * 1024 + 4, len(data["body"])
            print(f"[5.4] [OK] truncated={data['truncated']} body_bytes={len(data['body'])}")
            print()

            # 5.E - agent_extract (Pattern E1)
            print("[5.E] Prompt: 'Use agent_extract on quotes.toscrape.com to get quotes'")
            schema_json = {
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
            }
            r = await session.call_tool(
                "agent_extract",
                {
                    "url": "https://quotes.toscrape.com/",
                    "schema_json": schema_json,
                    "instruction": "Extract every quote on the page.",
                    "timeout_s": 240,
                },
            )
            data = _payload(r)
            assert isinstance(data, dict), data
            if data.get("error"):
                print(f"[5.E] [WARN] error={data['error']}")
            assert not data.get("blocked"), data
            assert data.get("data") is not None, data
            assert data.get("mode") == "extract", data
            quotes = data["data"]["quotes"] if isinstance(data["data"], dict) else data["data"]
            assert isinstance(quotes, list), quotes
            print(
                f"[5.E] [OK] mode={data['mode']} "
                f"quotes={len(quotes)} "
                f"duration={data['duration_s']:.1f}s "
                f"steps_used={data['steps_used']}"
            )
            print()

            # 5.F - agent_browse (Pattern E2)
            print("[5.F] Prompt: 'Use agent_browse on quotes.toscrape.com to paginate'")
            r = await session.call_tool(
                "agent_browse",
                {
                    "url": "https://quotes.toscrape.com/",
                    "instruction": (
                        "Click the 'Next' button at the bottom of the page to "
                        'go to page 2, then return a JSON object {"page": 2, '
                        '"count": <number of quotes shown on page 2>}.'
                    ),
                    "max_steps": 8,
                    "timeout_s": 240,
                },
            )
            data = _payload(r)
            assert isinstance(data, dict), data
            assert data.get("mode") == "browse", data
            assert not data.get("blocked"), data
            print(
                f"[5.F] [OK] mode={data['mode']} "
                f"steps_used={data['steps_used']} "
                f"duration={data['duration_s']:.1f}s "
                f"final_url={data['final_url']!r} "
                f"data={data['data']}"
            )
            print()

            print("=== MCP session E2E COMPLETE - all 7 tool checks passed ===")


if __name__ == "__main__":
    asyncio.run(main())

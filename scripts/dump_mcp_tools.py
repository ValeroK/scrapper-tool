"""Dump the MCP tool surface for the scrapper-tool MCP server.

Run after adding, removing, or re-signing a tool in
:mod:`scrapper_tool.mcp`::

    uv run python scripts/dump_mcp_tools.py

Output:

- ``docs/mcp-tools.json`` — every registered tool with its description
  and JSON-Schema parameters, sorted by name.

The file is committed so the tool surface is reviewable in a diff and
enforceable in CI. It exists because the MCP surface had no equivalent
of the ``openapi-spec-check`` guard that protects ``docs/openapi/``:
the server grew ``map_site`` and ``crawl_site`` while the docs, the
``instructions=`` string the LLM actually reads, and the e2e scripts'
tool lists all stayed at seven or fewer, and nothing failed.

The ``mcp-tool-surface-check`` CI job and
``tests/unit/test_mcp_tool_surface.py`` both fail if this file drifts
from the code — re-run this script to fix the drift.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from scrapper_tool import mcp as mcp_module

SNAPSHOT = Path(__file__).resolve().parent.parent / "docs" / "mcp-tools.json"


def build_snapshot() -> dict[str, Any]:
    """Return the tool surface as a JSON-serialisable dict.

    Imported by the drift test, so the test compares against the same
    construction the generator uses rather than a second implementation
    that could drift on its own.
    """
    server = mcp_module._build_server()
    tools = server._tool_manager._tools
    return {
        "tool_count": len(tools),
        "tools": [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for _, tool in sorted(tools.items())
        ],
    }


def render(snapshot: dict[str, Any]) -> str:
    """Serialise deterministically — stable key order, trailing newline."""
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


def main() -> int:
    """Write docs/mcp-tools.json from the live server registration."""
    snapshot = build_snapshot()
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(render(snapshot), encoding="utf-8")
    print(f"Wrote {SNAPSHOT} ({snapshot['tool_count']} tools)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

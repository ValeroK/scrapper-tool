"""The committed MCP tool surface must match the code.

``docs/openapi/`` has been protected from silent drift by the
``openapi-spec-check`` CI job since it was introduced. The MCP surface
had no equivalent, and it showed: the server grew ``map_site`` and
``crawl_site``, while docs/mcp.md, docs/agent-integration.md, the
``instructions=`` string the LLM actually reads, and both e2e scripts'
expected-tool sets stayed behind. Nothing failed, because nothing
checked.

This is that check. It runs in the normal unit suite so a local
``pytest`` catches the drift too, not just CI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HAS_MCP_SDK = importlib.util.find_spec("mcp") is not None

pytestmark = pytest.mark.skipif(
    not _HAS_MCP_SDK,
    reason="MCP tool-surface check requires the [agent] extra.",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT = _REPO_ROOT / "docs" / "mcp-tools.json"
_SCRIPTS = _REPO_ROOT / "scripts"

_REGEN = "Run: uv run python scripts/dump_mcp_tools.py and commit docs/mcp-tools.json"


def _load_generator() -> object:
    """Import scripts/dump_mcp_tools.py.

    ``scripts/`` is not a package and is outside the import path, so the
    test loads it by location rather than duplicating its logic — the
    point is to compare the snapshot against the generator, not against
    a second implementation free to drift on its own.
    """
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import dump_mcp_tools

    return dump_mcp_tools


def test_snapshot_exists() -> None:
    assert _SNAPSHOT.is_file(), f"docs/mcp-tools.json is missing. {_REGEN}"


def test_snapshot_matches_registered_tools() -> None:
    generator = _load_generator()
    expected = generator.render(generator.build_snapshot())  # type: ignore[attr-defined]
    actual = _SNAPSHOT.read_text(encoding="utf-8")
    assert actual == expected, f"MCP tool surface drifted from docs/mcp-tools.json. {_REGEN}"


def test_documented_tool_count_is_consistent() -> None:
    """Guard the count itself, so an off-by-one is named plainly."""
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert snapshot["tool_count"] == len(snapshot["tools"])


def test_instructions_string_names_every_tool() -> None:
    """The guidance the LLM reads must not omit tools that exist.

    This is the failure that motivated the whole check: ``map_site`` and
    ``crawl_site`` were registered and callable, but absent from
    ``instructions=``, so an agent had no reason to reach for them.
    """
    from scrapper_tool import mcp as mcp_module

    server = mcp_module._build_server()
    instructions = server.instructions or ""
    missing = [name for name in sorted(server._tool_manager._tools) if name not in instructions]
    assert not missing, f"tools missing from the instructions= string: {missing}"

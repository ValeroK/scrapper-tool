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

import ast
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


# ---- The derived copies ---------------------------------------------------
#
# The snapshot check above only proves docs/mcp-tools.json matches the code.
# That is not the same as the tool surface being documented, and the gap is
# not theoretical: adding a tenth tool and regenerating the snapshot (exactly
# what CI tells you to do) leaves every hand-written copy of the list stale
# and every test green. Measured before writing these — only the
# instructions= check fired, and once that was satisfied the whole suite
# passed with four stale lists in the tree.
#
# So the copies are checked too. Names only, not prose: the failure mode is a
# tool that exists and is undocumented, or one that is documented and gone.
# Generating the tables outright would churn hand-written descriptions that
# are worth more than the duplication costs.

_DOCS_LISTING_EVERY_TOOL = (
    "docs/mcp.md",
    "docs/agent-integration.md",
    "docs/E2E_TEST_PLAN.md",
    "skills/scrapper-tool/SKILL.md",
)
"""Files that enumerate the whole tool surface.

Deliberately excludes README.md and docs/quickstart.md, which name a couple
of tools in prose. Policing those would force them to become catalogues.
"""

_E2E_SCRIPTS = (
    "scripts/e2e/test_mcp_session.py",
    "scripts/e2e/test_mcp_session_http.py",
)


def _snapshot_tool_names() -> set[str]:
    snapshot = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    return {tool["name"] for tool in snapshot["tools"]}


def _literal_set(path: Path, name: str) -> set[str]:
    """Read a module-level set literal without importing the module.

    The e2e scripts build an MCP client at import time, so importing them
    here would be a side effect. Parsing is also what makes this work when
    the SDK is absent.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    msg = f"{path} has no module-level {name} assignment"
    raise AssertionError(msg)


@pytest.mark.parametrize("doc", _DOCS_LISTING_EVERY_TOOL)
def test_docs_name_every_registered_tool(doc: str) -> None:
    """Every registered tool is named in the docs that list the surface.

    This is the check that was missing when the server grew `map_site` and
    `crawl_site` and the tables stayed at seven.
    """
    text = (_REPO_ROOT / doc).read_text(encoding="utf-8")
    missing = sorted(name for name in _snapshot_tool_names() if name not in text)
    assert not missing, f"{doc} does not mention: {missing}. {_REGEN}"


@pytest.mark.parametrize("script", _E2E_SCRIPTS)
def test_e2e_expected_tools_match_the_snapshot(script: str) -> None:
    """The e2e scripts' expected-tool sets must be exact, and current.

    They are the only things in the repo that speak the wire protocol, and
    they are neither collected by pytest nor run in CI — so nothing else
    would report it when their hardcoded sets rot.
    """
    declared = _literal_set(_REPO_ROOT / script, "EXPECTED_TOOLS")
    expected = _snapshot_tool_names()
    assert declared == expected, (
        f"{script} EXPECTED_TOOLS is stale: "
        f"missing={sorted(expected - declared)} extra={sorted(declared - expected)}"
    )

"""Pytest top-level conftest for scrapper-tool.

Test layout
-----------
- ``tests/unit/`` — fast, hermetic, no network. Run on every CI build.
- ``tests/integration/`` — broader integration tests; can be slower but
  still hermetic (no live internet).
- ``tests/integration/test_live_probes.py`` — opt-in live-internet probes,
  marked ``@pytest.mark.live``. Skipped by default; CI runs them in a
  separate scheduled workflow (``.github/workflows/live-canary.yml``).

Run modes
---------
- ``uv run pytest`` — core unit + integration (live skipped via the
  default ``-m "not live"`` marker in ``pyproject.toml``).
- ``uv run pytest -m live`` — only live probes.
- ``uv run pytest -m "live or not live"`` — everything.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest

from scrapper_tool.recipe.policy import set_policy_store
from scrapper_tool.recipe.store import set_store

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _disable_render_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the stealth-render cascade tier off unless a test asks for it.

    The tier is default-ON in production (that's the point of B2), but it
    launches a real browser. Left enabled here, any test whose cascade escalates
    past Pattern D would spawn Camoufox — slow, and non-hermetic in a way that
    depends on ``sys.modules`` import order, which made the D-to-E1 tests pass
    alone and fail in a full run.

    Tests that exercise the render tier set ``SCRAPPER_TOOL_RENDER_TIER=1`` and
    fake ``render_html``; ``test_render_tier`` covers the default-on behaviour
    directly against :func:`_render_tier_enabled`.
    """
    monkeypatch.setenv("SCRAPPER_TOOL_RENDER_TIER", "0")


@pytest.fixture(autouse=True)
def _disable_hostile_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Pattern D out of the cascade unless a test asks for it.

    Same reasoning as :func:`_disable_render_tier` above, for the tier one rung
    below it. D launches Scrapling's Playwright browser, which resolves and
    fetches for real, so any test whose cascade escalates past A/B/C was
    hermetic only for as long as nobody installed the ``[hostile]`` extra. That
    made the suite's behaviour a property of the environment: the same test
    passed on a bare ``--extra dev`` sync and reached the internet under
    ``[full]``, which is the CI matrix row.

    Pinning it off here rather than in each test is what the call sites already
    voted for — 37 unit tests set this flag to False by hand and 7 set it to
    True. A default that two-thirds of its call sites have to correct is the
    wrong default. The 7 opt-ins are unaffected: they enable D explicitly
    (``_install_fake_hostile_client`` flips the same flag to True) and the
    fixture runs before the test body, so the test's own patch wins.

    Both surfaces are pinned because both have their own seam onto the same
    probe — REST reads ``http_server._hostile_available``, MCP reads
    ``mcp._hostile_available_for_mcp``. ``_extras.hostile_available`` itself is
    deliberately left alone: it is the thing ``test_extras`` asserts against.
    """
    from scrapper_tool import http_server, mcp

    monkeypatch.setattr(http_server, "_hostile_available", lambda: False)
    monkeypatch.setattr(mcp, "_hostile_available_for_mcp", lambda: False)


@pytest.fixture(autouse=True)
def _isolate_recipe_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give every test its own empty recipe cache.

    The cache is default-ON and persists to a shared temp dir, so without this a
    recipe learned by one test (or by a previous run, or by real local use)
    would silently serve the replay tier in another and change its cascade. Each
    test gets a fresh directory, so replay is a guaranteed miss unless the test
    populates it deliberately.
    """
    monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_DIR", str(tmp_path / "recipes"))
    set_store(None)  # drop the process-wide handle so the new dir takes effect
    # The per-domain tier policy (F2) lives under the same cache dir and is also
    # default-ON; reset its process-wide handle for the same reason, else a
    # policy learned in one test would skip tiers in another.
    set_policy_store(None)


# --- Tier probe (opt-in tripwire, CI job "hermeticity") ---------------------
#
# The fixtures above close the two leaks we know about by turning tiers off.
# This closes the general case: it fails the run when a `tests/unit/` test
# *reaches* a tier that would open a socket, whichever tier that is and however
# it got there.
#
# It exists because the leak it was written for was invisible to every obvious
# guard. The DNS lookups came from a Chromium subprocess, so blocking sockets in
# the pytest process would not have seen them, and the test passed or failed
# depending only on which extras happened to be installed — green on a bare
# `--extra dev` sync, reaching the internet on the `[full]` CI row.
#
# Off by default: it replaces the tier entry points, which is the wrong
# behaviour for the suites that legitimately drive them. Enabled by env var so
# one CI job pays for it. Run locally with:
#
#     SCRAPPER_TOOL_TIER_PROBE=1 uv run pytest tests/unit -q

_TIER_PROBE = os.getenv("SCRAPPER_TOOL_TIER_PROBE") == "1"

#: Suites that call a tier entry point on purpose and patch *below* it
#: (camoufox, StealthyFetcher). They are the subject under test, not callers of
#: it, so the probe leaves them alone rather than making them declare a fake of
#: the thing they exist to exercise.
_TIER_PROBE_EXEMPT = (
    "tests/unit/test_patterns_render.py",
    "tests/unit/test_cookies_tiers.py::TestDTier",
    "tests/unit/test_cookies_threading.py::TestRenderTier",
    # Asserts that SCRAPPER_TOOL_URL_GUARD_STRICT makes each tier entry point
    # *refuse*, so it has to call them — and the refusal happens before any
    # browser launches or socket opens, which is the property under test. The
    # probe cannot tell "called it and it refused" from "called it and it dialled
    # out", so it needs telling.
    "tests/unit/test_urlguard.py::TestStrictModeAtTheTierEntryPoints",
)

_TIER_PROBE_HITS: list[str] = []


@pytest.fixture(autouse=True)
def _tier_probe(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Record any test that reaches a browser/LLM tier without faking it first.

    Each entry point is replaced by a recorder that raises, so a test which
    installed its own fake never gets here (its patch lands after this fixture
    and wins) and a test which did not is reported by node id.
    """
    nodeid = request.node.nodeid.replace("\\", "/")
    if not _TIER_PROBE or any(exempt in nodeid for exempt in _TIER_PROBE_EXEMPT):
        return

    from contextlib import asynccontextmanager

    import scrapper_tool.agent as agent_pkg
    import scrapper_tool.patterns.render as render_mod
    from scrapper_tool.patterns import d as d_mod

    def reached(tier: str) -> RuntimeError:
        entry = f"{tier:<6} {nodeid}"
        if entry not in _TIER_PROBE_HITS:
            _TIER_PROBE_HITS.append(entry)
        return RuntimeError(f"tier probe: {tier} would have opened a socket")

    @asynccontextmanager
    async def probe_d(**_kwargs: Any) -> Any:
        raise reached("D")
        yield  # pragma: no cover — unreachable, keeps this a generator

    async def probe_render(_url: str, **_kwargs: Any) -> Any:
        raise reached("render")

    async def probe_e1(_url: str, *_a: Any, **_k: Any) -> Any:
        raise reached("E1")

    async def probe_e2(_url: str, *_a: Any, **_k: Any) -> Any:
        raise reached("E2")

    monkeypatch.setattr(d_mod, "hostile_client", probe_d, raising=False)
    monkeypatch.setattr(render_mod, "render_html", probe_render, raising=False)
    monkeypatch.setattr(agent_pkg, "agent_extract", probe_e1, raising=False)
    monkeypatch.setattr(agent_pkg, "agent_browse", probe_e2, raising=False)


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Name the offenders. A bare non-zero exit here is unactionable."""
    if not _TIER_PROBE_HITS:
        return
    terminalreporter.section("tier probe: tests reached a network-capable tier", red=True)
    for hit in _TIER_PROBE_HITS:
        terminalreporter.line(hit)
    terminalreporter.line("")
    terminalreporter.line(
        "Install a fake for the tier (see _install_fake_hostile_client in "
        "tests/unit/test_http_server.py), or pin the tier off for that test."
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the run on a hit even when every assertion passed."""
    if _TIER_PROBE_HITS and exitstatus == 0:
        session.exitstatus = 1

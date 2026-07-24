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

from typing import TYPE_CHECKING

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

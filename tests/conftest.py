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

import pytest


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

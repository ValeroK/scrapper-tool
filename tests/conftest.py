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

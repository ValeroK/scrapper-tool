"""Smoke test — verifies the package is importable and exposes its version.

Replaced by real tests in M1. Until then, this is the only test that
keeps the CI matrix green.
"""

from __future__ import annotations


def test_version_is_set() -> None:
    """``scrapper_tool.__version__`` is a non-empty string."""
    import scrapper_tool

    assert isinstance(scrapper_tool.__version__, str)
    assert scrapper_tool.__version__


def test_patterns_subpackage_importable() -> None:
    """The ``patterns`` subpackage imports without error.

    Submodules (``a``, ``b``, ``c``, ``d``) are populated by milestones
    M3-M5; only the subpackage namespace is asserted here.
    """
    import scrapper_tool.patterns  # noqa: F401

"""Smoke tests — package importability + top-level re-exports.

Replaced piecemeal by milestone-specific test modules. Until M9
release, the smoke checks here keep the broader CI surface honest.
"""

from __future__ import annotations

import scrapper_tool
import scrapper_tool.patterns


def test_version_is_set() -> None:
    """``scrapper_tool.__version__`` is a non-empty string."""
    assert isinstance(scrapper_tool.__version__, str)
    assert scrapper_tool.__version__


def test_version_matches_pyproject() -> None:
    """The version is declared twice and nothing kept the two in step.

    ``pyproject.toml`` feeds the sdist/wheel and PyPI; ``__version__`` feeds
    ``/version``, ``/ready``, the OpenAPI spec's ``info.version`` and doctor's
    banner. A bump that touches one and not the other publishes a package whose
    own API reports a different number, and every existing assertion compares
    a surface to ``__version__`` rather than to the packaging metadata, so all
    of them would still pass.
    """
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert declared == scrapper_tool.__version__, (
        f"pyproject.toml says {declared!r} but __version__ is "
        f"{scrapper_tool.__version__!r} — bump both"
    )


def test_patterns_subpackage_importable() -> None:
    """The ``patterns`` subpackage imports without error.

    Submodules (``a``, ``b``, ``c``, ``d``) are populated by milestones
    M3-M5; only the subpackage namespace is asserted here.
    """
    assert scrapper_tool.patterns.__name__ == "scrapper_tool.patterns"


def test_top_level_reexports() -> None:
    """The most commonly used symbols are reachable from ``scrapper_tool``."""
    assert hasattr(scrapper_tool, "vendor_client")
    assert hasattr(scrapper_tool, "request_with_retry")
    assert hasattr(scrapper_tool, "VendorHTTPError")
    assert hasattr(scrapper_tool, "ScrapingError")
    assert hasattr(scrapper_tool, "BlockedError")
    assert hasattr(scrapper_tool, "ParseError")

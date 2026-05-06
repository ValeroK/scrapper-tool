"""Extractor registry for Pattern D's multi-extractor pipeline (v1.4.0+).

Each extractor consumes raw HTML and returns an :class:`ExtractorResult`.
Pattern D's ``_do_d_step`` (and the cascade DSL's ``css_extract`` /
``json_ld_extract`` / ``microdata_extract`` / ``open_graph_extract``
handlers) dispatch through this registry so the same extractor lives
in one place regardless of how the cascade is composed.

The default extractor order for a Pattern D auto-cascade run:

1. ``json_ld_product`` — schema.org/Product LD+JSON blocks (Amayama,
   most server-rendered ecommerce).
2. ``microdata_price`` — ``<meta itemprop="price">`` microdata.
3. ``open_graph`` — ``<meta property="og:product:price:amount">`` and
   siblings (some Shopify / Magento storefronts).

When the caller supplies a CSS-shaped ``schema_json``
(``{baseSelector, fields}``), the ``css`` extractor is also run and
its output is the canonical ``data`` payload (Tasca / Megazip /
RevolutionParts dealers).

The pipeline stops on the first extractor that returns a structured
signal, unless the caller's ``cascade`` step explicitly disables
short-circuiting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ExtractorResult:
    """Outcome of one extractor pass over a single HTML document.

    ``data`` is the structured payload (any JSON-serialisable shape).
    ``signals`` is the lightweight evidence summary the cascade's
    success classifier consumes — boolean fields keep the classifier
    cheap when nothing matched.
    """

    data: dict[str, Any] | list[Any] | None = None
    has_signal: bool = False
    extractor_name: str = ""

    @classmethod
    def empty(cls, name: str) -> ExtractorResult:
        return cls(data=None, has_signal=False, extractor_name=name)


class Extractor(Protocol):
    """Protocol implemented by every extractor.

    Each extractor is a pure function: ``(html, options) -> ExtractorResult``.
    No I/O, no browser, no LLM. The fetcher (Pattern A/B/C or D) handles
    the HTTP / browser layer; extractors only parse.
    """

    name: str

    def extract(
        self,
        html: str,
        *,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> ExtractorResult: ...


# Lazy registry — extractors register themselves on first import.
_REGISTRY: dict[str, Extractor] = {}


def register(extractor: Extractor) -> Extractor:
    """Register an extractor under its ``name``. Idempotent."""
    _REGISTRY[extractor.name] = extractor
    return extractor


def get(name: str) -> Extractor:
    """Look up an extractor by name. Triggers lazy module import on first access."""
    # Bootstrap unconditionally — Python's module cache makes the
    # imports a near-no-op on the second+ call. Earlier versions
    # short-circuited on ``if not _REGISTRY`` which was buggy: when
    # one extractor module had already been imported (e.g. for
    # ``looks_like_css_schema``), the registry was non-empty and the
    # bootstrap got skipped, leaving the other built-ins missing.
    _bootstrap_builtins()
    if name not in _REGISTRY:
        msg = f"Unknown extractor {name!r}; known: {sorted(_REGISTRY)}"
        raise KeyError(msg)
    return _REGISTRY[name]


def all_names() -> list[str]:
    """Return all registered extractor names (after bootstrap)."""
    _bootstrap_builtins()
    return sorted(_REGISTRY)


def _bootstrap_builtins() -> None:
    """Trigger registration of the built-in extractors via import."""
    # Side-effect imports — modules register themselves on first import.
    from scrapper_tool._extractors import (  # noqa: F401, PLC0415
        css,
        json_ld,
        microdata,
        open_graph,
    )


__all__ = [
    "Extractor",
    "ExtractorResult",
    "all_names",
    "get",
    "register",
]

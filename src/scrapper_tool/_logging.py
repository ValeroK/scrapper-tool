"""Optional structlog binding shim.

The lib should be usable in projects that don't depend on ``structlog``.
This shim returns a real ``structlog`` logger if available, or a thin
adapter around stdlib ``logging`` that accepts the same keyword-arg
shape (``logger.warning("event", key=value)``) so call sites don't
have to branch.

Usage::

    from scrapper_tool._logging import get_logger

    _logger = get_logger(__name__)
    _logger.warning("vendor_http.transport_error", url=url, attempt=attempt)
"""

from __future__ import annotations

import logging
from typing import Any, Protocol


class _Logger(Protocol):
    """Minimal structured-logger surface used by the lib."""

    def debug(self, event: str, **kwargs: Any) -> None: ...
    def info(self, event: str, **kwargs: Any) -> None: ...
    def warning(self, event: str, **kwargs: Any) -> None: ...
    def error(self, event: str, **kwargs: Any) -> None: ...


class _StdlibStructAdapter:
    """Wraps a stdlib ``logging.Logger`` to accept ``key=value`` kwargs.

    Renders kwargs as ``key=value key2=value2`` after the event name —
    not as JSON; the goal is "readable in a plain console" rather than
    "structured log shipping". Consumers that want JSON should install
    ``structlog`` themselves; this lib will pick it up automatically.
    """

    def __init__(self, name: str) -> None:
        self._inner = logging.getLogger(name)

    @staticmethod
    def _render(event: str, kwargs: dict[str, Any]) -> str:
        if not kwargs:
            return event
        rendered = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return f"{event} {rendered}"

    def debug(self, event: str, **kwargs: Any) -> None:
        self._inner.debug(self._render(event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self._inner.info(self._render(event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self._inner.warning(self._render(event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self._inner.error(self._render(event, kwargs))


def get_logger(name: str) -> _Logger:
    """Return a structured logger for ``name``.

    Prefers ``structlog`` when installed; falls back to a stdlib
    adapter otherwise. Both implementations satisfy the ``_Logger``
    protocol so call sites are uniform.
    """
    try:
        import structlog  # noqa: PLC0415
    except ImportError:
        return _StdlibStructAdapter(name)
    return structlog.get_logger(name)  # type: ignore[no-any-return,unused-ignore]


__all__ = ["get_logger"]

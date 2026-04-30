"""Test helpers for adapters built on top of ``scrapper_tool``.

Two helpers most consumers want, both lifted out of the inline test
shims used in M2 / M3 / M4 / M5:

- :class:`FakeCurlSession` — drop-in replacement for
  :class:`curl_cffi.requests.AsyncSession`. ``respx`` does not intercept
  curl_cffi traffic (different transport), so adapters that use
  ``vendor_client(use_curl_cffi=True)`` need this fake to test the
  parser deterministically. Configure ``status_for_profile`` per
  impersonation profile, or pass per-URL overrides via
  ``response_factory``.
- :func:`replay_fixture` — load a fixture file from disk and feed it to
  a parser. Used together with golden-snapshot assertions to keep
  parsers honest as the lib evolves.

Usage with the impersonation ladder::

    from scrapper_tool.testing import FakeCurlSession
    from scrapper_tool import ladder as ladder_module

    def test_my_adapter(monkeypatch):
        FakeCurlSession.reset()
        FakeCurlSession.STATUS_FOR_PROFILE = {"chrome133a": 200}
        monkeypatch.setattr(
            ladder_module, "_CurlCffiAsyncSession", FakeCurlSession
        )
        ...

Usage with fixture replay::

    from pathlib import Path
    from scrapper_tool.testing import replay_fixture
    from my_adapter.parser import parse_html

    result = replay_fixture(
        Path("tests/fixtures/vendor_x/happy_path.html"),
        parse_html,
    )
    assert result.price == Decimal("19.99")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Self

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel


# --- Fake response --------------------------------------------------------


@dataclass
class FakeResponse:
    """Minimal duck-typed response — exposes the surface
    :func:`scrapper_tool.http.request_with_retry` actually inspects.

    Real ``httpx.Response`` objects also have ``.headers``, ``.cookies``,
    ``.url``, etc. — extend this fake when you need them.
    """

    status_code: int
    text: str = ""
    url: str = ""

    def json(self) -> Any:
        return json.loads(self.text) if self.text else None


# --- Fake curl_cffi session ----------------------------------------------


@dataclass
class FakeCurlSession:
    """Drop-in mock for :class:`curl_cffi.requests.AsyncSession`.

    Use via ``monkeypatch.setattr(scrapper_tool.ladder, "_CurlCffiAsyncSession", FakeCurlSession)``.

    Tests configure response status by setting class-level attributes
    *before* invoking the ladder:

    - :attr:`STATUS_FOR_PROFILE` — ``{profile_name: status_code}``. The
      session looks up its own ``self.impersonate`` to decide what
      status to return.
    - :attr:`RESPONSE_TEXT_FOR_PROFILE` — ``{profile_name: response_text}``.
      Optional; defaults to empty string.

    Tracks every constructed instance in :attr:`INSTANCES` so tests can
    assert on impersonation-profile rotation order.
    """

    timeout: float = 10.0
    headers: dict[str, str] = field(default_factory=dict)
    proxy: str | None = None
    allow_redirects: bool = True
    impersonate: str = ""

    # Class-level state (ClassVar so the dataclass decorator skips it).
    STATUS_FOR_PROFILE: ClassVar[dict[str, int]] = {}
    RESPONSE_TEXT_FOR_PROFILE: ClassVar[dict[str, str]] = {}
    INSTANCES: ClassVar[list[FakeCurlSession]] = []

    def __post_init__(self) -> None:
        self._calls: list[tuple[str, str]] = []
        type(self).INSTANCES.append(self)

    async def request(
        self,
        method: str,
        url: str,
        **_kwargs: Any,
    ) -> FakeResponse:
        self._calls.append((method, url))
        status = type(self).STATUS_FOR_PROFILE.get(self.impersonate, 200)
        text = type(self).RESPONSE_TEXT_FOR_PROFILE.get(self.impersonate, "")
        return FakeResponse(status_code=status, text=text, url=url)

    async def close(self) -> None:
        return None

    @classmethod
    def reset(cls) -> Self:
        """Clear all class-level state. Call at the start of every test
        that uses :class:`FakeCurlSession` to avoid cross-test bleed."""
        cls.STATUS_FOR_PROFILE = {}
        cls.RESPONSE_TEXT_FOR_PROFILE = {}
        cls.INSTANCES = []
        return cls  # type: ignore[return-value]

    @property
    def calls(self) -> list[tuple[str, str]]:
        """List of ``(method, url)`` requests issued through this session."""
        return list(self._calls)


# --- Fixture replay --------------------------------------------------------


def replay_fixture(path: Path, parser: Callable[[str], Any]) -> Any:
    """Load a fixture file from disk and feed its contents to ``parser``.

    Equivalent to::

        result = parser(path.read_text(encoding="utf-8"))

    Wrapped here so consumers can later swap implementations
    (e.g. add automatic encoding detection, JSON parsing for ``.json``
    fixtures, gzip support) without touching every test.
    """
    return parser(path.read_text(encoding="utf-8"))


def assert_pydantic_snapshot(
    obj: BaseModel,
    snapshot_path: Path,
    *,
    write_if_missing: bool = True,
) -> None:
    """Golden-snapshot diff for Pydantic models.

    On first invocation (when ``snapshot_path`` doesn't exist), writes
    the model's JSON serialisation to disk and returns. Re-running the
    test after that asserts byte-for-byte equality.

    Set ``write_if_missing=False`` in CI environments where you want
    missing snapshots to fail fast instead of silently passing on the
    first run.

    Parameters
    ----------
    obj : pydantic.BaseModel
        The model instance to snapshot.
    snapshot_path : Path
        Where to read/write the JSON snapshot. Convention:
        ``tests/fixtures/<vendor>/snapshots/<test-name>.json``.
    write_if_missing : bool
        Default ``True``. When ``False`` and the snapshot file is
        absent, raises :class:`AssertionError`.
    """
    rendered = obj.model_dump_json(indent=2, exclude_none=False)

    if not snapshot_path.exists():
        if not write_if_missing:
            msg = (
                f"Snapshot file missing: {snapshot_path}. "
                "Run the test locally with write_if_missing=True to seed it."
            )
            raise AssertionError(msg)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(rendered, encoding="utf-8")
        return

    expected = snapshot_path.read_text(encoding="utf-8")
    if rendered != expected:
        msg = (
            f"Pydantic snapshot mismatch at {snapshot_path}.\n"
            f"--- expected ---\n{expected}\n"
            f"--- got ---\n{rendered}\n"
            "If the new shape is correct, delete the snapshot and re-run."
        )
        raise AssertionError(msg)


__all__ = [
    "FakeCurlSession",
    "FakeResponse",
    "assert_pydantic_snapshot",
    "replay_fixture",
]

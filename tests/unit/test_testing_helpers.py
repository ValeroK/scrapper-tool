"""Meta-tests for ``scrapper_tool.testing``.

The helpers in ``testing.py`` exist to keep adapter unit tests honest;
this module verifies the helpers themselves are honest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from scrapper_tool.testing import (
    FakeCurlSession,
    FakeResponse,
    assert_pydantic_snapshot,
    replay_fixture,
)

if TYPE_CHECKING:
    from pathlib import Path


class _Sample(BaseModel):
    name: str
    price: float


class TestFakeResponse:
    def test_status_code_default_text(self) -> None:
        resp = FakeResponse(status_code=200)
        assert resp.status_code == 200
        assert resp.text == ""
        assert resp.json() is None

    def test_json_parses_text(self) -> None:
        resp = FakeResponse(status_code=200, text='{"ok": true}')
        assert resp.json() == {"ok": True}


class TestFakeCurlSession:
    def test_reset_clears_class_state(self) -> None:
        FakeCurlSession.STATUS_FOR_PROFILE = {"chrome133a": 200}
        FakeCurlSession.INSTANCES.append(FakeCurlSession(impersonate="chrome133a"))
        FakeCurlSession.reset()
        assert FakeCurlSession.STATUS_FOR_PROFILE == {}
        assert FakeCurlSession.INSTANCES == []

    @pytest.mark.asyncio
    async def test_request_returns_configured_status(self) -> None:
        FakeCurlSession.reset()
        FakeCurlSession.STATUS_FOR_PROFILE = {"chrome133a": 403}
        FakeCurlSession.RESPONSE_TEXT_FOR_PROFILE = {"chrome133a": "blocked"}
        session = FakeCurlSession(impersonate="chrome133a")
        resp = await session.request("GET", "https://example.test/x")
        assert resp.status_code == 403
        assert resp.text == "blocked"
        assert session.calls == [("GET", "https://example.test/x")]

    @pytest.mark.asyncio
    async def test_request_default_status_is_200(self) -> None:
        FakeCurlSession.reset()
        session = FakeCurlSession(impersonate="chrome999")  # not in STATUS_FOR_PROFILE
        resp = await session.request("GET", "https://example.test/y")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_close_is_a_noop(self) -> None:
        FakeCurlSession.reset()
        session = FakeCurlSession()
        await session.close()  # Must not raise

    def test_calls_property_returns_copy(self) -> None:
        FakeCurlSession.reset()
        session = FakeCurlSession()
        # Direct write to internal log — simulate two prior calls.
        session._calls.append(("GET", "https://example.test/a"))
        session._calls.append(("POST", "https://example.test/b"))
        snapshot = session.calls
        assert snapshot == [
            ("GET", "https://example.test/a"),
            ("POST", "https://example.test/b"),
        ]
        # Mutating the snapshot doesn't affect the session's log.
        snapshot.clear()
        assert len(session.calls) == 2


class TestReplayFixture:
    def test_loads_text_and_runs_parser(self, tmp_path: Path) -> None:
        fixture = tmp_path / "page.html"
        fixture.write_text("<html><body>hello</body></html>", encoding="utf-8")
        result = replay_fixture(fixture, lambda html: html.upper())
        assert "HELLO" in result


class TestPydanticSnapshot:
    def test_writes_snapshot_on_first_run(self, tmp_path: Path) -> None:
        snapshot_path = tmp_path / "subdir" / "snapshot.json"
        obj = _Sample(name="X", price=19.99)
        # First run — snapshot doesn't exist; should be written.
        assert_pydantic_snapshot(obj, snapshot_path)
        assert snapshot_path.exists()
        body = snapshot_path.read_text(encoding="utf-8")
        assert '"name": "X"' in body
        assert '"price": 19.99' in body

    def test_passes_when_object_matches_snapshot(self, tmp_path: Path) -> None:
        snapshot_path = tmp_path / "snap.json"
        obj = _Sample(name="X", price=19.99)
        assert_pydantic_snapshot(obj, snapshot_path)  # writes
        assert_pydantic_snapshot(obj, snapshot_path)  # asserts equal — no raise

    def test_fails_when_object_differs_from_snapshot(self, tmp_path: Path) -> None:
        snapshot_path = tmp_path / "snap.json"
        original = _Sample(name="X", price=19.99)
        assert_pydantic_snapshot(original, snapshot_path)  # seed

        drifted = _Sample(name="X", price=29.99)
        with pytest.raises(AssertionError, match="snapshot mismatch"):
            assert_pydantic_snapshot(drifted, snapshot_path)

    def test_write_if_missing_false_raises_when_absent(self, tmp_path: Path) -> None:
        snapshot_path = tmp_path / "missing.json"
        obj = _Sample(name="X", price=19.99)
        with pytest.raises(AssertionError, match="Snapshot file missing"):
            assert_pydantic_snapshot(obj, snapshot_path, write_if_missing=False)

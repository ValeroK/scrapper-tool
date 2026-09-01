"""An unsupported impersonation profile is a fact about us, not about the target.

`curl_cffi` reports an unknown profile as a *transport* error, which at the type
level is indistinguishable from "the host refused the connection". The ladder
treated it as the latter: it retried the identical impossible call three times
and then gave up, so the rungs that would have worked were never tried and a
purely local packaging mismatch was reported as a vendor blocking every request.

Reported against 4.0.0 by a consumer whose `curl-cffi` satisfied our `>=0.7` pin
but predated `chrome150`, the ladder's leading profile.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool import ladder as ladder_mod
from scrapper_tool.errors import BlockedError, ConfigurationError, VendorHTTPError


class TestProfileSupportProbe:
    def test_the_shipped_ladder_is_executable_by_the_pinned_curl_cffi(self) -> None:
        """The regression guard for the dependency pin.

        The floor in pyproject.toml exists to make this true. If a newer profile
        is promoted into the ladder without moving the floor, this fails here
        rather than in a consumer's logs as a vendor block.
        """
        unsupported = [
            p for p in ladder_mod.IMPERSONATE_LADDER if not ladder_mod._profile_is_supported(p)
        ]
        assert not unsupported, (
            f"IMPERSONATE_LADDER names profiles the installed curl_cffi cannot run: "
            f"{unsupported}. Raise the curl-cffi floor in pyproject.toml."
        )

    def test_an_unknown_profile_is_reported_unsupported(self) -> None:
        assert not ladder_mod._profile_is_supported("chrome999_nonexistent")

    def test_an_unreadable_profile_list_treats_everything_as_supported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail open.

        If curl_cffi ever stops exposing its profile list, refusing every profile
        would turn an introspection change into a total outage. Not knowing must
        cost nothing.
        """
        monkeypatch.setattr(ladder_mod, "_supported_profiles", frozenset)
        assert ladder_mod._profile_is_supported("anything-at-all")

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("Impersonating chrome999 is not supported", True),
            ("IMPERSONATING CHROME999 IS NOT SUPPORTED", True),
            ("Connection refused", False),
            ("timed out", False),
        ],
    )
    def test_runtime_error_classification(self, message: str, expected: bool) -> None:
        assert ladder_mod._is_unsupported_profile_error(RuntimeError(message)) is expected


class TestLadderSkipsUnsupportedRungs:
    @staticmethod
    def _served(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
        """Record which profiles actually reached the wire."""

        class _Resp:
            status_code = 200
            url = "https://vendor.test/p"
            text = "<html><body>ok</body></html>"
            headers: dict[str, str] = {}

        async def fake_request(client: Any, method: str, url: str, **kwargs: Any) -> Any:
            calls.append(client.profile)
            return _Resp()

        class _Session:
            def __init__(self, profile: str) -> None:
                self.profile = profile

            async def close(self) -> None: ...

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session(profile: str, **_: Any) -> Any:
            yield _Session(profile)

        monkeypatch.setattr(ladder_mod, "_curl_cffi_session", fake_session)
        monkeypatch.setattr(ladder_mod, "request_with_retry", fake_request)
        monkeypatch.setattr(ladder_mod, "assert_url_allowed_nodns", lambda _u: None)

    @pytest.mark.asyncio
    async def test_an_unsupported_first_rung_does_not_end_the_walk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported bug: rung two was never attempted."""
        calls: list[str] = []
        self._served(monkeypatch, calls)
        monkeypatch.setattr(
            ladder_mod, "_profile_is_supported", lambda p: p != "chrome999_nonexistent"
        )

        resp, profile = await ladder_mod.request_with_ladder(
            "GET", "https://vendor.test/p", ladder=("chrome999_nonexistent", "chrome146")
        )

        assert profile == "chrome146"
        assert resp.status_code == 200
        # The impossible rung must not have cost a single request.
        assert calls == ["chrome146"]

    @pytest.mark.asyncio
    async def test_no_request_is_wasted_on_an_impossible_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It used to burn three retries against a target it never contacted."""
        calls: list[str] = []
        self._served(monkeypatch, calls)
        monkeypatch.setattr(ladder_mod, "_profile_is_supported", lambda p: p == "good")

        await ladder_mod.request_with_ladder(
            "GET", "https://vendor.test/p", ladder=("bad1", "bad2", "good")
        )

        assert calls == ["good"]

    @pytest.mark.asyncio
    async def test_every_rung_unsupported_is_a_configuration_error_not_a_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The heart of the complaint.

        Reporting this as a block is what made a local packaging fault read as a
        vendor refusing every request. Nothing was sent, so nothing can have been
        refused, and the remedy is on this machine.
        """
        calls: list[str] = []
        self._served(monkeypatch, calls)
        monkeypatch.setattr(ladder_mod, "_profile_is_supported", lambda _p: False)

        with pytest.raises(ConfigurationError) as excinfo:
            await ladder_mod.request_with_ladder(
                "GET", "https://vendor.test/p", ladder=("bad1", "bad2")
            )

        message = str(excinfo.value)
        assert "NOT a block" in message
        assert "curl-cffi" in message
        assert calls == []
        # It must not be mistakable for the vendor's doing.
        assert not isinstance(excinfo.value, BlockedError)

    @pytest.mark.asyncio
    async def test_a_runtime_rejection_also_advances_the_ladder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The net for a curl_cffi that does not expose its profile list.

        The up-front probe cannot fire there, so the error has to be classified
        when it is raised instead.
        """
        calls: list[str] = []

        class _Resp:
            status_code = 200
            url = "https://vendor.test/p"
            text = "ok"
            headers: dict[str, str] = {}

        async def fake_request(client: Any, method: str, url: str, **kwargs: Any) -> Any:
            calls.append(client.profile)
            if client.profile == "unknown":
                raise VendorHTTPError("Impersonating unknown is not supported")
            return _Resp()

        class _Session:
            def __init__(self, profile: str) -> None:
                self.profile = profile

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session(profile: str, **_: Any) -> Any:
            yield _Session(profile)

        monkeypatch.setattr(ladder_mod, "_curl_cffi_session", fake_session)
        monkeypatch.setattr(ladder_mod, "request_with_retry", fake_request)
        monkeypatch.setattr(ladder_mod, "assert_url_allowed_nodns", lambda _u: None)
        # Probe says both are fine, so only the runtime error can save the walk.
        monkeypatch.setattr(ladder_mod, "_profile_is_supported", lambda _p: True)

        _resp, profile = await ladder_mod.request_with_ladder(
            "GET", "https://vendor.test/p", ladder=("unknown", "good")
        )

        assert profile == "good"
        assert calls == ["unknown", "good"]

    @pytest.mark.asyncio
    async def test_a_real_transport_failure_still_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only 'not supported' is ours. A refused connection is still the target's."""

        async def fake_request(client: Any, method: str, url: str, **kwargs: Any) -> Any:
            raise VendorHTTPError("Connection refused")

        class _Session:
            def __init__(self, profile: str) -> None:
                self.profile = profile

        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session(profile: str, **_: Any) -> Any:
            yield _Session(profile)

        monkeypatch.setattr(ladder_mod, "_curl_cffi_session", fake_session)
        monkeypatch.setattr(ladder_mod, "request_with_retry", fake_request)
        monkeypatch.setattr(ladder_mod, "_profile_is_supported", lambda _p: True)

        with pytest.raises(VendorHTTPError, match="Connection refused"):
            await ladder_mod.request_with_ladder("GET", "https://vendor.test/p", ladder=("a", "b"))

"""Unit tests for ``scrapper_tool.canary`` — fingerprint-health CLI.

Covers:
- ``run_canary`` happy path (first profile wins, others skipped).
- ``run_canary`` 403 fallback (rotates to next profile).
- ``run_canary`` all-403 (exit_code=1, no winning profile).
- ``run_canary`` empty ladder raises ``ValueError``.
- CLI ``main`` text output ends with newline.
- CLI ``main`` ``--json`` mode emits parseable JSON.
- CLI ``main`` ``--profiles`` overrides the default ladder.
- CLI ``main`` exit codes: 0 on success, 1 on all-blocked.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from scrapper_tool import canary as canary_module
from scrapper_tool import ladder as ladder_module
from scrapper_tool.ladder import IMPERSONATE_LADDER
from scrapper_tool.testing import FakeCurlSession


@pytest.fixture
def fake_curl(monkeypatch: pytest.MonkeyPatch) -> type[FakeCurlSession]:
    FakeCurlSession.reset()
    monkeypatch.setattr(ladder_module, "_CurlCffiAsyncSession", FakeCurlSession)
    return FakeCurlSession


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


class TestRunCanary:
    @pytest.mark.asyncio
    async def test_first_profile_wins_skips_rest(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = {IMPERSONATE_LADDER[0]: 200}
        report = await canary_module.run_canary("https://example.test/x")
        assert report["winning_profile"] == IMPERSONATE_LADDER[0]
        assert report["exit_code"] == 0
        results = report["results"]
        assert isinstance(results, list)
        # All four profiles in the report; first one ran, others skipped.
        assert len(results) == len(IMPERSONATE_LADDER)
        assert results[0]["profile"] == IMPERSONATE_LADDER[0]
        assert results[0]["status"] == 200
        assert results[0]["skipped"] is False
        for skipped in results[1:]:
            assert skipped["skipped"] is True
            assert skipped["status"] is None

    @pytest.mark.asyncio
    async def test_403_rotates_to_next_profile(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = {
            IMPERSONATE_LADDER[0]: 403,
            IMPERSONATE_LADDER[1]: 200,
            IMPERSONATE_LADDER[2]: 200,
            "firefox147": 200,
        }
        report = await canary_module.run_canary("https://example.test/x")
        assert report["winning_profile"] == IMPERSONATE_LADDER[1]
        assert report["exit_code"] == 0
        results = report["results"]
        assert isinstance(results, list)
        assert results[0]["status"] == 403
        assert results[0]["skipped"] is False
        assert results[1]["status"] == 200
        assert results[1]["skipped"] is False
        assert results[2]["skipped"] is True
        assert results[3]["skipped"] is True

    @pytest.mark.asyncio
    async def test_all_blocked_exit_code_1(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        report = await canary_module.run_canary("https://example.test/x")
        assert report["winning_profile"] is None
        assert report["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_empty_ladder_raises(self, fake_curl: type[FakeCurlSession]) -> None:
        with pytest.raises(ValueError, match="at least one"):
            await canary_module.run_canary("https://example.test/x", ladder=())

    @pytest.mark.asyncio
    async def test_custom_ladder(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = {IMPERSONATE_LADDER[1]: 200}
        report = await canary_module.run_canary(
            "https://example.test/x", ladder=(IMPERSONATE_LADDER[1],)
        )
        assert report["winning_profile"] == IMPERSONATE_LADDER[1]
        results = report["results"]
        assert isinstance(results, list)
        assert len(results) == 1


class TestCliMain:
    def test_text_output_human_readable(
        self,
        fake_curl: type[FakeCurlSession],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {IMPERSONATE_LADDER[0]: 200}
        exit_code = canary_module.main(["canary", "https://example.test/x"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "URL: https://example.test/x" in captured.out
        assert f"Effective profile: {IMPERSONATE_LADDER[0]}" in captured.out
        assert captured.out.endswith("\n")

    def test_json_mode_parseable(
        self,
        fake_curl: type[FakeCurlSession],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {IMPERSONATE_LADDER[0]: 200}
        exit_code = canary_module.main(["canary", "https://example.test/x", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["url"] == "https://example.test/x"
        assert parsed["winning_profile"] == IMPERSONATE_LADDER[0]
        assert parsed["exit_code"] == 0

    def test_profiles_flag_overrides_default(
        self,
        fake_curl: type[FakeCurlSession],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome999": 200}
        exit_code = canary_module.main(
            [
                "canary",
                "https://example.test/x",
                "--profiles",
                "chrome999",
                "--json",
            ]
        )
        assert exit_code == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["winning_profile"] == "chrome999"
        # Single-element ladder, so only one result row.
        assert len(parsed["results"]) == 1

    def test_all_blocked_exit_code_1(
        self,
        fake_curl: type[FakeCurlSession],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = dict.fromkeys(IMPERSONATE_LADDER, 403)
        exit_code = canary_module.main(["canary", "https://example.test/x"])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "all blocked" in captured.out

    def test_empty_profiles_flag_errors(
        self,
        fake_curl: type[FakeCurlSession],
    ) -> None:
        # argparse error → SystemExit(2)
        with pytest.raises(SystemExit) as excinfo:
            canary_module.main(["canary", "https://example.test/x", "--profiles", "  ,  "])
        assert excinfo.value.code == 2

    def test_help_flag(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            canary_module.main(["canary", "--help"])
        # argparse exits with code 0 for --help
        assert excinfo.value.code == 0

    def test_no_subcommand_errors(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            canary_module.main([])
        assert excinfo.value.code == 2


class TestVerdictSeparatesDownFromBlocked:
    """The canary reports on OUR fingerprints. It cannot when nothing answered.

    Over twelve weekly runs it failed five times, every one because httpbin.org
    -- a free public echo service -- was overloaded, and every one filed an issue
    advising that an impersonation profile had probably been fingerprinted.
    Nothing had been: nothing got a response, so nothing judged us.

    Both signals were present in the report and simply collapsed together.
    """

    @staticmethod
    def _tried(*statuses: int | None) -> list[dict[str, object]]:
        return [
            {"profile": f"p{i}", "status": st, "elapsed_ms": 1.0, "skipped": False, "error": None}
            for i, st in enumerate(statuses)
        ]

    def test_nothing_answered_is_unreachable_not_blocked(self) -> None:
        """The five real failures. A timeout is not a refusal."""
        verdict, detail = canary_module._verdict(self._tried(None, None, None))
        assert verdict == "unreachable"
        assert "nothing was fingerprinted" in detail

    def test_answered_and_refused_is_blocked(self) -> None:
        """The event the canary exists to catch."""
        verdict, _ = canary_module._verdict(self._tried(403, 403, 403))
        assert verdict == "blocked"

    def test_a_mix_is_degraded_and_names_the_refused_profiles(self) -> None:
        """What fingerprinting looks like before it becomes total."""
        verdict, detail = canary_module._verdict(self._tried(403, 200))
        assert verdict == "degraded"
        assert "p0" in detail

    @pytest.mark.asyncio
    async def test_degraded_reports_without_failing_the_job(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refused profile with another still winning means the ladder WORKED.

        It is the early warning this tool was written to give, so it is carried
        in `verdict` -- but failing a build while scraping succeeds is the same
        false alarm as the outage case, just from the other side.
        """
        seen: list[str] = []

        async def first_403(profile: str, url: str, **kwargs: object) -> tuple[int, float, None]:
            seen.append(profile)
            return (403, 1.0, None) if len(seen) == 1 else (200, 1.0, None)

        monkeypatch.setattr(canary_module, "probe_profile", first_403)
        report = await canary_module.run_canary("https://partial.test/")

        assert report["verdict"] == "degraded"
        assert report["exit_code"] == 0
        assert report["winning_profile"] is not None

    def test_a_win_is_ok(self) -> None:
        verdict, _ = canary_module._verdict(self._tried(200))
        assert verdict == "ok"

    def test_skipped_profiles_do_not_count_as_answers(self) -> None:
        results = [
            *self._tried(200),
            {
                "profile": "later",
                "status": None,
                "elapsed_ms": None,
                "skipped": True,
                "error": None,
            },
        ]
        assert canary_module._verdict(results)[0] == "ok"

    @pytest.mark.asyncio
    async def test_unreachable_does_not_fail_the_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A third-party outage must not file a fingerprinting issue.

        Exit 1 is reserved for "profiles were answered and refused". A weekly
        false alarm teaches everyone to ignore the label, which costs more than
        the alarm was ever worth.
        """

        async def all_timeout(profile: str, url: str, **kwargs: object) -> tuple[None, None, str]:
            return None, None, "curl (28) Operation timed out"

        monkeypatch.setattr(canary_module, "probe_profile", all_timeout)
        report = await canary_module.run_canary("https://down.test/")

        assert report["verdict"] == "unreachable"
        assert report["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_a_real_block_still_fails_the_job(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fix must not cost us the alarm we actually want."""

        async def all_403(profile: str, url: str, **kwargs: object) -> tuple[int, float, None]:
            return 403, 1.0, None

        monkeypatch.setattr(canary_module, "probe_profile", all_403)
        report = await canary_module.run_canary("https://hostile.test/")

        assert report["verdict"] == "blocked"
        assert report["exit_code"] == 1

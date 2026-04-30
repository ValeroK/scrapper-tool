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
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
        report = await canary_module.run_canary("https://example.test/x")
        assert report["winning_profile"] == "chrome133a"
        assert report["exit_code"] == 0
        results = report["results"]
        assert isinstance(results, list)
        # All four profiles in the report; first one ran, others skipped.
        assert len(results) == 4
        assert results[0]["profile"] == "chrome133a"
        assert results[0]["status"] == 200
        assert results[0]["skipped"] is False
        for skipped in results[1:]:
            assert skipped["skipped"] is True
            assert skipped["status"] is None

    @pytest.mark.asyncio
    async def test_403_rotates_to_next_profile(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 200,
            "safari18_0": 200,
            "firefox135": 200,
        }
        report = await canary_module.run_canary("https://example.test/x")
        assert report["winning_profile"] == "chrome124"
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
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 403,
            "safari18_0": 403,
            "firefox135": 403,
        }
        report = await canary_module.run_canary("https://example.test/x")
        assert report["winning_profile"] is None
        assert report["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_empty_ladder_raises(self, fake_curl: type[FakeCurlSession]) -> None:
        with pytest.raises(ValueError, match="at least one"):
            await canary_module.run_canary("https://example.test/x", ladder=())

    @pytest.mark.asyncio
    async def test_custom_ladder(self, fake_curl: type[FakeCurlSession]) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome142": 200}
        report = await canary_module.run_canary("https://example.test/x", ladder=("chrome142",))
        assert report["winning_profile"] == "chrome142"
        results = report["results"]
        assert isinstance(results, list)
        assert len(results) == 1


class TestCliMain:
    def test_text_output_human_readable(
        self,
        fake_curl: type[FakeCurlSession],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
        exit_code = canary_module.main(["canary", "https://example.test/x"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "URL: https://example.test/x" in captured.out
        assert "Effective profile: chrome133a" in captured.out
        assert captured.out.endswith("\n")

    def test_json_mode_parseable(
        self,
        fake_curl: type[FakeCurlSession],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        fake_curl.STATUS_FOR_PROFILE = {"chrome133a": 200}
        exit_code = canary_module.main(["canary", "https://example.test/x", "--json"])
        assert exit_code == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["url"] == "https://example.test/x"
        assert parsed["winning_profile"] == "chrome133a"
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
        fake_curl.STATUS_FOR_PROFILE = {
            "chrome133a": 403,
            "chrome124": 403,
            "safari18_0": 403,
            "firefox135": 403,
        }
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

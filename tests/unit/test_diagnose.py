"""``scrapper-tool diagnose <url>`` -- the escape hatch.

Every verdict here maps to one of the five symptoms in the field report that
prompted the command, because the whole point is to separate them in seconds
rather than days: a wrong path, a real wall, a wall on one network path only, and
a host that simply is not answering all present as "the vendor is blocking us"
when the only evidence is a failed scrape.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from scrapper_tool import diagnose as diag

_URL = "https://vendor.test/parts/68001234AA"
_CAPTCHA = (
    "<html><head><title>Verification</title></head><body>"
    "<h1>Please confirm you are not a robot</h1></body></html>"
)
_REAL = "<html><head><title>Widget</title></head><body>" + ("x" * 9000) + "</body></html>"


def _probe(axis: str, name: str, outcome: str) -> diag.ProbeResult:
    return diag.ProbeResult(axis, name, outcome, detail="")


class TestDescribe:
    """One body, one honest label -- from the shared classifier, not a copy."""

    def test_a_real_page(self) -> None:
        outcome, _ = diag._describe(_REAL, 200, _URL, _URL)
        assert outcome == "ok"

    def test_a_404_is_named_as_a_path_problem(self) -> None:
        """The single most expensive misread in the report."""
        outcome, detail = diag._describe("<html>not found</html>", 404, _URL, _URL)
        assert outcome == "not_found"
        assert "check the path" in detail

    def test_a_vendor_signature_is_a_challenge(self) -> None:
        wall = "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"
        outcome, detail = diag._describe(wall, 200, _URL, _URL)
        assert outcome == "challenge"
        assert "cloudflare" in detail

    def test_a_captcha_redirect_is_a_challenge(self) -> None:
        """The detail now names the EVIDENCE rather than the destination.

        Every gate reports through one verdict since the facade landed, and that
        verdict's vocabulary is the evidence kind -- which is what a caller can
        branch on. The final URL is already its own field.
        """
        outcome, detail = diag._describe(_CAPTCHA, 200, _URL, "https://vendor.test/captcha.html")
        assert outcome == "challenge"
        assert "redirect" in detail

    def test_a_thin_body_is_flagged_but_not_condemned(self) -> None:
        outcome, _ = diag._describe("<html>hi</html>", 200, _URL, _URL)
        assert outcome == "thin"


class TestVerdict:
    """The sentence a human reads first."""

    def test_all_404_reads_as_a_url_problem(self) -> None:
        probes = [_probe("profile", f"p{i}", "not_found") for i in range(3)]
        verdict, reasons = diag._verdict(probes)
        assert verdict == "wrong_url"
        assert "URL problem" in reasons[0]

    def test_any_profile_succeeding_means_reachable(self) -> None:
        probes = [_probe("profile", "a", "ok"), _probe("profile", "b", "challenge")]
        verdict, reasons = diag._verdict(probes)
        assert verdict == "reachable"
        assert any("fingerprinting" in r for r in reasons)

    def test_all_challenged_is_scoped_to_this_network_path(self) -> None:
        """The finding that cost two hours: a wall is not necessarily universal."""
        probes = [_probe("profile", f"p{i}", "challenge") for i in range(3)]
        verdict, reasons = diag._verdict(probes)
        assert verdict == "challenged"
        assert "egress" in reasons[0]

    def test_silence_is_not_reported_as_hostility(self) -> None:
        probes = [_probe("profile", f"p{i}", "timeout") for i in range(3)]
        verdict, reasons = diag._verdict(probes)
        assert verdict == "unreachable"
        assert "rather than anti-bot" in reasons[0]


class TestOutput:
    """Read in a terminal, often over ssh, often on Windows."""

    @staticmethod
    def _report() -> dict[str, Any]:
        d = diag.Diagnosis(url=_URL)
        d.probes = [
            diag.ProbeResult("profile", "chrome150", "ok", "HTTP 200, 51,274 b", 200, 51274, _URL)
        ]
        d.verdict, d.reasons = "reachable", ["one profile worked"]
        return d.to_dict()

    def test_the_table_is_pure_ascii(self) -> None:
        """An em-dash renders as a replacement character in a Windows console."""
        text = diag._format_text(self._report())
        assert text.isascii(), [c for c in text if not c.isascii()]

    def test_every_reason_string_is_ascii(self) -> None:
        for outcome in ("not_found", "ok", "challenge", "timeout"):
            probes = [_probe("profile", "p", outcome)]
            _, reasons = diag._verdict(probes)
            assert all(r.isascii() for r in reasons)

    def test_bytes_is_serialised_under_its_real_name(self) -> None:
        """``bytes_`` is a Python keyword dodge, not part of the contract."""
        row = diag.ProbeResult("profile", "p", "ok", "d", 200, 1234, _URL).to_dict()
        assert row["bytes"] == 1234
        assert "bytes_" not in row


class TestCli:
    def test_a_non_http_url_is_refused_before_any_request(self) -> None:
        parser = argparse.ArgumentParser()
        args = argparse.Namespace(url="file:///etc/passwd", json=False)
        with pytest.raises(SystemExit):
            diag.run_cli(args, parser)

    @pytest.mark.asyncio
    async def test_the_url_guard_runs_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A diagnostic that reaches hosts a scrape would refuse is a scanner.

        This one takes a URL straight off a command line, so the guard is not
        optional and must run before any probe.
        """
        probed: list[str] = []

        async def refuse(url: str, **_: Any) -> None:
            raise ValueError("refused by guard")

        async def spy(url: str) -> list[Any]:
            probed.append(url)
            return []

        monkeypatch.setattr("scrapper_tool._urlguard.assert_url_allowed", refuse)
        monkeypatch.setattr(diag, "_probe_profiles", spy)

        with pytest.raises(ValueError, match="refused by guard"):
            await diag.run_diagnose(_URL)
        assert probed == [], "probes ran despite the guard refusing the URL"

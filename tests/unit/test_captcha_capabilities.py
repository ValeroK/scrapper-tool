"""What this deployment can actually clear, reported before a live target proves it.

Ten captcha kinds are modelled. With no paid key and without the
``[turnstile-solver]`` extra, Turnstile -- which is what Cloudflare serves -- has
exactly one strategy: wait eight seconds and reload. Nothing said so, and the
only way to discover it was to fail slowly on a real page.

The matrix is computed from the installed cascade rather than from documentation,
because the two disagreed.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool.agent.backends.captcha import (
    UNSOLVABLE_KINDS,
    captcha_capabilities,
)
from scrapper_tool.agent.types import AgentConfig


def _by_kind(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["kind"]: row for row in rows}


class TestTheFreeMatrix:
    """No key, no extras -- the configuration most installs actually run."""

    @pytest.fixture()
    def rows(self) -> dict[str, dict[str, Any]]:
        return _by_kind(captcha_capabilities(AgentConfig()))

    def test_every_known_kind_is_reported(self, rows: dict[str, dict[str, Any]]) -> None:
        """Silence about a kind is indistinguishable from coverage of it."""
        assert set(rows) == {
            "turnstile",
            "hcaptcha",
            "recaptcha-v2",
            "recaptcha-v3",
            "image",
            "funcaptcha",
            "arkose",
            "geetest",
            "aws-waf",
            "datadome",
        }

    def test_turnstile_has_only_the_settle(self, rows: dict[str, dict[str, Any]]) -> None:
        """The Cloudflare row, and the reason a reported target never cleared.

        `settle` solves nothing -- it waits and reloads. Reporting that honestly
        is the point of this whole matrix.
        """
        assert rows["turnstile"]["strategies"] == ["settle"]

    def test_the_checkbox_and_slider_kinds_are_covered(
        self, rows: dict[str, dict[str, Any]]
    ) -> None:
        assert "checkbox" in rows["recaptcha-v2"]["strategies"]
        assert "checkbox" in rows["hcaptcha"]["strategies"]
        assert "slider" in rows["geetest"]["strategies"]
        assert "slider" in rows["datadome"]["strategies"]

    @pytest.mark.parametrize("kind", ["image", "funcaptcha", "arkose", "aws-waf"])
    def test_the_paid_only_kinds_say_so(self, kind: str, rows: dict[str, dict[str, Any]]) -> None:
        assert rows[kind]["strategies"] == []
        assert rows[kind]["solvable"] is False
        assert "SCRAPPER_TOOL_CAPTCHA_KEY" in rows[kind]["note"]


class TestUnsolvableIsNotAGap:
    """A gap implies a key would fix it. For v3, nothing does."""

    def test_recaptcha_v3_is_never_solvable(self) -> None:
        with_key = _by_kind(
            captcha_capabilities(AgentConfig(captcha_api_key="k"), vision_available=True)
        )
        row = with_key["recaptcha-v3"]
        assert row["solvable"] is False, "a paid key cannot solve an invisible score"
        assert "score-based" in row["note"]
        assert "fingerprint" in row["note"]

    def test_it_is_declared_unsolvable_by_nature(self) -> None:
        assert "recaptcha-v3" in UNSOLVABLE_KINDS


class TestWhatChangesTheMatrix:
    def test_a_paid_key_covers_the_gaps(self) -> None:
        rows = _by_kind(captcha_capabilities(AgentConfig(captcha_api_key="k")))
        for kind in ("image", "funcaptcha", "arkose", "aws-waf"):
            assert rows[kind]["strategies"] == ["paid"]
            assert rows[kind]["solvable"] is True

    def test_vision_adds_the_grid_tier_only_where_a_grid_exists(self) -> None:
        """v2 and hCaptcha escalate to an image grid; the slider kinds do not."""
        rows = _by_kind(captcha_capabilities(AgentConfig(), vision_available=True))
        assert "vision-grid" in rows["recaptcha-v2"]["strategies"]
        assert "vision-grid" in rows["hcaptcha"]["strategies"]
        assert "vision-grid" not in rows["geetest"]["strategies"]
        assert "vision-grid" not in rows["turnstile"]["strategies"]

    def test_no_vision_removes_the_grid_tier(self) -> None:
        """The no-GPU deployment, reported honestly rather than optimistically."""
        rows = _by_kind(captcha_capabilities(AgentConfig(), vision_available=False))
        assert "vision-grid" not in rows["recaptcha-v2"]["strategies"]
        assert rows["recaptcha-v2"]["strategies"] == ["checkbox"]

    def test_solver_none_disables_everything_free(self) -> None:
        rows = _by_kind(captcha_capabilities(AgentConfig(captcha_solver="none")))
        assert all(not row["strategies"] for row in rows.values())


class TestDoctorReportsCoverage:
    """Install-time visibility, so a gap is found before a live target finds it."""

    def test_uncoverable_kinds_exclude_the_naturally_unsolvable(self) -> None:
        from scrapper_tool.doctor import _uncoverable_captcha_kinds

        gaps = _uncoverable_captcha_kinds(AgentConfig(), vision_ok=False)
        assert "recaptcha-v3" not in gaps, "listing it implies a key would fix it"
        assert set(gaps) == {"image", "funcaptcha", "arkose", "aws-waf"}

    def test_a_paid_key_closes_the_report(self) -> None:
        from scrapper_tool.doctor import _uncoverable_captcha_kinds

        assert _uncoverable_captcha_kinds(AgentConfig(captcha_api_key="k"), vision_ok=True) == []

"""What this deployment can actually clear, reported before a live target proves it.

Ten captcha kinds are modelled and nothing said which of them an install could
do anything about. The matrix is computed from the installed cascade rather than
from documentation, because the two disagreed -- in both directions.

It under-claimed too, which is its own failure mode: the Turnstile probe looked
for a module name that does not exist, so it reported Cloudflare's captcha as
settle-only on installs that had the solver all along. A report that
under-claims sends an operator shopping for a key they do not need.
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

    def test_turnstile_reports_the_solver_when_it_is_installed(
        self, rows: dict[str, dict[str, Any]]
    ) -> None:
        """The Cloudflare row, and a correction worth keeping.

        `settle` solves nothing -- it waits and reloads -- so for a while this
        was believed to be Turnstile's only free strategy. It was not: the
        distribution is `turnstile-solver` and the module is `turnstile_solver`,
        while the probe looked for `theyka`, the upstream project's name and the
        one this codebase calls the tier. The solver was installed the whole time
        via the [full] extra.

        A capability report that UNDER-claims is its own failure: it sends an
        operator shopping for a paid key they already do not need.
        """
        import importlib.util

        expected = ["settle"]
        if importlib.util.find_spec("turnstile_solver") is not None:
            expected.append("turnstile-solver")
        assert rows["turnstile"]["strategies"] == expected

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


class TestVisionProbeResultIsParsedNotCompared:
    """`_captcha_vision_state` returns a sentence, not a status token.

    It is written to be read in a report -- "<model> ok", "<model> (probe
    failed)", "<model> NOT AVAILABLE" -- so comparing it to the bare literal
    "ok" was always False, and coverage was computed as if no model existed even
    on a host serving one.

    It hid in the captcha matrix because vision only adds a `vision-grid` tier to
    kinds that already have `checkbox`, so the UNCOVERABLE list never moved. It
    surfaced the moment wall detection reported the same flag directly, which is
    an argument for reporting a derived value in more than one place.
    """

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("qwen3.8-27b-apex ok", True),
            ("reuses model (qwen3-vl:8b) ok", True),
            ("gemma-4 (probe failed)", False),
            ("gemma-4 NOT AVAILABLE", False),
            ("gemma-4 (LLM unreachable)", False),
            ("ok", False),
            (None, False),
            ("", False),
        ],
    )
    def test_only_a_successful_probe_counts(self, state: str | None, expected: bool) -> None:
        from scrapper_tool.doctor import _vision_probe_succeeded

        assert _vision_probe_succeeded(state) is expected


class TestWallDetectionIsReported:
    """Absence must be legible at install time, not deduced from a missed wall.

    The 3.2.0 lesson was that a vision tier can be switched off for months
    without anyone noticing.
    """

    def test_both_halves_active(self) -> None:
        from scrapper_tool.doctor import _wall_detection_state

        assert "vision" in _wall_detection_state(vision_ok=True)

    def test_no_model_says_what_will_be_missed(self) -> None:
        from scrapper_tool.doctor import _wall_detection_state

        state = _wall_detection_state(vision_ok=False)
        assert "markup only" in state
        assert "missed" in state

    def test_switched_off_is_distinguished_from_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ "I turned it off" and "it could not start" need different fixes."""
        from scrapper_tool.doctor import _wall_detection_state

        monkeypatch.setenv("SCRAPPER_TOOL_VISION_WALL_DETECT", "0")
        assert "disabled by" in _wall_detection_state(vision_ok=True)

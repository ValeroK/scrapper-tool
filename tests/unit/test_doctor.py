"""Unit tests for ``scrapper_tool.doctor`` — the cascade preflight command.

Covers:
- Every tier probe, in its ok / degraded / missing / blocked / disabled states.
- Status + exit-code resolution, including the A/B/C floor.
- ``--require-tier`` as a healthcheck gate, and rejection of an unknown tier.
- Both renderers, and that ``--json`` emits parseable JSON.
- The Fixes block de-duplicates while preserving order.
- The two findings doctor exists to surface: module-ok-but-binary-missing, and
  E2 being unrunnable on the default backend.

Hermetic throughout: every probe is monkeypatched, so these assert doctor's
*logic*, not the machine it runs on. ``run_doctor`` returns plain data with
``exit_code`` as a field, following ``run_canary``'s contract, so the renderers
and the CLI can be tested against a hand-built report.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scrapper_tool import _extras
from scrapper_tool import doctor as doctor_module


class _FakeCfg:
    """Stand-in for AgentConfig — only the fields doctor reads."""

    def __init__(
        self,
        browser: str = "patchright",
        llm: str = "ollama",
        model: str = "qwen3-vl:8b",
        ollama_url: str = "http://localhost:11434",
    ) -> None:
        self.browser = browser
        self.llm = llm
        self.model = model
        self.ollama_url = ollama_url


@pytest.fixture
def all_healthy(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Pin every probe to its happy answer; individual tests break one thing."""
    monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_DIR", str(tmp_path / "recipes"))
    monkeypatch.setattr(_extras, "hostile_available", lambda: True)
    monkeypatch.setattr(_extras, "agent_available", lambda: True)
    monkeypatch.setattr(_extras, "crawl4ai_available", lambda: True)
    monkeypatch.setattr(_extras, "geoip2_available", lambda: True)
    monkeypatch.setattr(_extras, "cookie_backend_available", lambda: True)
    monkeypatch.setattr(_extras, "browser_binary_present", lambda *a, **k: True)
    monkeypatch.setattr(_extras, "check_browser_module", lambda b: "ok")
    monkeypatch.setattr(_extras, "user_data_dir_supported", lambda: True)
    monkeypatch.setattr(_extras, "crawl4ai_accepts", lambda p: True)
    monkeypatch.setattr(_extras, "browser_use_accepts", lambda p: True)
    monkeypatch.setattr(_extras, "obscura_endpoint_reachable", lambda *a, **k: True)
    monkeypatch.setattr(_extras, "render_tier_enabled", lambda: True)

    async def _llm_ok(_cfg: Any) -> tuple[bool, bool]:
        return True, True

    monkeypatch.setattr(_extras, "probe_llm", _llm_ok)
    monkeypatch.setattr(doctor_module, "_load_agent_config", lambda: (_FakeCfg(), None))


class TestTierProbes:
    def test_a_b_c_is_ok_on_a_bare_install(self) -> None:
        """A/B/C rides core deps only — it must never report broken."""
        assert doctor_module._probe_a_b_c().status == "ok"

    def test_d_missing_without_hostile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_extras, "hostile_available", lambda: False)
        result = doctor_module._probe_d()
        assert result.status == "missing"
        assert "[hostile]" in result.detail
        assert any("hostile" in fix for fix in result.fixes)

    def test_d_degraded_when_module_present_but_binary_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_extras, "hostile_available", lambda: True)
        monkeypatch.setattr(_extras, "browser_binary_present", lambda *a, **k: False)
        result = doctor_module._probe_d()
        assert result.status == "degraded"

    def test_render_disabled_is_not_a_fault(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_extras, "render_tier_enabled", lambda: False)
        result = doctor_module._probe_render(_FakeCfg())
        assert result.status == "disabled"
        assert result.fixes == []

    def test_render_reports_module_ok_binary_missing_separately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The `camoufox fetch || true` case: import succeeds, blob is absent."""
        monkeypatch.setattr(_extras, "render_tier_enabled", lambda: True)
        monkeypatch.setattr(_extras, "check_browser_module", lambda b: "ok")
        monkeypatch.setattr(_extras, "browser_binary_present", lambda *a, **k: False)
        result = doctor_module._probe_render(_FakeCfg(browser="camoufox"))
        assert result.status == "degraded"
        assert "binary is missing" in result.detail
        assert result.fixes == ["camoufox fetch"]

    @pytest.mark.asyncio
    async def test_e1_degraded_when_llm_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_extras, "crawl4ai_available", lambda: True)

        async def _unreachable(_cfg: Any) -> tuple[bool, bool]:
            return False, False

        monkeypatch.setattr(_extras, "probe_llm", _unreachable)
        result = await doctor_module._probe_e1(_FakeCfg())
        assert result.status == "degraded"
        assert "unreachable" in result.detail

    @pytest.mark.asyncio
    async def test_e1_names_the_model_when_only_the_model_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_extras, "crawl4ai_available", lambda: True)

        async def _no_model(_cfg: Any) -> tuple[bool, bool]:
            return True, False

        monkeypatch.setattr(_extras, "probe_llm", _no_model)
        result = await doctor_module._probe_e1(_FakeCfg(model="llama9"))
        assert result.status == "degraded"
        assert "llama9" in result.detail
        assert result.fixes == ["ollama pull llama9"]

    @pytest.mark.asyncio
    async def test_e1_survives_a_hanging_llm_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A diagnostic that hangs is a failed diagnosis."""
        import asyncio

        monkeypatch.setattr(_extras, "crawl4ai_available", lambda: True)
        monkeypatch.setattr(doctor_module, "_PROBE_TIMEOUT_S", 0.05)

        async def _hang(_cfg: Any) -> tuple[bool, bool]:
            await asyncio.sleep(10)
            return True, True

        monkeypatch.setattr(_extras, "probe_llm", _hang)
        result = await doctor_module._probe_e1(_FakeCfg())
        assert result.status == "degraded"
        assert "timed out" in result.detail

    def test_e2_blocked_on_the_default_backend(self) -> None:
        """The headline finding: a stock config can never run E2."""
        result = doctor_module._probe_e2(_FakeCfg(browser="camoufox"))
        assert result.status == "blocked"
        assert "no CDP" in result.detail
        assert any("patchright" in fix for fix in result.fixes)

    def test_e2_ok_on_patchright(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_extras, "browser_binary_present", lambda *a, **k: True)
        assert doctor_module._probe_e2(_FakeCfg(browser="patchright")).status == "ok"

    def test_e2_degraded_when_obscura_endpoint_is_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_extras, "obscura_endpoint_reachable", lambda *a, **k: False)
        result = doctor_module._probe_e2(_FakeCfg(browser="obscura"))
        assert result.status == "degraded"

    def test_cookies_missing_without_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_extras, "cookie_backend_available", lambda: False)
        result = doctor_module._probe_cookies()
        assert result.status == "missing"
        assert any("cookies" in fix for fix in result.fixes)

    def test_replay_degraded_when_cache_dir_unusable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """Root ignores mode bits, so make the *parent* a file instead.

        ``mkdir`` under a regular file raises NotADirectoryError for every user,
        which keeps this test meaningful in CI containers that run as root.
        """
        not_a_dir = tmp_path / "afile"
        not_a_dir.write_text("")
        monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_DIR", str(not_a_dir / "recipes"))
        result = doctor_module._probe_replay()
        assert result.status == "degraded"
        assert "not writable" in result.detail


class TestStatusResolution:
    def _tiers(self, **overrides: str) -> dict[str, Any]:
        base = dict.fromkeys(doctor_module.REPORT_TIERS, "ok")
        base.update(overrides)
        return {name: doctor_module._TierResult(status, "detail") for name, status in base.items()}

    def test_all_ok_is_ready(self) -> None:
        status, code = doctor_module._resolve_status(self._tiers(), require_tier=None)
        assert (status, code) == ("ready", 0)

    def test_disabled_does_not_count_against_health(self) -> None:
        status, code = doctor_module._resolve_status(
            self._tiers(render="disabled"), require_tier=None
        )
        assert (status, code) == ("ready", 0)

    def test_one_broken_tier_is_degraded(self) -> None:
        status, code = doctor_module._resolve_status(self._tiers(e2="blocked"), require_tier=None)
        assert (status, code) == ("degraded", 1)

    def test_broken_a_b_c_is_not_ready(self) -> None:
        """A/B/C is the floor: below it the install isn't merely degraded."""
        status, code = doctor_module._resolve_status(
            self._tiers(a_b_c="missing"), require_tier=None
        )
        assert (status, code) == ("not_ready", 2)

    def test_require_tier_passes_when_that_tier_is_ok(self) -> None:
        status, code = doctor_module._resolve_status(
            self._tiers(e2="blocked"), require_tier="a_b_c"
        )
        assert (status, code) == ("ready", 0)

    def test_require_tier_fails_on_that_tier_even_if_rest_are_fine(self) -> None:
        status, code = doctor_module._resolve_status(self._tiers(e2="blocked"), require_tier="e2")
        assert (status, code) == ("not_ready", 2)


@pytest.mark.usefixtures("all_healthy")
class TestRunDoctorHealthy:
    @pytest.mark.asyncio
    async def test_reports_ready_when_everything_passes(self) -> None:
        report = await doctor_module.run_doctor()
        assert report["status"] == "ready"
        assert report["exit_code"] == 0
        assert report["fixes"] == []

    @pytest.mark.asyncio
    async def test_report_covers_every_declared_tier(self) -> None:
        report = await doctor_module.run_doctor()
        assert set(report["tiers"]) == set(doctor_module.REPORT_TIERS)

    @pytest.mark.asyncio
    async def test_tier_names_match_the_cascades_own_vocabulary(self) -> None:
        """Row labels must match pattern_used / escalation_log or they mislead."""
        from scrapper_tool.recipe.policy import TIER_ORDER

        assert set(TIER_ORDER) <= set(doctor_module.REPORT_TIERS)

    @pytest.mark.asyncio
    async def test_never_prints_the_captcha_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_CAPTCHA_KEY", "super-secret-value")
        report = await doctor_module.run_doctor()
        assert report["checks"]["captcha_key"] == "set"
        assert "super-secret-value" not in json.dumps(report)


class TestFixesBlock:
    @pytest.mark.asyncio
    async def test_deduplicates_while_preserving_order(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """The same pip line is often the remedy for several tiers at once."""
        monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_DIR", str(tmp_path / "r"))
        monkeypatch.setattr(_extras, "agent_available", lambda: True)
        monkeypatch.setattr(_extras, "hostile_available", lambda: False)
        monkeypatch.setattr(_extras, "crawl4ai_available", lambda: False)
        monkeypatch.setattr(_extras, "cookie_backend_available", lambda: False)
        monkeypatch.setattr(_extras, "geoip2_available", lambda: True)
        monkeypatch.setattr(_extras, "check_browser_module", lambda b: "missing")
        monkeypatch.setattr(_extras, "browser_binary_present", lambda *a, **k: False)
        monkeypatch.setattr(_extras, "user_data_dir_supported", lambda: False)
        monkeypatch.setattr(_extras, "render_tier_enabled", lambda: True)
        monkeypatch.setattr(doctor_module, "_load_agent_config", lambda: (_FakeCfg(), None))

        report = await doctor_module.run_doctor()
        fixes = report["fixes"]
        assert len(fixes) == len(set(fixes)), f"duplicate fixes: {fixes}"
        # render and e1 both want [llm-agent]; it must appear exactly once.
        llm_agent_lines = [f for f in fixes if "llm-agent" in f]
        assert len(llm_agent_lines) == 1


class TestRenderers:
    def _report(self) -> dict[str, Any]:
        return {
            "status": "degraded",
            "version": "9.9.9",
            "exit_code": 1,
            "require_tier": None,
            "tiers": {
                name: {"status": "ok", "detail": f"{name} detail", "fixes": []}
                for name in doctor_module.REPORT_TIERS
            },
            "checks": {"platform": "linux"},
            "fixes": ["pip install 'scrapper-tool[cookies]'"],
        }

    def test_text_lists_every_tier_and_the_version(self) -> None:
        text = doctor_module._format_text(self._report())
        assert "9.9.9" in text
        assert "degraded" in text
        for name in doctor_module.REPORT_TIERS:
            assert name in text

    def test_text_includes_the_fixes_block(self) -> None:
        text = doctor_module._format_text(self._report())
        assert "Fixes:" in text
        assert "scrapper-tool[cookies]" in text

    def test_text_omits_the_fixes_block_when_healthy(self) -> None:
        report = self._report()
        report["fixes"] = []
        assert "Fixes:" not in doctor_module._format_text(report)


@pytest.mark.usefixtures("all_healthy")
class TestCliIntegration:
    def test_text_mode_exits_zero_when_ready(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = doctor_module.main([])
        assert code == 0
        assert "scrapper-tool doctor" in capsys.readouterr().out

    def test_json_mode_emits_parseable_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = doctor_module.main(["--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ready"
        assert set(payload["tiers"]) == set(doctor_module.REPORT_TIERS)

    def test_output_ends_with_a_newline(self, capsys: pytest.CaptureFixture[str]) -> None:
        doctor_module.main([])
        assert capsys.readouterr().out.endswith("\n")

    def test_require_tier_gate_passes(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert doctor_module.main(["--require-tier", "a_b_c"]) == 0
        capsys.readouterr()

    def test_unknown_require_tier_is_a_usage_error(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            doctor_module.main(["--require-tier", "not-a-tier"])
        assert excinfo.value.code == 2

    def test_main_forwards_explicit_argv_not_sys_argv(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: main(None) once built ["doctor"] and dropped every flag."""
        monkeypatch.setattr("sys.argv", ["scrapper-tool-doctor", "--json"])
        doctor_module.main()
        json.loads(capsys.readouterr().out)  # would raise if --json were dropped

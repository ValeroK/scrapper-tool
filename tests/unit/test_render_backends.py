"""Render-tier backend exhaustion.

A bot wall is a verdict on *this browser*, not on the render tier. Measured on
tascaparts.com, Camoufox got a clean HTTP 200 where Patchright earned a hard
"you have been blocked" WAF page; on other targets that inverts. Backends are
complementary rather than ranked, which is the whole reason trying a second one
beats picking a better one — and why a walled render retries on a different
engine before the cascade pays for an LLM tier.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool import http_server
from scrapper_tool.agent.backends.browser import BACKEND_CAPABILITIES
from scrapper_tool.agent.types import AgentConfig

_WALL = "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"
_GOOD = (
    '<html><head><script type="application/ld+json">'
    '{"@context":"https://schema.org","@type":"Product","name":"Widget","sku":"X1",'
    '"offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}'
    "</script></head><body></body></html>"
)
_NO_SIGNAL = "<html><head><title>Real page</title></head><body><p>hi</p></body></html>"


def _req(browser: str | None = None, learned: str | None = None) -> Any:
    """The real request model, so these tests cannot drift from production."""
    req = http_server.ScrapeRequest(url="https://walled.test/p", browser=browser)
    if learned:
        req.__dict__["_policy_best_backend"] = learned
    return req


class TestRenderBackendCandidates:
    """Which browsers the render tier is willing to burn on one URL."""

    def test_default_is_two_backends_across_two_engines(self) -> None:
        """The second attempt must change engine or it is not worth paying for."""
        candidates = http_server._render_backend_candidates(_req(), AgentConfig(browser="camoufox"))
        assert len(candidates) == 2
        assert len({BACKEND_CAPABILITIES[n].engine for n in candidates}) == 2

    def test_an_explicit_browser_disables_the_search(self) -> None:
        """A caller who named a backend has decided; there is nothing to search."""
        candidates = http_server._render_backend_candidates(
            _req(browser="patchright"), AgentConfig(browser="patchright")
        )
        assert candidates == ("patchright",)

    def test_the_learned_backend_leads(self) -> None:
        """Evidence about this domain outranks the operator's global default."""
        candidates = http_server._render_backend_candidates(
            _req(learned="patchright"), AgentConfig(browser="camoufox")
        )
        assert candidates[0] == "patchright"
        assert "camoufox" in candidates

    def test_the_configured_backend_leads_when_nothing_was_learned(self) -> None:
        candidates = http_server._render_backend_candidates(_req(), AgentConfig(browser="obscura"))
        assert candidates[0] == "obscura"

    def test_the_cap_is_tunable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_MAX_BACKENDS", "4")
        candidates = http_server._render_backend_candidates(_req(), AgentConfig(browser="camoufox"))
        assert len(candidates) == 4

    def test_a_garbage_cap_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_MAX_BACKENDS", "not-a-number")
        candidates = http_server._render_backend_candidates(_req(), AgentConfig(browser="camoufox"))
        assert len(candidates) == 2

    def test_the_cap_can_never_be_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero would silently disable the tier that was asked for."""
        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_MAX_BACKENDS", "0")
        candidates = http_server._render_backend_candidates(_req(), AgentConfig(browser="camoufox"))
        assert len(candidates) == 1


class TestRenderBackendExhaustion:
    @staticmethod
    def _install(
        monkeypatch: pytest.MonkeyPatch, per_backend: dict[str, str], calls: list[str]
    ) -> None:
        import scrapper_tool.patterns.render as render_mod

        monkeypatch.setenv("SCRAPPER_TOOL_RENDER_TIER", "1")

        async def fake_render_html(url: str, **kwargs: Any) -> Any:
            backend = kwargs["browser"]
            calls.append(backend)
            html = per_backend.get(backend)
            if html is None:
                raise RuntimeError(f"{backend} unavailable")
            return render_mod.RenderResult(html=html, status=200, final_url=url)

        monkeypatch.setattr(render_mod, "render_html", fake_render_html)

    @pytest.mark.asyncio
    async def test_a_walled_render_retries_on_a_different_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        self._install(monkeypatch, {"camoufox": _WALL, "patchright": _GOOD}, calls)
        req = _req()

        response, error = await http_server._do_render_step(req, [], 0.0)

        assert calls == ["camoufox", "patchright"]
        assert error is None
        assert response is not None
        assert response["pattern_used"] == "render"
        assert req.__dict__["_render_backend_used"] == "patchright"

    @pytest.mark.asyncio
    async def test_a_clean_render_never_pays_for_a_second_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        self._install(monkeypatch, {"camoufox": _GOOD, "patchright": _GOOD}, calls)

        response, _ = await http_server._do_render_step(_req(), [], 0.0)

        assert calls == ["camoufox"]
        assert response is not None

    @pytest.mark.asyncio
    async def test_the_retry_is_visible_in_the_escalation_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller must be able to see that two browsers were spent."""
        self._install(monkeypatch, {"camoufox": _WALL, "patchright": _GOOD}, [])
        req = _req()

        await http_server._do_render_step(req, [], 0.0)

        rows = req.__dict__["_escalation_log"]
        walled = [r for r in rows if r["outcome"] == "failed" and r["reason"] == "blocked"]
        assert len(walled) == 1
        assert "camoufox" in walled[0]["detail"]
        assert "cloudflare" in walled[0]["detail"]
        assert "patchright" in walled[0]["detail"]

    @pytest.mark.asyncio
    async def test_a_crashing_backend_does_not_end_the_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing extra is a fact about one backend, not about the tier."""
        calls: list[str] = []
        self._install(monkeypatch, {"patchright": _GOOD}, calls)

        response, _ = await http_server._do_render_step(_req(), [], 0.0)

        assert calls == ["camoufox", "patchright"]
        assert response is not None

    @pytest.mark.asyncio
    async def test_every_backend_walled_falls_through_to_the_llm_tiers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exhausting the backends is not a win; the cascade must keep climbing."""
        calls: list[str] = []
        self._install(monkeypatch, {"camoufox": _WALL, "patchright": _WALL}, calls)

        response, error = await http_server._do_render_step(_req(), [], 0.0)

        assert calls == ["camoufox", "patchright"]
        assert response is None
        assert error is None

    @pytest.mark.asyncio
    async def test_a_no_signal_page_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only a wall is backend-dependent evidence.

        A page that rendered fine but carries nothing extractable will render
        exactly as emptily on the next engine, so retrying it doubles the cost
        for a guaranteed identical answer.
        """
        calls: list[str] = []
        self._install(monkeypatch, {"camoufox": _NO_SIGNAL, "patchright": _GOOD}, calls)

        response, _ = await http_server._do_render_step(_req(), [], 0.0)

        assert calls == ["camoufox"]
        assert response is None

    @pytest.mark.asyncio
    async def test_the_winning_backend_is_remembered_for_the_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Next request on this domain starts where the last one succeeded."""
        from scrapper_tool.recipe.policy import get_policy_store

        self._install(monkeypatch, {"camoufox": _WALL, "patchright": _GOOD}, [])
        req = _req()

        response, _ = await http_server._do_render_step(req, [], 0.0)
        assert response is not None
        http_server._record_policy(response, req)

        policy = get_policy_store().get("https://walled.test/p")
        assert policy is not None
        assert policy.best_backend == "patchright"

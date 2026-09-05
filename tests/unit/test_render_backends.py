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


class TestTheLastEngineIsStillJudged:
    """scrapper-tool#32 — the wall verdict was discarded when nothing was left to try.

    `is_last` was meant to end the *search*. It also ended the *verdict*: the
    loop classified every attempt and then broke without acting on the answer
    whenever there was no spare engine, so whichever backend went second won by
    going second. With a single candidate -- an explicit ``browser=``, or a cap
    of one -- the wall check never ran at all.

    Every test above missed it because `_WALL` carries nothing extractable, so
    the walled page was rejected a few lines later for `no_signal` and the case
    passed for the wrong reason. The fixtures here close that gap from both
    sides: a caller whose accept rule a wall satisfies, and a wall that carries a
    signal of its own.
    """

    #: `mode="fetch"` accepts every page by definition -- the caller only wanted
    #: the bytes. It is the shape of a sidecar that parses HTML itself, and it is
    #: how the reported deployment was calling this tier.
    @staticmethod
    def _fetch_req(browser: str | None = None) -> Any:
        return http_server.ScrapeRequest(url="https://walled.test/p", mode="fetch", browser=browser)

    @pytest.mark.asyncio
    async def test_a_wall_is_not_a_win_just_because_the_caller_wanted_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        TestRenderBackendExhaustion._install(
            monkeypatch, {"camoufox": _WALL, "patchright": _WALL}, calls
        )

        response, error = await http_server._do_render_step(self._fetch_req(), [], 0.0)

        assert calls == ["camoufox", "patchright"]
        assert response is None, "an interstitial was returned as content"
        assert error is None

    @pytest.mark.asyncio
    async def test_one_candidate_is_judged_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The worst case: `is_last` is true on the first iteration.

        An explicit ``browser=`` collapses the candidate list to one, so the
        wall check was unreachable on every request that named a backend.
        """
        calls: list[str] = []
        TestRenderBackendExhaustion._install(monkeypatch, {"patchright": _WALL}, calls)

        response, _ = await http_server._do_render_step(
            self._fetch_req(browser="patchright"), [], 0.0
        )

        assert calls == ["patchright"]
        assert response is None

    @pytest.mark.asyncio
    async def test_the_vendor_is_reported_rather_than_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`challenge_detected=None` on a Cloudflare wall is the reported symptom.

        Knowing a target is walled is the single most useful thing for tuning it,
        and the tier that had the evidence in hand was throwing it away.
        """
        TestRenderBackendExhaustion._install(
            monkeypatch, {"camoufox": _WALL, "patchright": _WALL}, []
        )
        req = self._fetch_req()

        await http_server._do_render_step(req, [], 0.0)

        assert req.__dict__.get("_challenge_detected") == "cloudflare"
        rows = req.__dict__["_escalation_log"]
        rejected = [r for r in rows if r["step"] == "render" and r["reason"] == "blocked"]
        assert rejected, "the escalation log must show why render did not win"
        assert "cloudflare" in rejected[-1]["detail"]

    @pytest.mark.asyncio
    async def test_the_policy_does_not_learn_the_laundering_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The entrenchment half of #32.

        `_record_policy` keys off the payload, and the walled payload said
        `blocked=False`. Three observations later the domain was pre-routed
        straight to the tier that returns interstitials, `challenge_vendor`
        stayed null, and E2 was never attempted.
        """
        from scrapper_tool.recipe.policy import get_policy_store

        TestRenderBackendExhaustion._install(
            monkeypatch, {"camoufox": _WALL, "patchright": _WALL}, []
        )
        req = self._fetch_req()

        response, _ = await http_server._do_render_step(req, [], 0.0)
        http_server._record_policy(response, req)

        assert get_policy_store().get("https://walled.test/p") is None

    @pytest.mark.asyncio
    async def test_a_clean_page_still_wins_for_a_fetch_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must cost nothing when there is no wall."""
        TestRenderBackendExhaustion._install(monkeypatch, {"camoufox": _NO_SIGNAL}, [])

        response, _ = await http_server._do_render_step(self._fetch_req(), [], 0.0)

        assert response is not None
        assert response["pattern_used"] == "render"
        assert response["blocked"] is False

    @pytest.mark.asyncio
    async def test_a_wall_that_carries_a_signal_is_still_a_wall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other half of why the suite was blind to this.

        `no_signal` was doing the wall's job by accident. A challenge page that
        also carries JSON-LD -- a vendor that walls a product URL while leaving
        the head intact, which is common -- defeats that accident entirely.
        """
        walled_with_signal = _WALL + _GOOD
        TestRenderBackendExhaustion._install(
            monkeypatch, {"camoufox": walled_with_signal, "patchright": walled_with_signal}, []
        )

        response, _ = await http_server._do_render_step(_req(), [], 0.0)

        assert response is None


class TestWhichEvidenceOverrulesThePage:
    """`classify_wall` grades its evidence, and only one grade may reject content.

    Getting this backwards breaks one of the two cases the render tier exists
    for. A named vendor is an identification. `"unknown"` is inferred from a
    block status on a small body -- an argument from absence, and store.mopar.com
    serves a real DOM under HTTP 403, so absence is exactly what it is not.
    """

    @pytest.mark.parametrize("evidence", ["cloudflare", "redirect", "host_titled_wall", "radware"])
    def test_an_identified_vendor_rejects_even_a_page_with_content(self, evidence: str) -> None:
        assert http_server._wall_outranks_content(evidence, has_signal=True)

    def test_a_bare_block_status_defers_to_the_content(self) -> None:
        """The mopar case: 403 + a real Product is a win, not a wall."""
        assert not http_server._wall_outranks_content("unknown", has_signal=True)

    def test_a_bare_block_status_with_nothing_in_it_still_rejects(self) -> None:
        """Where the two agree, there is nothing to weigh."""
        assert http_server._wall_outranks_content("unknown", has_signal=False)

"""The model as a second opinion, and what happens when there is no model.

Vision is not here because it is more accurate. Measured on the real fixtures it
is *worse* than markup -- 2 correct against 6, with one outright miss. It is here
because markup's 6 of 6 is a memorisation score: three of its detectors were
written after the exact pages they now catch, so it cannot recognise the next
wall by construction. The model identified the reported host-titled interstitial
cold, from a prompt naming no vendor, no class and no phrase.

So the tests that matter most are the ones pinning where it must NOT interfere.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool.agent.backends.wall_vision import look_at_page
from scrapper_tool.patterns import render as render_mod

_URL = "https://www.vendor.test/parts/1"
_SIGNATURE_WALL = (
    "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"
)
_REAL_PAGE = (
    '<html><head><script type="application/ld+json">{"@type":"Product","name":"W"}</script>'
    "</head><body><h1>Widget</h1><p>Real words on a real page.</p></body></html>"
)
#: A wall markup genuinely cannot see: it has a title (so the content-free-shell
#: net does not catch it), carries no vendor signature, was reached with no
#: redirect, and does not name itself after the host. Verified against
#: `classify_wall` in the test below rather than assumed -- an earlier version of
#: this fixture WAS caught, which would have made every assertion here vacuous.
_UNKNOWN_WALL = (
    '<html><head><title>Checking</title></head><body><div class="Xy9Qz">'
    "<p>One moment please.</p></div><script>var a=1;</script></body></html>"
)


class _Backend:
    """A stand-in vision model with a scripted sequence of replies."""

    model = "fake-vlm"

    def __init__(self, *replies: str) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def complete_vision(self, prompt: str, images: Any, **kwargs: Any) -> str:
        self.calls += 1
        if not self._replies:
            return "PAGE"
        return self._replies.pop(0)


class _Page:
    """A page that paints, counts screenshots, and can be waited on."""

    def __init__(self, url: str = _URL) -> None:
        self.url = url
        self.shots = 0
        self.waited_ms = 0

    async def screenshot(self, **_: Any) -> bytes:
        self.shots += 1
        return b"\x89PNG-not-really"

    async def wait_for_timeout(self, ms: int) -> None:
        self.waited_ms += ms


def test_the_unknown_wall_fixture_really_is_unknown_to_markup() -> None:
    """Guards every assertion below it.

    If markup can see this fixture, the tests that claim the model was consulted
    are testing nothing. An earlier version of the fixture was caught by the
    content-free-shell net, which made exactly that mistake.
    """
    from scrapper_tool._challenge import classify_wall

    assert not classify_wall(_UNKNOWN_WALL, 200, requested_url=_URL, final_url=_URL).walled


class TestLookAtPage:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("reply", ["WALL", "wall", "  WALL  ", "The answer is WALL"])
    async def test_it_reads_a_wall_verdict(self, reply: str) -> None:
        assert await look_at_page(b"png", _Backend(reply)) == "WALL"

    @pytest.mark.asyncio
    async def test_blank_is_a_verdict_not_a_failure(self) -> None:
        """Three of six real fixtures painted nothing.

        A binary prompt was confidently wrong on all three; letting the model
        abstain is what makes it safe to believe when it does answer.
        """
        assert await look_at_page(b"png", _Backend("BLANK")) == "BLANK"

    @pytest.mark.asyncio
    async def test_an_unparseable_reply_is_no_opinion(self) -> None:
        assert await look_at_page(b"png", _Backend("I'm not sure about this one")) is None

    @pytest.mark.asyncio
    async def test_a_raising_model_never_propagates(self) -> None:
        """An enrichment on top of a render that already happened."""

        class _Broken:
            model = "broken"

            async def complete_vision(self, *a: Any, **k: Any) -> str:
                raise RuntimeError("model on fire")

        assert await look_at_page(b"png", _Broken()) is None

    @pytest.mark.asyncio
    async def test_no_screenshot_means_no_call(self) -> None:
        backend = _Backend("WALL")
        assert await look_at_page(b"", backend) is None
        assert backend.calls == 0


class TestSecondOpinion:
    """Where the model is consulted, and where it must stay out of the way."""

    @staticmethod
    def _vision(monkeypatch: pytest.MonkeyPatch, backend: Any) -> None:
        async def _backend() -> Any:
            return backend

        monkeypatch.setattr(render_mod, "_vision_backend", _backend)

    @pytest.mark.asyncio
    async def test_markup_answers_first_and_the_model_is_not_asked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Markup is better on everything it has a signature for, and free.

        Spending 3.6s confirming a verdict we already hold would be pure cost.
        """
        backend = _Backend("WALL")
        self._vision(monkeypatch, backend)

        evidence = await render_mod._second_opinion(_Page(), _URL, _SIGNATURE_WALL, 200)

        assert evidence == "cloudflare"
        assert backend.calls == 0, "the model was asked about a page markup had already judged"

    @pytest.mark.asyncio
    async def test_the_model_is_asked_when_markup_found_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case vision exists for: a wall with no signature."""
        backend = _Backend("WALL")
        self._vision(monkeypatch, backend)

        evidence = await render_mod._second_opinion(_Page(), _URL, _UNKNOWN_WALL, 200)

        assert evidence == "vision"
        assert backend.calls == 1

    @pytest.mark.asyncio
    async def test_a_page_the_model_calls_content_stays_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._vision(monkeypatch, _Backend("PAGE"))
        assert await render_mod._second_opinion(_Page(), _URL, _REAL_PAGE, 200) is None

    @pytest.mark.asyncio
    async def test_blank_waits_for_paint_and_asks_once_more(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A JS wall and a loading app shell are the same blank frame.

        The only way to separate them is to let one of them finish.
        """
        backend = _Backend("BLANK", "WALL")
        self._vision(monkeypatch, backend)
        page = _Page()

        evidence = await render_mod._second_opinion(page, _URL, _UNKNOWN_WALL, 200)

        assert evidence == "vision"
        assert backend.calls == 2
        assert page.shots == 2
        assert page.waited_ms == render_mod._REPAINT_WAIT_MS

    @pytest.mark.asyncio
    async def test_two_blanks_is_no_opinion(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """It never painted. Guessing here is what the abstention exists to avoid."""
        self._vision(monkeypatch, _Backend("BLANK", "BLANK"))
        assert await render_mod._second_opinion(_Page(), _URL, _UNKNOWN_WALL, 200) is None


class TestWithoutAnyModel:
    """The deployment with no GPU, no LM Studio, no [llm-agent] extra.

    On the current corpus this is indistinguishable from the full configuration:
    markup alone scores 6 of 6. What is lost is generalisation to unknown walls,
    not accuracy on known ones -- and nothing may break.
    """

    @pytest.mark.asyncio
    async def test_no_backend_falls_back_to_markup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _none() -> Any:
            return None

        monkeypatch.setattr(render_mod, "_vision_backend", _none)

        # Markup still catches everything it has a signature for...
        assert await render_mod._second_opinion(_Page(), _URL, _SIGNATURE_WALL, 200) == "cloudflare"
        # ...and simply cannot see the ones it does not.
        assert await render_mod._second_opinion(_Page(), _URL, _UNKNOWN_WALL, 200) is None

    @pytest.mark.asyncio
    async def test_a_screenshot_is_never_taken_without_a_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No model means no cost at all, not a wasted capture."""

        async def _none() -> Any:
            return None

        monkeypatch.setattr(render_mod, "_vision_backend", _none)
        page = _Page()
        await render_mod._second_opinion(page, _URL, _UNKNOWN_WALL, 200)
        assert page.shots == 0

    @pytest.mark.asyncio
    async def test_the_detection_can_be_switched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator may want the solver without paying for inference."""
        monkeypatch.setenv("SCRAPPER_TOOL_VISION_WALL_DETECT", "0")
        backend = _Backend("WALL")
        self_vision = backend

        async def _backend() -> Any:
            return self_vision

        monkeypatch.setattr(render_mod, "_vision_backend", _backend)
        assert await render_mod._second_opinion(_Page(), _URL, _UNKNOWN_WALL, 200) is None
        assert backend.calls == 0

    def test_it_is_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCRAPPER_TOOL_VISION_WALL_DETECT", raising=False)
        assert render_mod._vision_detection_enabled() is True


class TestTheSolverSeesTheVisionVerdict:
    """The coupling, closed for the third time and now structurally.

    A wall only the model can see must still reach the solver, or vision buys
    nothing but a better error message.
    """

    @pytest.mark.asyncio
    async def test_a_vision_only_wall_still_reaches_the_solver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called: list[str] = []

        async def fake_solve(page: Any, solver: Any, url: str, **kwargs: Any) -> bool:
            called.append(url)
            return True

        async def no_vision(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr("scrapper_tool.agent.backends.captcha_dom.solve_on_page", fake_solve)
        monkeypatch.setattr("scrapper_tool.agent.backends.llm.get_vision_backend", no_vision)

        class _P:
            async def content(self) -> str:
                return "<html><body>cleared</body></html>"

        out = await render_mod._try_clear_challenge(
            _P(), _URL, _UNKNOWN_WALL, 200, final_url=_URL, known_wall=True
        )

        assert called, "a wall only the model could see never reached the solver"
        assert out == "<html><body>cleared</body></html>"

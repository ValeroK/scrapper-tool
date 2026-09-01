"""A wall that defeats vocabulary and size in opposite directions.

The reported page carried no challenge words at all -- no "captcha", no "just a
moment", no "checking your browser" -- and its class names were per-deploy
gibberish. At ~29 KB it was also far too LARGE for every small-body gate, which
is the opposite of how an interstitial usually evades detection. And it arrived
with no redirect, so the URL comparison could not fire either.

It was therefore returned as a clean success, which is the worst outcome
available: a consumer records "walked it, found nothing" for a page it never saw,
and in a catalog that is indistinguishable from a genuinely empty category.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool._challenge import looks_like_host_titled_wall
from scrapper_tool.patterns import render as render_mod

_URL = "https://www.amayama.com/en/catalog/toyota/x"

#: The reported body, padded with inline script the way the real one was.
_WALL = (
    "<html><head><title>www.amayama.com</title></head>"
    '<body><div class="main-wrapper KfMSd3" role="main">'
    '<div class="main-content"><div class="YpeSi0">'
    '<img src="/favicon.ico" class="VNsDw9 TiPCY0" alt="Icon for www.amayama.com">'
    "<h1>www.amayama.com</h1>"
    "</div></div></div><script>" + ("var pad=1;" * 2800) + "</script></body></html>"
)


class TestTheReportedPage:
    def test_it_is_recognised(self) -> None:
        assert looks_like_host_titled_wall(_WALL, _URL)

    def test_it_is_big_enough_to_beat_every_small_body_gate(self) -> None:
        """Pins the property that made this evade detection.

        If someone later 'simplifies' this by adding a byte-size cap, this fails.
        """
        assert len(_WALL) > 25_000

    def test_it_contains_no_challenge_vocabulary(self) -> None:
        """The other half of why it evaded: nothing to match on."""
        lowered = _WALL.lower()
        for word in ("captcha", "just a moment", "checking your browser", "cf-chl"):
            assert word not in lowered

    def test_the_icon_alt_alone_is_enough(self) -> None:
        """Cloudflare states the host twice; either statement should do."""
        no_h1 = _WALL.replace("<h1>www.amayama.com</h1>", "")
        assert looks_like_host_titled_wall(no_h1, _URL)

    def test_the_heading_alone_is_enough(self) -> None:
        no_icon = _WALL.replace(
            '<img src="/favicon.ico" class="VNsDw9 TiPCY0" alt="Icon for www.amayama.com">', ""
        )
        assert looks_like_host_titled_wall(no_icon, _URL)


class TestItDoesNotFireOnRealPages:
    """Both halves are required, because either alone is legitimate."""

    def test_a_catalog_page_of_similar_size(self) -> None:
        real = (
            "<html><head><title>Toyota parts</title></head><body><h1>Front suspension</h1>"
            + ("<table><tr><td>48510-60J40</td><td>Shock absorber</td></tr></table>" * 300)
            + "</body></html>"
        )
        assert not looks_like_host_titled_wall(real, _URL)

    def test_a_page_whose_heading_is_the_host_but_which_says_things(self) -> None:
        """A parked domain or minimal landing page is allowed to name itself."""
        page = (
            "<html><body><h1>www.amayama.com</h1><p>"
            + ("Welcome to the catalog. " * 40)
            + "</p></body></html>"
        )
        assert not looks_like_host_titled_wall(page, _URL)

    def test_structured_data_vetoes_it_outright(self) -> None:
        """A document publishing schema.org asserts itself as content.

        No bot wall does that, so this is a hard veto rather than a signal.
        """
        page = (
            '<html><head><script type="application/ld+json">{"@type":"Product"}</script>'
            "</head><body><h1>www.amayama.com</h1></body></html>"
        )
        assert not looks_like_host_titled_wall(page, _URL)

    def test_an_unhydrated_spa_shell_is_not_a_wall(self) -> None:
        shell = (
            '<html><body><div id="root"></div><h1>Loading</h1><script>x=1</script></body></html>'
        )
        assert not looks_like_host_titled_wall(shell, _URL)

    def test_a_heading_naming_a_different_host(self) -> None:
        assert not looks_like_host_titled_wall(
            "<html><body><h1>example.org</h1></body></html>", _URL
        )

    @pytest.mark.parametrize(
        ("url", "heading"),
        [
            ("https://www.amayama.com/p", "amayama.com"),
            ("https://amayama.com/p", "www.amayama.com"),
            ("https://amayama.com/p", "AMAYAMA.COM"),
        ],
    )
    def test_www_and_case_do_not_defeat_it(self, url: str, heading: str) -> None:
        assert looks_like_host_titled_wall(f"<html><body><h1>{heading}</h1></body></html>", url)

    def test_missing_inputs_are_safe(self) -> None:
        assert not looks_like_host_titled_wall("", _URL)
        assert not looks_like_host_titled_wall(_WALL, "")


class TestItSwitchesTheSolverOn:
    """The coupling the reporter identified, and the reason this matters most.

    The captcha stack is gated on an already-detected challenge, so a wall the
    classifier cannot see never reaches the solver that might clear it. That is
    the second time one detector's blind spot has silently disabled the solver.
    """

    @pytest.mark.asyncio
    async def test_the_solver_is_invoked_on_a_host_titled_wall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool._challenge import is_interstitial, landed_on_challenge

        # Neither older detector can see it.
        assert is_interstitial(_WALL, 200) is None
        assert not landed_on_challenge(_URL, _URL, _WALL)

        called: list[str] = []

        async def fake_solve(page: Any, solver: Any, url: str, **kwargs: Any) -> bool:
            called.append(url)
            return True

        async def no_vision(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr("scrapper_tool.agent.backends.captcha_dom.solve_on_page", fake_solve)
        monkeypatch.setattr("scrapper_tool.agent.backends.llm.get_vision_backend", no_vision)

        class _Page:
            async def content(self) -> str:
                return "<html><body>cleared</body></html>"

        out = await render_mod._try_clear_challenge(_Page(), _URL, _WALL, 200, final_url=_URL)

        assert called, "a wall with no signature and no redirect never reached the solver"
        assert out == "<html><body>cleared</body></html>"

"""Unit tests for the local-VLM image-grid solver.

The parsing helpers are pinned against strings captured from *live* reCAPTCHA
challenges, because both bugs this module shipped with were parsing bugs that a
plausible-looking synthetic string would have hidden:

- the prompt renders as two lines ("Select all squares with" / "motorcycles"),
  so reading the first line reduced the target to ``"a"`` and asked the model to
  find nothing;
- the panel screenshot included the banner and the SKIP footer, so the tiles
  filled ~70% of the image the model was told was a 4x4 grid.

``solve_grid`` itself is driven with fakes so the orchestration — rounds, empty
replies, click ordering, and above all *honest failure* — is testable without a
browser or a GPU.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool.agent.backends import captcha_vision as cv
from scrapper_tool.errors import AgentLLMError


class TestPromptParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Captured live: innerText of .rc-imageselect-desc-no-canonical.
            ("Select all squares with\nmotorcycles", "Select all squares with motorcycles"),
            (
                "Select all images with\ncrosswalks\nClick verify once there are none left.",
                "Select all images with crosswalks",
            ),
            (
                "Select all squares with\nbuses\nIf there are none, click skip",
                "Select all squares with buses",
            ),
        ],
    )
    def test_prompt_lines_are_joined_not_truncated(self, raw: str, expected: str) -> None:
        assert cv._clean_prompt(raw) == expected

    @pytest.mark.parametrize(
        ("prompt", "target"),
        [
            ("Select all squares with\nmotorcycles", "motorcycles"),
            ("Select all images with crosswalks", "crosswalks"),
            ("Select all images with a bus", "bus"),
            ("Please click each image containing a motorbus", "motorbus"),
        ],
    )
    def test_target_extraction(self, prompt: str, target: str) -> None:
        assert cv.extract_target(prompt) == target

    def test_unparseable_prompt_falls_back_to_the_whole_text(self) -> None:
        """Better to over-describe than to hand the model an empty target."""
        assert cv.extract_target("Pick the odd one out") == "Pick the odd one out"


class TestTileReplyParsing:
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("6,7,8,11", [5, 6, 7, 10]),  # a real gemma-4 reply
            ("2,5,8", [1, 4, 7]),
            ("Tiles 1, 4 and 7 contain buses.", [0, 3, 6]),
            ("**3**, **9**", [2, 8]),
            ("NONE", []),
            ("", []),
        ],
    )
    def test_parses_real_and_chatty_replies(self, reply: str, expected: list[int]) -> None:
        assert cv.parse_tile_reply(reply, 16) == expected

    def test_out_of_range_numbers_are_dropped_not_clamped(self) -> None:
        """A model naming tile 12 of a 9-tile grid is confused, not off by a bit.

        Clamping would turn that confusion into a confident wrong click.
        """
        assert cv.parse_tile_reply("3, 12, 40", 9) == [2]

    def test_duplicates_collapse(self) -> None:
        assert cv.parse_tile_reply("4,4,4", 9) == [3]

    def test_none_alongside_digits_still_yields_the_digits(self) -> None:
        assert cv.parse_tile_reply("None of them except 5", 9) == [4]


class TestGridGeometry:
    @pytest.mark.parametrize(("tiles", "columns"), [(9, 3), (16, 4), (8, 4)])
    def test_columns_for(self, tiles: int, columns: int) -> None:
        assert cv._columns_for(tiles) == columns

    def test_prompt_states_the_grid_shape(self) -> None:
        challenge = cv.GridChallenge(prompt="p", target="buses", tile_count=16, screenshot_b64="x")
        text = cv._build_prompt(challenge, 4)
        assert "4x4 grid of 16 photo tiles" in text
        assert "buses" in text
        assert "1 to 16" in text


class _FakeFrame:
    def __init__(self, url: str = "https://www.google.com/recaptcha/api2/bframe?k=x") -> None:
        self.url = url


class TestChallengeFrame:
    def test_bframe_is_the_challenge_frame_not_the_anchor(self) -> None:
        """The mirror image of ``captcha_dom.find_anchor_frame``.

        Picking the anchor here would screenshot the checkbox, not the grid.
        """
        anchor = _FakeFrame("https://www.google.com/recaptcha/api2/anchor?k=x")
        bframe = _FakeFrame("https://www.google.com/recaptcha/api2/bframe?k=x")
        page = type("P", (), {"frames": [anchor, bframe]})()
        assert cv.find_challenge_frame(page, "recaptcha-v2") is bframe

    def test_no_frame_for_kinds_without_a_grid(self) -> None:
        page = type("P", (), {"frames": [_FakeFrame()]})()
        assert cv.find_challenge_frame(page, "turnstile") is None


# --- solve_grid orchestration ---------------------------------------------


class _FakeLLM:
    """Returns queued replies; records the prompts it was given."""

    name = "fake"
    model = "fake"

    def __init__(self, replies: list[str] | Exception) -> None:
        self._replies = replies
        self.prompts: list[str] = []
        self.max_tokens: list[int] = []

    async def complete_vision(
        self, prompt: str, images_b64: Any, *, max_tokens: int = 0, temperature: float = 0.0
    ) -> str:
        if isinstance(self._replies, Exception):
            raise self._replies
        self.prompts.append(prompt)
        self.max_tokens.append(max_tokens)
        return self._replies.pop(0) if self._replies else "NONE"


class _GridPage:
    """Minimal page+frame fake exposing just what solve_grid touches."""

    def __init__(
        self,
        *,
        challenges: list[cv.GridChallenge | None],
        tokens: list[str],
        tile_count: int = 9,
    ) -> None:
        self.frames = [_FakeFrame()]
        self._challenges = challenges
        self._tokens = tokens
        self.tile_count = tile_count
        self.clicked: list[int] = []
        self.verified = 0

    async def wait_for_timeout(self, _ms: float) -> None: ...

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        from scrapper_tool.agent.backends import captcha_dom

        if js is captcha_dom._RESPONSE_FIELD_JS:
            return self._tokens.pop(0) if self._tokens else ""
        return None

    def next_challenge(self) -> cv.GridChallenge | None:
        return self._challenges.pop(0) if self._challenges else None


def _install_page_fakes(monkeypatch: pytest.MonkeyPatch, page: _GridPage) -> None:
    async def fake_read_challenge(frame: Any, kind: str) -> cv.GridChallenge | None:
        return page.next_challenge()

    async def fake_click_tiles(frame: Any, selector: str, indices: list[int]) -> bool:
        page.clicked.extend(indices)
        return True

    async def fake_click_verify(frame: Any, selector: str) -> bool:
        page.verified += 1
        return True

    monkeypatch.setattr(cv, "read_challenge", fake_read_challenge)
    monkeypatch.setattr(cv, "_click_tiles", fake_click_tiles)
    monkeypatch.setattr(cv, "_click_verify", fake_click_verify)


def _challenge(tiles: int = 9) -> cv.GridChallenge:
    return cv.GridChallenge(
        prompt="Select all squares with buses",
        target="buses",
        tile_count=tiles,
        screenshot_b64="AAA",
    )


@pytest.mark.asyncio
async def test_solves_when_the_answer_mints_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _GridPage(challenges=[_challenge()], tokens=["03AGdBq26..."])
    _install_page_fakes(monkeypatch, page)
    llm = _FakeLLM(["1,5,9"])
    assert await cv.solve_grid(page, "recaptcha-v2", llm, settle_s=1) is True
    assert page.clicked == [0, 4, 8]
    assert page.verified == 1


@pytest.mark.asyncio
async def test_grid_solve_gets_a_generous_token_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured, not stylistic: at 512 tokens gemma-4 returned no answer at all.

    A reasoning model thinks proportionally to how much there is to look at, and
    sixteen photographs is a lot; starving it reads as a solver failure.
    """
    page = _GridPage(challenges=[_challenge(16)], tokens=["tok"])
    _install_page_fakes(monkeypatch, page)
    llm = _FakeLLM(["1"])
    await cv.solve_grid(page, "recaptcha-v2", llm, settle_s=1)
    assert llm.max_tokens[0] >= 2048


@pytest.mark.asyncio
async def test_unsolved_after_max_rounds_reports_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property that keeps the cascade honest.

    A hopeful True would stop the cascade on an unsolved challenge — the exact
    bug just fixed in ``solve_on_page``.
    """
    page = _GridPage(challenges=[_challenge(), _challenge(), _challenge()], tokens=[])
    _install_page_fakes(monkeypatch, page)
    llm = _FakeLLM(["1,2", "3,4", "5,6"])
    assert await cv.solve_grid(page, "recaptcha-v2", llm, settle_s=1, max_rounds=3) is False
    assert page.verified == 3


@pytest.mark.asyncio
async def test_repeated_empty_replies_stop_early(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two nothing-selected rounds means we are not converging — hand over."""
    page = _GridPage(challenges=[_challenge()] * 5, tokens=[])
    _install_page_fakes(monkeypatch, page)
    llm = _FakeLLM(["NONE", "NONE", "NONE", "NONE", "NONE"])
    assert await cv.solve_grid(page, "recaptcha-v2", llm, settle_s=1, max_rounds=5) is False
    # Stopped at the empty-round cap rather than burning all five rounds.
    assert len(llm.prompts) == cv._MAX_EMPTY_ROUNDS


@pytest.mark.asyncio
async def test_llm_failure_is_not_raised_to_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable or unloadable model must escalate, not crash the agent loop.

    Measured for real: ``qwen/qwen3.6-27b`` returns HTTP 400 "insufficient system
    resources" on a machine that cannot hold it.
    """
    page = _GridPage(challenges=[_challenge()], tokens=[])
    _install_page_fakes(monkeypatch, page)
    llm = _FakeLLM(AgentLLMError("Vision call returned HTTP 400: insufficient system resources"))
    assert await cv.solve_grid(page, "recaptcha-v2", llm, settle_s=1) is False


@pytest.mark.asyncio
async def test_no_grid_present_checks_for_an_existing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vanished grid means either "already solved" or "never appeared"."""
    page = _GridPage(challenges=[None], tokens=["03AGdBq26..."])
    _install_page_fakes(monkeypatch, page)
    assert await cv.solve_grid(page, "recaptcha-v2", _FakeLLM([]), settle_s=1) is True


@pytest.mark.asyncio
async def test_unsupported_kind_is_declined_immediately() -> None:
    """Turnstile and reCAPTCHA v3 have no puzzle to look at."""
    llm = _FakeLLM(["1"])
    page = _GridPage(challenges=[], tokens=[])
    for kind in ("turnstile", "recaptcha-v3", "aws-waf", "datadome"):
        assert await cv.solve_grid(page, kind, llm, settle_s=1) is False
    assert llm.prompts == []

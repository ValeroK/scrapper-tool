"""Unit tests for the slider-captcha solver.

Grounded on three canvases captured from a **live** GeeTest v3 challenge
(``tests/fixtures/slider/``), because every wrong turn taken while building this
looked perfectly reasonable against synthetic images:

- correlating the piece's texture against the background scored a random-noise
  "piece" at 0.57 and always returned the last valid offset;
- a smooth synthetic gradient made a border-pair heuristic look solid while it
  false-positived on backgrounds with no notch at all;
- the piece canvas turned out to be the *same size* as the background, so a
  canvas-width sanity check rejected every real input.

The known-good answer for these fixtures is a gap at x=178 with a 41px piece,
established by diffing the notched and un-notched backgrounds.
"""

from __future__ import annotations

import io
import itertools
import random
from pathlib import Path

import pytest

from scrapper_tool.agent.backends import captcha_slider as cs

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "slider"
_GAP_X = 178
_PIECE_WIDTH = 41


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


@pytest.fixture(scope="module")
def real() -> dict[str, bytes]:
    return {
        "bg": _fixture("geetest_bg.png"),
        "fullbg": _fixture("geetest_fullbg.png"),
        "piece": _fixture("geetest_slice.png"),
    }


class TestPieceBounds:
    def test_piece_is_measured_by_alpha_not_canvas_width(self, real: dict[str, bytes]) -> None:
        """The slice canvas is background-sized and mostly transparent.

        Treating its width as the piece width made it equal the background width,
        which every sanity check then rejected — so the solver declined every
        real challenge before doing any work.
        """
        bounds = cs.piece_bounds(real["piece"])
        assert bounds == (0, 40)
        assert bounds[1] - bounds[0] + 1 == _PIECE_WIDTH

    def test_fully_transparent_piece_yields_nothing(self) -> None:
        import numpy as np
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(np.zeros((20, 20, 4), dtype="uint8"), "RGBA").save(buf, "PNG")
        assert cs.piece_bounds(buf.getvalue()) is None

    def test_undecodable_input_is_declined(self) -> None:
        assert cs.piece_bounds(b"not an image") is None


class TestGapFromFullBackground:
    def test_exact_on_real_canvases(self, real: dict[str, bytes]) -> None:
        """The primary method: where the two backgrounds differ IS the notch.

        No threshold tuning, no dependence on how busy the photo is.
        """
        match = cs.gap_from_full_background(real["bg"], real["fullbg"])
        assert match is not None
        assert match.x == _GAP_X
        assert match.confidence == 1.0

    def test_identical_images_have_no_gap(self, real: dict[str, bytes]) -> None:
        assert cs.gap_from_full_background(real["bg"], real["bg"]) is None

    def test_mismatched_sizes_are_declined(self, real: dict[str, bytes]) -> None:
        import numpy as np
        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(np.zeros((10, 10), dtype="uint8"), "L").save(buf, "PNG")
        assert cs.gap_from_full_background(real["bg"], buf.getvalue()) is None


class TestDetectGapOffset:
    def test_border_fallback_agrees_with_the_exact_method(self, real: dict[str, bytes]) -> None:
        """The fallback for when no un-notched background is available.

        It must land on the same answer the diff gives, or it is not a fallback.
        """
        match = cs.detect_gap_offset(real["bg"], real["piece"])
        assert match is not None
        assert abs(match.x - _GAP_X) <= 2
        assert match.confidence >= cs._MIN_CONFIDENCE

    def test_undecodable_background_is_declined(self, real: dict[str, bytes]) -> None:
        assert cs.detect_gap_offset(b"nope", real["piece"]) is None


class TestHumanDragPath:
    """The endpoint is not the whole test — these products score the motion."""

    def test_ends_exactly_on_target(self) -> None:
        path = cs.human_drag_path(180, rng=random.Random(1))
        assert path[-1].x == 180

    def test_overshoots_then_corrects(self) -> None:
        """A hand overshoots and comes back; a script stops dead on the pixel."""
        path = cs.human_drag_path(180, rng=random.Random(1))
        assert max(step.x for step in path) > 180

    def test_velocity_is_not_constant(self) -> None:
        """Constant velocity is itself the bot signal."""
        path = cs.human_drag_path(200, rng=random.Random(2))
        deltas = [b.x - a.x for a, b in itertools.pairwise(path)]
        assert max(deltas) > 2 * min(d for d in deltas if d > 0)

    def test_drifts_vertically(self) -> None:
        path = cs.human_drag_path(180, rng=random.Random(3))
        assert any(abs(step.y) > 0.2 for step in path)

    def test_deterministic_for_a_given_seed(self) -> None:
        a = cs.human_drag_path(150, rng=random.Random(9))
        b = cs.human_drag_path(150, rng=random.Random(9))
        assert a == b

    @pytest.mark.parametrize("distance", [0, -5])
    def test_no_path_for_nonpositive_distance(self, distance: float) -> None:
        assert cs.human_drag_path(distance, rng=random.Random(0)) == []

    def test_step_count_scales_with_distance_but_stays_bounded(self) -> None:
        short = cs.human_drag_path(20, rng=random.Random(4))
        long = cs.human_drag_path(600, rng=random.Random(4))
        assert len(short) < len(long) <= 50


class TestSolveSliderGuards:
    @pytest.mark.asyncio
    async def test_unsupported_kind_declines(self) -> None:
        assert await cs.solve_slider(object(), "recaptcha-v2") is False

    @pytest.mark.asyncio
    async def test_missing_images_decline_without_dragging(self) -> None:
        """Declining is the point: a wrong drag costs a retry AND feeds the
        site's scorer a failed trajectory."""

        class _Page:
            frames: list[object] = []

            async def evaluate(self, js: str, arg: object = None) -> dict[str, str]:
                return {}

            async def query_selector(self, selector: str) -> None:
                return None

        assert await cs.solve_slider(_Page(), "geetest") is False

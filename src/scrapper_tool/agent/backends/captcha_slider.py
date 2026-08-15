"""Slider-captcha solver for GeeTest and DataDome — classic CV, no model.

A slider captcha is a gap-alignment puzzle: a background image with a
puzzle-piece-shaped notch cut out of it, and the piece itself, which you drag
horizontally until it lines up. That is a template-matching problem with an exact
answer, so it needs no VLM at all — and unlike the image-grid solvers it is not
accuracy-bound by whatever model happens to fit in local VRAM.

**Why a 1-D profile rather than 2-D template matching.** The piece only ever
moves horizontally, so the y offset is fixed and the whole problem collapses to
"which x aligns the two edge patterns". Summing the edge magnitude down each
column turns both images into a 1-D signal, and correlating those is both far
faster than sliding a 2-D template and *more* robust: averaging down the column
suppresses the per-pixel photographic noise that trips 2-D matchers on the
detailed nature photos these captchas use.

**Getting the position right is only half of it.** A drag that moves at constant
velocity in a straight line is itself a bot signal — these products score the
trajectory, not just the endpoint. :func:`human_drag_path` therefore produces
accelerate/decelerate motion with overshoot, correction and jitter.

Numpy and Pillow only: no OpenCV. Both are already present wherever the agent
extras are, and neither is a core dependency this module can assume beyond that.
"""

from __future__ import annotations

import base64
import io
import math
import random
from typing import TYPE_CHECKING, Any, NamedTuple

from scrapper_tool._logging import get_logger

if TYPE_CHECKING:
    from scrapper_tool.agent.backends.captcha import CaptchaKind

_logger = get_logger(__name__)

SUPPORTED_KINDS = frozenset({"geetest", "datadome"})

# Correlation below this means the piece was not found — the images were
# unrelated, or one of them failed to load. Better to decline than to drag to a
# confidently wrong position, which burns the attempt and trains the site's
# scorer on a failed trajectory.
_MIN_CONFIDENCE = 0.30
# The piece never starts at x=0 on screen, but its own silhouette sits at the
# left edge of the piece image; ignore matches inside that band or the solver
# "finds" the piece against itself and never moves.
_MIN_GAP_X = 12
# Both borders this many times above the photo's own 90th-percentile edge energy
# counts as full confidence. Tuned so an ordinary busy photo does not reach it.
_CONFIDENCE_SCALE = 2.0
# Columns differing by more than this fraction of the peak count as part of the
# notch. Loose on purpose: JPEG ringing spreads a little energy either side.
_DIFF_COLUMN_FRACTION = 0.35
# Anything below this is a broken measurement, not a real zoom level; ignore it
# rather than shrink the drag to nothing.
_MIN_PLAUSIBLE_SCALE = 0.5
# Poll for the canvases to actually paint. ~4 s total, which is well inside the
# window these widgets stay open and cheap when the puzzle is already there.
_READY_POLLS = 9
_READY_POLL_S = 0.5


class GapMatch(NamedTuple):
    """Where the gap is, and how much to trust it."""

    x: int
    """Offset in background-image pixels from its left edge."""
    confidence: float
    """Peak normalised correlation, 0..1."""


def _to_gray(png_bytes: bytes) -> Any:
    """Decode a PNG/JPEG to a 2-D float array of luminance."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    with Image.open(io.BytesIO(png_bytes)) as img:
        return np.asarray(img.convert("L"), dtype=np.float64)


def _edge_profile(gray: Any) -> Any:
    """Collapse an image to a 1-D per-column edge-energy signal.

    The horizontal gradient is what matters: the gap's left and right borders are
    vertical discontinuities, and the piece's silhouette is the same shape.
    """
    import numpy as np  # noqa: PLC0415

    if gray.shape[1] < 3:  # noqa: PLR2004 — need a left and right neighbour
        return np.zeros(gray.shape[1], dtype=np.float64)
    horizontal = np.zeros_like(gray)
    horizontal[:, 1:-1] = np.abs(gray[:, 2:] - gray[:, :-2])
    return horizontal.sum(axis=0)


def piece_bounds(piece: bytes) -> tuple[int, int] | None:
    """Horizontal extent of the actual puzzle piece inside its canvas.

    GeeTest's slice canvas is the **same size as the background** (260x160
    measured live) and almost entirely transparent, with the piece drawn at its
    current slider position. Taking the canvas width as the piece width is
    therefore wrong by a factor of six, and — since it then equals the background
    width — makes every sanity check reject the input outright. The alpha channel
    is what actually delimits the piece.

    Returns ``(x0, x1)`` inclusive, or ``None`` if the image has no alpha or is
    fully transparent.
    """
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    try:
        with Image.open(io.BytesIO(piece)) as img:
            alpha = np.asarray(img.convert("RGBA"))[..., 3]
    except Exception as exc:
        _logger.debug("agent.captcha_slider.piece_decode_failed", error=str(exc))
        return None
    columns = np.where(alpha.sum(axis=0) > 0)[0]
    if columns.size == 0:
        return None
    return int(columns.min()), int(columns.max())


def gap_from_full_background(background: bytes, full_background: bytes) -> GapMatch | None:
    """Exact gap position by diffing the notched and un-notched backgrounds.

    GeeTest v3 ships both: `geetest_canvas_bg` has the notch, `geetest_canvas_fullbg`
    is the same photo intact. It renders at 0x0 so it is invisible, but the canvas
    still holds pixels and `toDataURL` reads them. Where they differ IS the notch —
    no heuristics, no threshold, no photo-dependent tuning. Verified against a live
    challenge: the differing columns were exactly 178-218, a 41px span matching the
    piece's own 41px alpha width.

    Always preferred over :func:`detect_gap_offset` when the full background is
    obtainable.
    """
    import numpy as np  # noqa: PLC0415

    try:
        notched = _to_gray(background)
        intact = _to_gray(full_background)
    except Exception as exc:
        _logger.debug("agent.captcha_slider.decode_failed", error=str(exc))
        return None
    if notched.shape != intact.shape:
        return None

    columns = np.abs(notched - intact).sum(axis=0)
    peak = float(columns.max())
    if peak <= 0:
        return None
    differing = np.where(columns > peak * _DIFF_COLUMN_FRACTION)[0]
    if differing.size == 0:
        return None
    return GapMatch(x=int(differing.min()), confidence=1.0)


def detect_gap_offset(  # noqa: PLR0911 — one guard per way the input can be unusable
    background: bytes, piece: bytes
) -> GapMatch | None:
    """Locate the notch in ``background`` that ``piece`` fits into.

    The fallback for when the un-notched background is not available; prefer
    :func:`gap_from_full_background`, which is exact.

    Finds the notch by its **borders**, not by matching the piece's texture. The
    obvious approach — correlate the piece image against the background — does
    not work and measurably picks boundary artefacts instead: the piece is a crop
    of the very photo it sits in, so its texture correlates about equally well
    everywhere, and a random-noise "piece" scored 0.57 against a real background.

    What is actually distinctive is that cutting the notch out leaves two hard
    vertical edges exactly ``piece_width`` apart, against a photo whose own edges
    are diffuse. Scoring that pair is both discriminative and needs nothing from
    the piece image except its width.

    Returns ``None`` when the images cannot be decoded, the piece is wider than
    the background, or the peak is too weak to act on.
    """
    import numpy as np  # noqa: PLC0415

    try:
        bg_gray = _to_gray(background)
    except Exception as exc:
        _logger.debug("agent.captcha_slider.decode_failed", error=str(exc))
        return None

    bounds = piece_bounds(piece)
    if bounds is None:
        try:
            width = int(_to_gray(piece).shape[1])
        except Exception:
            return None
    else:
        width = bounds[1] - bounds[0] + 1
    if width < 2 or width >= bg_gray.shape[1]:  # noqa: PLR2004
        return None

    profile = _edge_profile(bg_gray)
    # The first and last columns have no two-sided neighbour, so their gradient
    # is structurally zero. Left in, that fabricates a "border" at each end and
    # the search reliably lands on the last valid offset.
    interior = profile[1:-1]
    if interior.size <= width:
        return None

    # Normalise against the image's own edge energy so the threshold means the
    # same thing on a busy photo and a plain one.
    baseline = float(np.median(interior))
    spread = float(np.percentile(interior, 90) - baseline)
    if spread <= 0:
        return None
    strength = (interior - baseline) / spread

    # Score every offset by how strong BOTH notch borders are. The min of the
    # pair, not the sum: one strong edge is just a feature of the photo, whereas
    # two strong edges the piece's width apart is the gap.
    left = strength[: strength.size - width]
    right = strength[width:]
    paired = np.minimum(left, right)

    # Border strength alone is not enough. A photo with any periodic structure
    # supplies plenty of strong-edge pairs at arbitrary separations — a synthetic
    # background with no notch at all scored 0.74 on borders alone. What makes a
    # notch a notch is that the span BETWEEN the borders has been flattened or
    # darkened, so its interior edge energy drops. Subtracting the interior mean
    # rewards "two hard rims around a quiet middle" and penalises "busy region
    # that happens to have edges at both ends".
    window = np.ones(width, dtype=np.float64) / width
    interior_mean = np.convolve(strength, window, mode="valid")[: paired.size]
    paired = paired - interior_mean

    searchable = paired.copy()
    searchable[:_MIN_GAP_X] = -np.inf
    if not np.isfinite(searchable).any():
        return None

    best = int(np.argmax(searchable))
    confidence = float(np.clip(searchable[best] / _CONFIDENCE_SCALE, 0.0, 1.0))
    # +1 undoes the interior slice above.
    best_x = best + 1
    if confidence < _MIN_CONFIDENCE:
        _logger.info(
            "agent.captcha_slider.low_confidence", confidence=round(confidence, 3), x=best_x
        )
        return None
    return GapMatch(x=best_x, confidence=confidence)


class DragStep(NamedTuple):
    x: float
    y: float
    delay_s: float


def human_drag_path(
    distance: float, *, rng: random.Random | None = None, steps: int = 0
) -> list[DragStep]:
    """A humanlike trajectory from 0 to ``distance``, as cumulative offsets.

    Endpoint accuracy is not the whole test: GeeTest and DataDome both score the
    *motion*. A linear, evenly-timed sweep to the exact pixel is a stronger bot
    signal than a slightly imprecise human one, so this produces:

    - an ease-in/ease-out velocity curve rather than constant speed,
    - a small overshoot past the target followed by a correction back, which is
      what a hand does when it stops,
    - sub-pixel vertical drift, because a real drag is never perfectly level,
    - jittered per-step delays.

    ``rng`` is injectable so tests are deterministic; production passes ``None``
    and gets a fresh unseeded generator.
    """
    rng = rng or random.Random()  # noqa: S311 — trajectory realism, not cryptography
    if distance <= 0:
        return []
    if steps <= 0:
        # More steps for a longer drag, but bounded: a 400 px sweep in 8 events
        # is robotic, and one in 400 is a stall.
        steps = max(12, min(45, int(distance / 6)))

    overshoot = min(rng.uniform(3.0, 9.0), max(1.0, distance * 0.06))
    peak = distance + overshoot

    path: list[DragStep] = []
    for index in range(1, steps + 1):
        # Smoothstep: slow start, fast middle, slow finish.
        t = index / steps
        eased = t * t * (3.0 - 2.0 * t)
        x = eased * peak + rng.uniform(-0.6, 0.6)
        y = math.sin(t * math.pi) * rng.uniform(0.4, 1.8) + rng.uniform(-0.4, 0.4)
        path.append(DragStep(x=x, y=y, delay_s=rng.uniform(0.008, 0.028)))

    # Correction back from the overshoot, slower than the approach.
    corrections = rng.randint(2, 4)
    for index in range(1, corrections + 1):
        t = index / corrections
        x = peak - overshoot * t + rng.uniform(-0.3, 0.3)
        path.append(DragStep(x=x, y=rng.uniform(-0.4, 0.4), delay_s=rng.uniform(0.03, 0.09)))

    # Land exactly, then pause as a hand does before releasing.
    path.append(DragStep(x=distance, y=0.0, delay_s=rng.uniform(0.12, 0.3)))
    return path


# Per-product DOM contract, same convention as the grid solver.
_SLIDER_SELECTORS: dict[str, dict[str, str]] = {
    "geetest": {
        "background": ".geetest_bg, .geetest_canvas_bg, canvas.geetest_canvas_bg",
        "piece": ".geetest_slice, .geetest_canvas_slice, canvas.geetest_canvas_slice",
        # Order matters: `.geetest_btn` is the RADAR button ("Click to verify"),
        # not the drag handle, and it matched first — measured live as a 300px-wide
        # box against the real handle's 66px, so every drag began from the middle
        # of the wrong element and nothing was ever accepted.
        "handle": ".geetest_slider_button, .geetest_slider .geetest_arrow, .geetest_arrow",
    },
    "datadome": {
        "background": ".sliderContainer img, #captcha__puzzle canvas, .captcha__puzzle",
        "piece": ".slider img, #captcha__puzzle .slider",
        "handle": ".slider, .sliderContainer .slider, #ddv1-captcha-container .slider",
    },
}


# Read the slider canvases straight out of the page. Screenshotting will not do:
# `geetest_canvas_fullbg` renders at 0x0 so it has no box to capture, yet the
# canvas still holds the un-notched photo that makes the exact diff possible.
# Verified live that these canvases are NOT tainted, so toDataURL succeeds.
_CANVAS_GRAB_JS = r"""
() => {
  const out = {};
  for (const c of document.querySelectorAll('canvas')) {
    const cls = (c.className || '').toString();
    const key = cls.indexOf('fullbg') !== -1 ? 'fullbg'
              : cls.indexOf('slice') !== -1 ? 'piece'
              : cls.indexOf('_bg') !== -1 ? 'background'
              : null;
    if (!key || out[key]) continue;
    try { out[key] = c.toDataURL('image/png'); } catch (e) { /* tainted */ }
    if (key === 'background') {
      // The gap is measured in canvas pixels but the drag happens in CSS
      // pixels, and these differ: measured live at 260 intrinsic vs 258
      // displayed. Small, but a slider is scored on pixel alignment.
      const rect = c.getBoundingClientRect();
      if (c.width > 0 && rect.width > 0) out.scale = String(rect.width / c.width);
    }
  }
  return out;
}
"""


def _decode_data_url(value: Any) -> bytes | None:
    if not isinstance(value, str) or "," not in value:
        return None
    try:
        return base64.b64decode(value.split(",", 1)[1])
    except Exception:
        return None


async def _canvas_images(frame: Any) -> dict[str, bytes]:
    """Extract background / fullbg / piece from the page's canvases."""
    evaluate = getattr(frame, "evaluate", None)
    if not callable(evaluate):
        return {}
    try:
        raw = await evaluate(_CANVAS_GRAB_JS)
    except Exception as exc:
        _logger.debug("agent.captcha_slider.canvas_grab_failed", error=str(exc))
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bytes] = {}
    for key, value in raw.items():
        if key == "scale":
            continue
        decoded = _decode_data_url(value)
        if decoded:
            out[str(key)] = decoded
    try:
        out["__scale__"] = str(float(raw.get("scale", 1.0))).encode()
    except (TypeError, ValueError):
        out["__scale__"] = b"1.0"
    return out


async def _element_png(frame: Any, selector: str) -> bytes | None:
    """Screenshot the first matching element. ``None`` if absent or unshootable."""
    try:
        element = await frame.query_selector(selector)
        if element is None:
            return None
        return bytes(await element.screenshot())
    except Exception as exc:
        _logger.debug("agent.captcha_slider.shot_failed", selector=selector, error=str(exc))
        return None


async def _await_puzzle(
    page: Any, target: Any, selectors: dict[str, str]
) -> tuple[dict[str, bytes], bytes, GapMatch] | None:
    """Poll until the slider canvases are painted, then locate the gap.

    The canvas ELEMENTS appear before they are drawn into, and reading them in
    that window is indistinguishable from "there is no puzzle": the piece canvas
    is still fully transparent and the background still equals fullbg, so both
    detectors correctly return None and the solver declines a puzzle that was
    about to exist.

    Measured directly — three consecutive attempts on the same target gave
    ``piece=None, gap=None`` twice and a clean ``GapMatch(x=149, confidence=1.0)``
    once. That race, not the detection maths, is what made the live success rate
    swing between runs.
    """
    for attempt in range(_READY_POLLS):
        if attempt:
            waiter = getattr(page, "wait_for_timeout", None)
            if callable(waiter):
                await waiter(_READY_POLL_S * 1000.0)

        canvases = await _canvas_images(target)
        background = canvases.get("background") or await _element_png(
            target, selectors["background"]
        )
        piece = canvases.get("piece") or await _element_png(target, selectors["piece"])
        if background is None or piece is None:
            continue

        match = None
        full_background = canvases.get("fullbg")
        if full_background is not None:
            # Exact, and independent of how busy the photo is.
            match = gap_from_full_background(background, full_background)
        if match is None:
            match = detect_gap_offset(background, piece)
        if match is not None:
            return canvases, piece, match
    return None


async def solve_slider(
    page: Any,
    kind: CaptchaKind,
    frame: Any | None = None,
    *,
    rng: random.Random | None = None,
) -> bool:
    """Align and drag a slider captcha. Returns whether the drag was performed.

    ``True`` means "we found the gap and dragged to it", not "the site accepted
    it" — acceptance shows up as the challenge clearing, which the caller checks.
    Returns ``False`` without dragging whenever the gap cannot be located
    confidently, so a wrong drag is never attempted: a failed attempt costs a
    retry *and* feeds the site's scorer a bad trajectory.
    """
    if kind not in SUPPORTED_KINDS:
        return False
    selectors = _SLIDER_SELECTORS[kind]
    target = frame if frame is not None else page

    ready = await _await_puzzle(page, target, selectors)
    if ready is None:
        _logger.debug("agent.captcha_slider.puzzle_never_ready", kind=kind)
        return False
    canvases, piece, match = ready

    # The piece does not necessarily start at x=0, and the drag distance is the
    # difference, not the gap's absolute position.
    bounds = piece_bounds(piece)
    distance = match.x - (bounds[0] if bounds else 0)
    # Convert canvas pixels to CSS pixels before driving the mouse.
    try:
        scale = float(canvases.get("__scale__", b"1.0").decode())
    except (ValueError, AttributeError):
        scale = 1.0
    if _MIN_PLAUSIBLE_SCALE < scale != 1.0:
        distance = round(distance * scale)
    if distance <= 0:
        _logger.info("agent.captcha_slider.nonpositive_distance", kind=kind, x=match.x)
        return False
    _logger.info(
        "agent.captcha_slider.gap_found",
        kind=kind,
        x=match.x,
        distance=distance,
        confidence=round(match.confidence, 3),
        exact="fullbg" in canvases,
    )

    handle = None
    try:
        handle = await target.query_selector(selectors["handle"])
    except Exception as exc:
        _logger.debug("agent.captcha_slider.handle_query_failed", error=str(exc))
    if handle is None:
        _logger.debug("agent.captcha_slider.no_handle", kind=kind)
        return False

    return await _drag(page, handle, distance, rng=rng)


async def _drag(page: Any, handle: Any, distance: int, *, rng: random.Random | None) -> bool:
    """Press the handle, follow a humanlike path, release."""
    mouse = getattr(page, "mouse", None)
    if mouse is None:
        return False
    try:
        box = await handle.bounding_box()
        if not box:
            return False
        start_x = box["x"] + box["width"] / 2
        start_y = box["y"] + box["height"] / 2

        await mouse.move(start_x, start_y)
        await mouse.down()
        for step in human_drag_path(distance, rng=rng):
            await mouse.move(start_x + step.x, start_y + step.y)
            waiter = getattr(page, "wait_for_timeout", None)
            if callable(waiter):
                await waiter(step.delay_s * 1000.0)
        await mouse.up()
    except Exception as exc:
        _logger.warning("agent.captcha_slider.drag_failed", error=str(exc))
        return False
    return True


def _b64(png_bytes: bytes) -> str:  # pragma: no cover — debugging aid
    return base64.b64encode(png_bytes).decode("ascii")


__all__ = [
    "SUPPORTED_KINDS",
    "DragStep",
    "GapMatch",
    "detect_gap_offset",
    "gap_from_full_background",
    "human_drag_path",
    "piece_bounds",
    "solve_slider",
]

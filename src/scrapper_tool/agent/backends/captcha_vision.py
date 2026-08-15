"""Local-VLM image-grid solver for reCAPTCHA v2 and hCaptcha.

The free tier between :func:`~scrapper_tool.agent.backends.captcha_dom.click_checkbox`
and the paid solvers. When the checkbox is not accepted outright, both products
fall back to "select every image containing X" — a visual question, which is
exactly what a local vision model can answer at no cost and without shipping the
page to a third party.

**Screenshot the grid, don't parse it.** reCAPTCHA serves 3x3 challenges as one
image sliced across nine ``<td>``s by CSS, 4x4 as sixteen slices of one image,
and dynamic variants swap individual tiles in afterwards; hCaptcha uses separate
per-tile images. Reconstructing any of that is work that breaks whenever the
markup moves. A screenshot of the grid element is identical for all of them, so
the model sees what a human sees and the only page-specific knowledge left is
"where do I click".

**Scope, honestly.** This targets the *image-grid* challenges of reCAPTCHA v2 and
hCaptcha and nothing else. Turnstile and reCAPTCHA v3 have no puzzle to look at —
they are attestation and risk scores. FunCaptcha's rotating 3D objects are a
different and much harder visual task. AWS WAF is proof-of-work. Those stay with
the paid tier, and a failure here returns ``False`` so the cascade escalates
rather than reporting a solve that did not happen.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Any, NamedTuple

from scrapper_tool._logging import get_logger
from scrapper_tool.agent.backends.captcha_dom import read_response_token
from scrapper_tool.errors import AgentLLMError

if TYPE_CHECKING:
    from scrapper_tool.agent.backends.captcha import CaptchaKind
    from scrapper_tool.agent.backends.llm import LLMBackend

_logger = get_logger(__name__)

# reCAPTCHA renders the challenge in a second iframe ("bframe"), separate from
# the checkbox one ("anchor"). hCaptcha uses a frame whose URL carries `frame=c`.
_CHALLENGE_FRAME_PATTERNS: dict[str, tuple[str, ...]] = {
    "recaptcha-v2": ("recaptcha/api2/bframe", "recaptcha/enterprise/bframe"),
    "hcaptcha": ("hcaptcha.com/captcha", "newassets.hcaptcha.com"),
}

# Per-product DOM contract. Kept as data so a markup change is a one-line edit
# rather than a hunt through the logic.
_GRID_SELECTORS: dict[str, dict[str, str]] = {
    "recaptcha-v2": {
        "root": "#rc-imageselect",
        # Screenshot the TABLE, not the panel. `#rc-imageselect` also contains the
        # instruction banner and the reload/audio/SKIP footer, so the 16 tiles
        # cover only the middle ~70% of that image — and the model, told "this is
        # a 4x4 grid numbered 1-16", has to work out where the grid even starts
        # before it can number anything. Framing the table exactly removes that
        # guess entirely.
        "grid": "#rc-imageselect-target table, .rc-imageselect-table-33, "
        ".rc-imageselect-table-44, .rc-imageselect-table-42",
        "prompt": ".rc-imageselect-desc-no-canonical, .rc-imageselect-desc",
        # The subject sits in its own <strong> inside the prompt — captured live:
        # ``Select all squares with <strong>motorcycles</strong>``. Reading the
        # element is exact; regexing the joined innerText is not, because the
        # element renders as two lines ("Select all squares with" / "motorcycles")
        # and any line-based reading drops one half of the sentence.
        "subject": "strong",
        "tile": "td.rc-imageselect-tile",
        "verify": "#recaptcha-verify-button",
    },
    "hcaptcha": {
        "root": ".challenge-view, .challenge-container",
        "grid": ".task-grid, .challenge-example + div, .task-answers",
        "prompt": ".prompt-text, .challenge-prompt",
        "subject": "strong, .challenge-prompt-highlight",
        "tile": ".task-image",
        "verify": ".button-submit",
    },
}

SUPPORTED_KINDS = frozenset(_GRID_SELECTORS)

# reCAPTCHA routinely requires several rounds: "dynamic" challenges replace each
# correct tile with a new image and only accept once no matches remain, so a
# single pass cannot solve them even with perfect vision.
_DEFAULT_MAX_ROUNDS = 3
# A round that selects nothing and is not accepted means we are not converging;
# two in a row is the signal to hand over rather than burn more rounds.
_MAX_EMPTY_ROUNDS = 2

# Far above the module default, and measured rather than guessed: against a live
# 16-tile challenge ``google/gemma-4-e4b`` spent an entire 512-token budget on
# ``reasoning_content`` and returned no answer at all, three rounds running.
# Reasoning models think proportionally to how much there is to look at, and a
# grid of sixteen photographs is a lot; starving them reads as a solver failure
# when it is really a truncated thought.
_GRID_MAX_TOKENS = 2048


class GridChallenge(NamedTuple):
    """The challenge as the solver sees it."""

    prompt: str
    """Raw instruction text, e.g. "Select all images with crosswalks"."""
    target: str
    """Just the subject — "crosswalks" — extracted from the prompt."""
    tile_count: int
    screenshot_b64: str


_BOILERPLATE_PREFIXES = ("click verify", "if there are none", "press verify")


def _clean_prompt(raw: str) -> str:
    """Join the instruction into one line, dropping the interaction boilerplate.

    reCAPTCHA renders the prompt across two lines — "Select all squares with" and
    then the subject in its own ``<strong>`` — plus a "Click verify once there are
    none left" hint. Taking the *first* line therefore silently loses the subject:
    a live 4x4 challenge for "motorcycles" reduced to the target ``"a"``, and the
    model was asked to find nothing at all.
    """
    lines = [
        stripped
        for line in (raw or "").splitlines()
        if (stripped := line.strip()) and not stripped.lower().startswith(_BOILERPLATE_PREFIXES)
    ]
    return " ".join(lines)


def extract_target(prompt: str) -> str:
    """Pull the subject out of an instruction line.

    "Select all images with **crosswalks**" -> "crosswalks". Falls back to the
    whole prompt, which still works — it just gives the model more to read.
    """
    cleaned = _clean_prompt(prompt)
    match = re.search(
        r"(?:images?|squares?|pictures?)\s+(?:with|containing|of)\s+(?:a\s+|an\s+|the\s+)?(.+)",
        cleaned,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" .:")
    # hCaptcha phrases it as "Please click each image containing a motorbus".
    match = re.search(r"click (?:each|all|on all).*?(?:containing|with)\s+(.+)", cleaned, re.I)
    if match:
        return match.group(1).strip(" .:")
    return cleaned


def _build_prompt(challenge: GridChallenge, columns: int) -> str:
    """The instruction sent alongside the screenshot.

    Deliberately demands a bare list and forbids prose: the parser is tolerant,
    but a model that narrates burns the token budget a reasoning model needs, and
    the measured failure mode of ``gemma-4-e4b`` is exhausting its budget before
    emitting any content at all.
    """
    return (
        f"This image is a {columns}x{challenge.tile_count // columns} grid of "
        f"{challenge.tile_count} photo tiles.\n"
        f"Tiles are numbered 1 to {challenge.tile_count}, left to right, "
        "top to bottom.\n\n"
        f"TASK: identify every tile that contains: {challenge.target}\n\n"
        "Include a tile if any part of the subject appears in it, even partially.\n"
        "Answer with ONLY the tile numbers separated by commas, for example: 1,4,7\n"
        "If no tile matches, answer exactly: NONE\n"
        "Do not explain."
    )


def parse_tile_reply(reply: str, tile_count: int) -> list[int]:
    """Parse a model reply into zero-based tile indices.

    Tolerant on purpose — small local models wrap answers in prose or markdown
    however firmly they are told not to. Numbers outside the grid are dropped
    rather than clamped: a model that says "12" about a 9-tile grid is confused,
    and clicking tile 3 instead would be a guess dressed up as an answer.
    """
    if not reply:
        return []
    if re.search(r"\bNONE\b", reply, re.IGNORECASE) and not re.search(r"\d", reply):
        return []
    seen: list[int] = []
    for raw in re.findall(r"\d+", reply):
        index = int(raw) - 1
        if 0 <= index < tile_count and index not in seen:
            seen.append(index)
    return seen


def find_challenge_frame(page: Any, kind: CaptchaKind) -> Any:
    """Return the iframe holding the image grid, or ``None``.

    For reCAPTCHA this is the *bframe*, the opposite of the anchor frame that
    ``captcha_dom.find_anchor_frame`` looks for.
    """
    patterns = _CHALLENGE_FRAME_PATTERNS.get(kind, ())
    if not patterns:
        return None
    for frame in getattr(page, "frames", None) or ():
        frame_url = str(getattr(frame, "url", "") or "")
        if kind == "recaptcha-v2" and "anchor" in frame_url:
            continue
        if any(pattern in frame_url for pattern in patterns):
            return frame
    return None


async def read_challenge(frame: Any, kind: CaptchaKind) -> GridChallenge | None:
    """Screenshot the grid and read its instruction. ``None`` if no grid is up."""
    selectors = _GRID_SELECTORS.get(kind)
    if selectors is None:
        return None
    subject = ""
    try:
        root = await frame.query_selector(selectors["root"])
        if root is None:
            return None
        prompt_el = await frame.query_selector(selectors["prompt"])
        raw_prompt = await prompt_el.inner_text() if prompt_el is not None else ""
        if prompt_el is not None:
            # The subject has its own element. Preferred over parsing the text,
            # which is ambiguous once the browser joins the two rendered lines.
            subject_el = await prompt_el.query_selector(selectors["subject"])
            if subject_el is not None:
                subject = str(await subject_el.inner_text() or "").strip()
        tiles = await frame.query_selector_all(selectors["tile"])
        if not tiles:
            return None
        # Frame the grid exactly when we can; fall back to the whole panel.
        grid_el = await frame.query_selector(selectors["grid"])
        shot = await (grid_el or root).screenshot()
    except Exception as exc:
        _logger.debug("agent.captcha_vision.read_failed", kind=kind, error=str(exc))
        return None

    prompt = _clean_prompt(str(raw_prompt))
    return GridChallenge(
        prompt=prompt,
        target=subject or extract_target(prompt),
        tile_count=len(tiles),
        screenshot_b64=base64.b64encode(shot).decode("ascii"),
    )


def _columns_for(tile_count: int) -> int:
    """Grid width. reCAPTCHA uses 3x3 and 4x4; hCaptcha 3x3."""
    if tile_count % 4 == 0 and tile_count >= 16:  # noqa: PLR2004 — 4x4 grid
        return 4
    if tile_count == 9:  # noqa: PLR2004 — 3x3 grid
        return 3
    if tile_count == 8:  # noqa: PLR2004 — hCaptcha's 2x4 variant
        return 4
    return 3


async def solve_grid(  # noqa: PLR0911 — one honest exit per failure mode
    page: Any,
    kind: CaptchaKind,
    llm: LLMBackend,
    *,
    settle_s: float = 8.0,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
) -> bool:
    """Solve an image-grid challenge with ``llm``. Returns whether it produced a token.

    Returns ``False`` — honestly, and without raising — whenever the grid is
    absent, the model is unreachable, the reply is unparseable, or the rounds run
    out. The caller escalates to a paid tier on ``False``, so a hopeful ``True``
    here would be worse than useless: it would stop the cascade on an unsolved
    challenge, which is the exact bug this module's neighbours just had fixed.
    """
    if kind not in SUPPORTED_KINDS:
        return False
    frame = find_challenge_frame(page, kind)
    if frame is None:
        _logger.debug("agent.captcha_vision.no_challenge_frame", kind=kind)
        return False

    selectors = _GRID_SELECTORS[kind]
    empty_rounds = 0

    for round_number in range(1, max_rounds + 1):
        challenge = await read_challenge(frame, kind)
        if challenge is None:
            # No grid left to solve — either we cleared it or it never appeared.
            return bool(await read_response_token(page, kind))

        columns = _columns_for(challenge.tile_count)
        try:
            reply = await llm.complete_vision(
                _build_prompt(challenge, columns),
                [challenge.screenshot_b64],
                max_tokens=_GRID_MAX_TOKENS,
            )
        except AgentLLMError as exc:
            _logger.warning("agent.captcha_vision.llm_failed", kind=kind, error=str(exc))
            return False

        indices = parse_tile_reply(reply, challenge.tile_count)
        _logger.info(
            "agent.captcha_vision.round",
            kind=kind,
            round=round_number,
            target=challenge.target,
            tiles=challenge.tile_count,
            selected=len(indices),
            reply=reply.strip()[:120],
        )

        if not indices:
            empty_rounds += 1
            if empty_rounds >= _MAX_EMPTY_ROUNDS:
                _logger.info("agent.captcha_vision.not_converging", kind=kind)
                return False
        else:
            empty_rounds = 0
            if not await _click_tiles(frame, selectors["tile"], indices):
                return False

        if not await _click_verify(frame, selectors["verify"]):
            return False

        # The token is minted asynchronously once the answer is accepted.
        if await _await_token(page, kind, settle_s):
            _logger.info("agent.captcha_vision.solved", kind=kind, rounds=round_number)
            return True

    _logger.info("agent.captcha_vision.rounds_exhausted", kind=kind, rounds=max_rounds)
    return False


async def _click_tiles(frame: Any, tile_selector: str, indices: list[int]) -> bool:
    """Click the selected tiles, re-querying between clicks.

    The re-query is not defensive padding: a dynamic challenge replaces each tile
    the moment it is clicked, which detaches every handle captured beforehand.
    """
    for index in indices:
        try:
            tiles = await frame.query_selector_all(tile_selector)
            if index >= len(tiles):
                continue
            await tiles[index].click()
        except Exception as exc:
            _logger.debug("agent.captcha_vision.tile_click_failed", index=index, error=str(exc))
            return False
    return True


async def _click_verify(frame: Any, verify_selector: str) -> bool:
    try:
        button = await frame.query_selector(verify_selector)
        if button is None:
            return False
        await button.click()
    except Exception as exc:
        _logger.debug("agent.captcha_vision.verify_click_failed", error=str(exc))
        return False
    return True


async def _await_token(page: Any, kind: CaptchaKind, timeout_s: float) -> bool:
    wait = getattr(page, "wait_for_timeout", None)
    for _ in range(max(1, int(timeout_s / _TOKEN_POLL_S))):
        if await read_response_token(page, kind):
            return True
        if not callable(wait):
            break
        try:
            await wait(_TOKEN_POLL_S * 1000.0)
        except Exception:  # pragma: no cover — page closed mid-poll
            break
    return bool(await read_response_token(page, kind))


_TOKEN_POLL_S = 0.5


__all__ = [
    "SUPPORTED_KINDS",
    "GridChallenge",
    "extract_target",
    "find_challenge_frame",
    "parse_tile_reply",
    "read_challenge",
    "solve_grid",
]

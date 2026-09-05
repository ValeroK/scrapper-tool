"""Looking at the page, for walls no signature can describe.

Every markup detector in :mod:`scrapper_tool._challenge` was written *after* a
wall got through. That is a good process and it has a floor it cannot cross: a
signature can only recognise a wall someone has already lost a day to. The next
wall will not match, by construction.

This is the one detection surface an adversary cannot cheaply randomise. Class
names, ids, sizes and phrasing are all free to rotate per deploy -- the reported
Cloudflare interstitial used ``KfMSd3``, ``YpeSi0``, ``VNsDw9`` -- but the page
must stay legible to a human or it stops working as a wall. A model that reads it
the way a human does is looking at the part that has to stay stable.

Measured before being trusted, on real fixtures through a real browser:

* median latency 3.6 s on a local 27B VLM
* it identified the reported host-titled interstitial **cold**, from a prompt
  naming no vendor, no class and no phrase -- the page that had defeated every
  heuristic we had
* on the pages we already had signatures for it was *worse* than markup: two
  correct, three abstentions, one miss

So this is deliberately NOT the primary detector, and it never overrides a markup
verdict. Markup is faster, deterministic, explainable and better on everything
currently testable. This runs exactly where markup must fail -- a page it has no
signature for -- and its value is generalisation, not accuracy.

**BLANK is a first-class answer.** Three of six fixtures painted nothing at all
(verified at the pixel level: one colour across the frame), because JS-driven
walls and unhydrated app shells look identical before they paint. A binary
prompt was confidently wrong on all three. Letting the model say "nothing has
rendered yet" is what makes it safe to believe when it does answer.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Literal

from scrapper_tool._logging import get_logger

if TYPE_CHECKING:
    from scrapper_tool.agent.backends.llm import LLMBackend

_logger = get_logger(__name__)

#: WALL - something stands between the user and the content.
#: BLANK - nothing has painted; no verdict is possible yet.
#: PAGE - real content, or a page that is genuinely empty.
VisionVerdict = Literal["WALL", "BLANK", "PAGE"]

# Reasoning models spend budget before emitting content, and this one is asked
# for a single word -- so the budget has to cover the thinking, not the answer.
# Measured: ~240 reasoning tokens before a one-word reply.
_MAX_TOKENS = 2000

# Deliberately names no vendor, no product and no phrase. The whole point is a
# verdict that does not depend on having seen this wall before; feeding it our
# signature vocabulary would just launder markup detection through a model.
_PROMPT = (
    "Screenshot of a web page a scraper just fetched. Reply with ONE word:\n"
    "WALL  - a bot check, captcha, 'verify you are human', or access-denied "
    "notice standing between the user and the content\n"
    "BLANK - nothing has rendered; the page is empty or still loading\n"
    "PAGE  - real readable content, or a page that is genuinely empty of results\n"
    "One word only."
)


#: A verdict is one word. Anything longer than this is the model thinking out
#: loud, not answering, and must not be mined for a keyword.
_MAX_ANSWER_CHARS = 40

_VERDICTS: tuple[VisionVerdict, ...] = ("WALL", "BLANK", "PAGE")


def _parse(reply: str) -> VisionVerdict | None:
    """The reply's verdict, or None if it did not actually give one.

    Substring-matching a long reply is unsafe, and measurably so. A reasoning
    model that runs out of budget returns partial *reasoning* rather than an
    answer -- ``_extract_message_text`` surfaces it deliberately, to make the
    starvation legible -- and that reasoning restates the task, so it contains
    the very words this function looks for. Measured against the local 27B on a
    page it calls WALL at full budget:

        budget 2000 -> "WALL"                     correct
        budget   60 -> reasoning -> WALL          right by accident
        budget   30 -> reasoning -> PAGE          WRONG, and confident

    A wall returned as content is the failure this whole detector exists to
    prevent, so a reply is only a verdict if it *is* one: short, and naming
    exactly one of the three. Everything else is "no opinion", which costs a
    markup-only verdict and nothing more.
    """
    upper = reply.strip().upper()
    if len(upper) > _MAX_ANSWER_CHARS:
        return None
    found: list[VisionVerdict] = [v for v in _VERDICTS if v in upper]
    if len(found) != 1:
        return None
    return found[0]


async def look_at_page(png: bytes, backend: LLMBackend) -> VisionVerdict | None:
    """Ask the model what it sees. ``None`` when it could not be asked at all.

    Never raises. A vision call is an enrichment on top of a verdict that already
    exists, so every failure mode -- no model, unreachable server, timeout,
    unparseable reply -- has to degrade to "no opinion" rather than to an
    exception. The markup verdict is the floor and it stands on its own.
    """
    if not png:
        return None
    try:
        reply = await backend.complete_vision(
            _PROMPT, [base64.b64encode(png).decode()], max_tokens=_MAX_TOKENS
        )
    except Exception as exc:
        _logger.info("wall_vision.unavailable", error=str(exc)[:160])
        return None
    verdict = _parse(reply)
    if verdict is None:
        _logger.info("wall_vision.unparseable", reply=reply[:80])
        return None
    _logger.info("wall_vision.verdict", verdict=verdict)
    return verdict


__all__ = ["VisionVerdict", "look_at_page"]

"""Bot-wall and content-quality detection, shared by every surface.

Previously these heuristics lived inside ``http_server`` (so MCP had none), only
knew Cloudflare, and were used solely to pick a Scrapling retry strategy. They
belong in the cascade's escalation decision, across both surfaces.

Two distinct questions, deliberately separated:

1. **Were we walled?** :func:`is_interstitial` — a bot-vendor challenge page
   instead of content.
2. **Is the content actually usable?** :func:`looks_unhydrated` — a 200 with a
   big, plausible body whose text is still un-rendered template placeholders.

Both were found by testing real sites, and both are cases the signal-only
classifier gets wrong today:

- ``one.co.il`` returned **200, 419 KB, and a JSON-LD block** — so the classifier
  accepted it — but the JSON-LD was ``@type: WebSite`` site metadata and the
  headings were literal ``{displayTitle}`` placeholders. Rendering the same URL
  produced **212 real headlines vs 4**. A silent-quality failure, worse than a
  block because nothing errors.
- ``store.mopar.com`` returned **HTTP 403 with 1.35 MB of genuine content** — the
  anti-bot 403s the document, then JS clears it and the real DOM renders. So
  **status code is not a success signal**; content is.

Signature safety is calibrated against real captures, not guesswork: every marker
below was verified *absent* from a known-good, Akamai-protected Mopar render, so
matching one is strong evidence of an actual wall rather than incidental script
text.
"""

from __future__ import annotations

import re

# Only the head of a document is scanned — challenge pages declare themselves early
# and this keeps the check cheap on multi-MB documents.
_BODY_SCAN_BYTES = 8_192
# Challenge interstitials are tiny. A large body with a block status is far more
# likely to be real content served with a hostile status (the Mopar case).
_CHALLENGE_BODY_MAX_BYTES = 50_000
_BLOCK_STATUS_CODES: frozenset[int] = frozenset({403, 503})
_SPA_SHELL_MAX_BYTES = 30_000

# Cloudflare-only subset. Kept separate because Pattern D's `solve_cloudflare`
# probe must keep its exact original behaviour — broadening it would make
# Scrapling attempt a CF solve on non-CF vendors.
_CF_CHALLENGE_SIGNATURES: tuple[str, ...] = (
    "<title>just a moment...",
    "<title>attention required! | cloudflare",
    "challenges.cloudflare.com/turnstile",
    'cf-mitigated"',
    "cf-chl-bypass",
)

# Per-vendor markers. Verified absent from a known-good Akamai-protected render.
_VENDOR_SIGNATURES: dict[str, tuple[str, ...]] = {
    "cloudflare": _CF_CHALLENGE_SIGNATURES,
    "radware": (
        "validate.perfdrive.com",
        "are you for real",
        "shieldsquare",
        "_pxhd_radware",
    ),
    "datadome": ("geo.captcha-delivery.com", "datadome captcha", "dd_cookie_test"),
    "perimeterx": ("px-captcha", "human verification: px", "/px/captcha"),
    "akamai": ("reference #18.", "akamai reference", "ak_bmsc_challenge"),
    "kasada": ("kpsdk-challenge", "/149e9513-01fa/2i/"),
    "incapsula": ("incapsula incident id", "_incapsula_resource"),
}

# SPA roots — a shell that hasn't hydrated yet.
_SPA_SHELL_SIGNATURES: tuple[str, ...] = (
    'id="root"',
    'id="app"',
    'id="__next"',
    "data-reactroot",
    "ng-version=",
    "window.__nuxt__",
    "window.__initial_state__",
)

# `{identifier}` left inside visible heading text = the template never rendered.
# Scoped to headings (not raw HTML) so ordinary inline JS with braces can't trip it.
_HEADING_RE = re.compile(r"<h[1-3][^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")
# Measured on real pages (see looks_unhydrated): a *ratio*, not an absolute count.
# An absolute threshold false-positives badly — a fully-rendered page keeps stray
# placeholders in unused/hidden templates (one.co.il rendered: 5 of 248 headings).
#   one.co.il unhydrated : 5/26  = 0.19   <- bad
#   one.co.il rendered   : 5/248 = 0.02   <- good
#   mopar rendered       : 0/23  = 0.00   <- good
_MAX_PLACEHOLDER_HEADING_RATIO = 0.10
_MIN_PLACEHOLDER_HEADINGS = 3


def is_cf_challenge_body(html: str, status_code: int) -> bool:
    """True when the response looks like a **Cloudflare** challenge page.

    Deliberately Cloudflare-only: Pattern D uses this to decide whether to retry
    with Scrapling's ``solve_cloudflare``, and that solver is CF-specific.
    """
    if status_code in _BLOCK_STATUS_CODES and html and len(html) < _CHALLENGE_BODY_MAX_BYTES:
        return True
    if not html:
        return False
    head = html[:_BODY_SCAN_BYTES].lower()
    return any(sig in head for sig in _CF_CHALLENGE_SIGNATURES)


def is_interstitial(html: str, status_code: int = 200) -> str | None:
    """Return the bot-vendor name if this looks like a challenge page, else None.

    Content-first by design. A block status only counts when the body is *small*
    — a 403 carrying 1.35 MB of real DOM is a served page, not a wall.
    """
    if not html:
        return None
    head = html[:_BODY_SCAN_BYTES].lower()
    for vendor, signatures in _VENDOR_SIGNATURES.items():
        if any(sig in head for sig in signatures):
            return vendor
    # Also scan the tail-agnostic full body for redirect-style walls that put their
    # marker outside the first 8 KB (Radware/PerfDrive redirects do this).
    lowered = html.lower()
    for vendor in ("radware", "datadome", "perimeterx"):
        if any(sig in lowered for sig in _VENDOR_SIGNATURES[vendor]):
            return vendor
    if status_code in _BLOCK_STATUS_CODES and len(html) < _CHALLENGE_BODY_MAX_BYTES:
        return "unknown"
    return None


def looks_like_spa_shell(html: str) -> bool:
    """True when the response looks like an unhydrated SPA shell (small + SPA root)."""
    if not html or len(html) > _SPA_SHELL_MAX_BYTES:
        return False
    head = html[:_BODY_SCAN_BYTES].lower()
    return any(sig in head for sig in _SPA_SHELL_SIGNATURES)


def looks_unhydrated(html: str) -> bool:
    """True when headings are mostly un-rendered template placeholders.

    Catches the ``one.co.il`` case: a 419 KB HTTP 200 whose headings read
    ``{displayTitle}``, where rendering the same URL yields 212 real headlines.
    Size-independent on purpose — :func:`looks_like_spa_shell` caps at 30 KB and
    that page sailed straight past it.

    **Advisory, not authoritative.** Measured against real pages, placeholders are
    a weak discriminator: the unhydrated page still had 19 genuine headings (static
    nav), and a fully-rendered page still had 5 leftover placeholders. It is
    therefore a ratio test with a floor, and it is deliberately NOT used to reject
    content (see :func:`has_real_content`) — treat a True as "worth escalating to
    the render tier", not as "this page is garbage".
    """
    if not html:
        return False
    headings = _HEADING_RE.findall(html)
    if not headings:
        return False
    hits = sum(1 for heading in headings if _PLACEHOLDER_RE.search(heading))
    if hits < _MIN_PLACEHOLDER_HEADINGS:
        return False
    return (hits / len(headings)) > _MAX_PLACEHOLDER_HEADING_RATIO


def looks_like_block_message(text: str) -> bool:
    """Whether an *error message* suggests an anti-bot block rather than a bug.

    Distinct from :func:`is_interstitial`, which inspects a document: by the time
    a browser or LLM tier raises, there is no document — only whatever string the
    library produced. So this matches on the message, and it deliberately reuses
    the same per-vendor signatures, because a navigation error frequently carries
    the wall's own hostname (``validate.perfdrive.com``,
    ``geo.captcha-delivery.com``) right in the text.

    Used to decide between ``AgentBlockedError`` (the cascade may escalate) and
    ``AgentError`` (a real failure). Over-matching costs a pointless escalation;
    under-matching means a blocked page is reported as a crash, so the generic
    terms below are kept broad on purpose.
    """
    if not text:
        return False
    lowered = text.lower()
    if any(term in lowered for term in _BLOCK_MESSAGE_TERMS):
        return True
    return any(sig in lowered for sigs in _VENDOR_SIGNATURES.values() for sig in sigs)


# Generic wording that shows up in anti-bot failures across libraries. Kept
# separate from the vendor table so each can grow without disturbing the other.
_BLOCK_MESSAGE_TERMS: tuple[str, ...] = (
    "challenge",
    "captcha",
    "blocked",
    "403",
    "429",
    "access denied",
    "forbidden",
    "unusual traffic",
    "rate limit",
    "too many requests",
    "verify you are human",
    "are you a robot",
    "bot detect",
)


def has_real_content(html: str, status_code: int = 200) -> bool:
    """Whether a fetch/render produced usable (non-walled) content.

    The success signal for the render tier and for proxy-health accounting —
    explicitly NOT the status code, because a bot-walled 200 is a failure while a
    challenged-then-rendered 403 is a success (store.mopar.com).

    Note this checks **only** for a bot wall. Hydration is intentionally excluded:
    it's a weak, advisory heuristic (see :func:`looks_unhydrated`), and a false
    "unusable" here would be actively harmful — it would discard good content and
    penalise a healthy proxy. Content *quality* is the classifier's job (did we
    extract a signal?); this function answers the narrower "were we walled?".
    """
    if not html:
        return False
    return is_interstitial(html, status_code) is None


__all__ = [
    "has_real_content",
    "is_cf_challenge_body",
    "is_interstitial",
    "looks_like_block_message",
    "looks_like_spa_shell",
    "looks_unhydrated",
]

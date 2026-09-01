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
from urllib.parse import urlsplit

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
    # Akamai Bot Manager. The first three are the classic "Access Denied"
    # reference-number pages. The rest are the *challenge container* ids used by
    # the interstitial and behavioural (tile-clicking) walls, captured live from
    # dickssportinggoods.com — see tests/fixtures/challenge/.
    #
    # Deliberately NOT here: the Akamai *sensor* script path (a random-looking
    # `/<segment>/<segment>/...?v=<uuid>` <script src>). It is served on walled
    # AND on perfectly good pages from the same host — the real Angular shell in
    # akamai_protected_real_shell_200.html carries it — so matching it would flag
    # every Akamai-protected page as a wall. Same for `sec-overlay` /
    # `sec-container`, which the good shell also carries. `sec-if-cpt-container`
    # (interstitial captcha) and `sec-bc-*` (behavioural challenge) are distinct
    # ids that appear only on the wall.
    "akamai": (
        "reference #18.",
        "akamai reference",
        "ak_bmsc_challenge",
        "sec-if-cpt-container",
        "sec-bc-tile-container",
        "sec-bc-text-container",
        "scf-akamai-protected-by",
    ),
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

# --- content-free-shell fallback (catches a 200 wall with no known signature) --
#
# A signature miss on a 403 degrades safely to "unknown" via the status gate. On a
# 200 there is no such net, and the failure is silent *data corruption*: the
# caller is handed a bot wall as content. This is the net for that case.
#
# It is deliberately narrow, because the obvious heuristic does not work. Three
# real bodies from one host (tests/fixtures/challenge/) constrain it:
#
#   akamai_behavioral_200.html      2,365 B  wall            -> must flag
#   akamai_protected_real_shell_200 3,406 B  real SPA shell  -> must NOT flag
#   example.com                       559 B  real content    -> must NOT flag
#
# "Small body with almost no visible text" flags all three: the legitimate
# Angular shell's only text is a <noscript> line. The discriminator that actually
# separates them is the **<title>**: real documents have one (even the shell —
# "DICK'S Sporting Goods - Official Site"), while the wall ships bare
# `<html lang="en"><body>` with no <head> at all. A titleless, script-bearing,
# text-free HTML 200 is not a page anyone meant to serve.
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_STYLE_RE = re.compile(r"<(style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SHELL_MAX_BYTES = 15_000
_SHELL_MAX_TEXT_CHARS = 200
# Structured data is proof of intent: a bot wall does not publish a schema.org
# Product or Open Graph tags. Without this escape hatch the check condemns a
# legitimate page whose payload lives entirely in a JSON-LD block and whose
# <body> is filled in by JS — which has no title and no visible text either, so
# it is otherwise indistinguishable from the Akamai wall.
_STRUCTURED_DATA_MARKERS: tuple[str, ...] = (
    "application/ld+json",
    "itemtype=",
    "itemprop=",
    'property="og:',
    "property='og:",
)
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

    # The head scan is deliberately UNBOUNDED by size. A challenge page declares
    # itself early, and size is not the discriminator here — measured on yad2's
    # Radware wall, which is **118 KB** because Radware pads it with an enormous
    # obfuscated JS payload. Capping this scan by body size (a plausible-sounding
    # rule) accepts that wall as content.
    head = html[:_BODY_SCAN_BYTES].lower()
    for vendor, signatures in _VENDOR_SIGNATURES.items():
        if any(sig in head for sig in signatures):
            return vendor

    # Past the head, size IS the discriminator, and this scan needs the cap the
    # head scan must not have. Three real bodies fix the rule:
    #
    #   yad2 ladder wall      118 KB   marker at offset  3,347  (in head)  -> wall
    #   yad2 rendered page   3.3 MB    marker at offset 11,766  (past head) -> served
    #   Radware redirect      < 50 KB  marker past head                     -> wall
    #
    # The rendered page has 27 prices and 729 listing elements and carries
    # `validate.perfdrive.com` only because that is the site's own protection
    # script. Without the cap, every successful scrape of a Radware / DataDome /
    # PerimeterX site was reported as blocked — so the cascade escalated past a
    # tier that had already won, and `has_real_content` told the proxy pool to
    # `mark_blocked` a proxy that had just done its job.
    #
    # The tail scan exists for redirect-style walls that put their marker outside
    # the first 8 KB, and those are small; the cap preserves that intent exactly.
    if len(html) < _CHALLENGE_BODY_MAX_BYTES:
        lowered = html.lower()
        for vendor in ("radware", "datadome", "perimeterx"):
            if any(sig in lowered for sig in _VENDOR_SIGNATURES[vendor]):
                return vendor
    if status_code in _BLOCK_STATUS_CODES and len(html) < _CHALLENGE_BODY_MAX_BYTES:
        return "unknown"
    if looks_like_content_free_shell(html):
        return "unknown"
    return None


def looks_like_content_free_shell(html: str) -> bool:
    """True for a small, titleless, script-only document with no visible text.

    The last net under :func:`is_interstitial` for a wall served with **HTTP 200**
    and no recognised vendor signature — the case that has no status gate to fall
    back on and therefore fails silently, handing a bot wall to the caller as
    content. See the constants above for the three real bodies this is calibrated
    against, and why ``<title>`` rather than body size is the discriminator.

    A false positive here costs one unnecessary escalation; a false negative
    corrupts the result. Narrow, but biased in the safe direction.
    """
    if not html or len(html) > _SHELL_MAX_BYTES:
        return False
    lowered = html.lower()
    if any(marker in lowered for marker in _STRUCTURED_DATA_MARKERS):
        return False
    title_match = _TITLE_RE.search(html)
    if title_match is not None and title_match.group(1).strip():
        return False
    if "<script" not in lowered:
        return False
    stripped = _STYLE_RE.sub(" ", _SCRIPT_RE.sub(" ", html))
    text = _TAG_RE.sub(" ", stripped)
    return len(" ".join(text.split())) < _SHELL_MAX_TEXT_CHARS


# --- redirect-to-challenge -------------------------------------------------
#
# The signature tables above answer "does this body announce a known vendor?".
# They cannot answer "did the vendor quietly move us somewhere else?", and that
# is the gap that corrupts results the worst way round.
#
# Measured: a vendor answered a product URL with a 4,419-byte page at
# ``/captcha.html``. It has a <title>, it has visible text, and it carries no
# vendor signature -- so ``is_interstitial`` returned None and
# ``has_real_content`` returned True, and the caller was handed a captcha as
# content. A false negative, which is strictly worse than a false block: a false
# block costs one escalation, a false pass corrupts the dataset silently.
#
# The evidence the signature tables miss is in the *URL*: we asked for a product
# page and finished somewhere else entirely.
#
# What this must NOT do is treat any redirect as suspicious. Ordinary redirects
# are everywhere -- scheme upgrades, trailing slashes, www canonicalisation,
# locale prefixes, and genuine content moves -- and flagging them would escalate
# most of the web. So difference alone is never enough; there has to be
# corroborating evidence, and there are exactly two kinds worth trusting.
_CHALLENGE_PATH_TOKENS: tuple[str, ...] = (
    "/captcha",
    "/challenge",
    "/blocked",
    "/access-denied",
    "/accessdenied",
    "/_sec/",
    "/cdn-cgi/challenge",
    "/distil_r_captcha",
    "/px/captcha",
    "/sorry/",
)
# Phrases a page shows a *human* when it is asking them to prove they are one.
# Deliberately much narrower than `_BLOCK_MESSAGE_TERMS`, which matches things
# like "403" and "challenge" anywhere -- fine for an exception string, far too
# loose for page text, where "security check" in a footer would condemn the page.
_VERIFICATION_PHRASES: tuple[str, ...] = (
    "are not a robot",
    "are a robot",
    "verify you are human",
    "verify you're human",
    "confirm you are human",
    "complete the security check",
    "security check to access",
    "enable javascript and cookies",
    "unusual traffic from your",
)


def _normalised_path(url: str) -> str | None:
    """Path of ``url``, lowercased, without a trailing slash. None if unparseable.

    Host and scheme are deliberately discarded: an http->https upgrade or a www
    canonicalisation is not a redirect worth reasoning about, and comparing full
    URLs would flag both.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.path:
        return "/"
    return parts.path.rstrip("/").lower() or "/"


def landed_on_challenge(requested_url: str, final_url: str, html: str) -> bool:
    """True when a request finished on a page that is asking us to prove we're human.

    Two independent signals, either sufficient, both requiring that the path
    actually changed:

    1. The final path *names* itself a challenge (``/captcha``, ``/sorry/``,
       ``/cdn-cgi/challenge``...). High precision -- no ordinary product route
       looks like this -- so no body evidence is needed.
    2. The path changed and the body is small and says, in words, that it wants
       a human. Small because challenge interstitials are; the phrase list is
       narrow because page furniture is not evidence.

    Returns False for everything else, including every redirect that merely moved
    scheme, host or trailing slash. See :func:`is_interstitial` for the
    body-signature half of this question -- the two are complementary, and this
    one exists precisely for walls that carry no recognisable signature.
    """
    if not requested_url or not final_url or not html:
        return False
    requested_path = _normalised_path(requested_url)
    final_path = _normalised_path(final_url)
    if requested_path is None or final_path is None or requested_path == final_path:
        return False

    if any(token in final_path for token in _CHALLENGE_PATH_TOKENS):
        return True

    if len(html) >= _CHALLENGE_BODY_MAX_BYTES:
        return False
    lowered = html.lower()
    return any(phrase in lowered for phrase in _VERIFICATION_PHRASES)


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


def block_evidence(text: str) -> str | None:
    """What in ``text`` suggests a block, or None if nothing does.

    The evidence half of :func:`looks_like_block_message`. That function answers
    "is this a block?" and throws away *why*, which turned out to be the half
    callers actually need: a result carrying ``blocked=True`` and no named cause
    asserts a wall while naming no evidence of one, and a consumer cannot act on
    it. Returning the reason makes the claim checkable.

    A vendor name wins over a generic term when both match, because "datadome" is
    strictly more useful to a caller than "captcha".
    """
    if not text:
        return None
    lowered = text.lower()
    for vendor, signatures in _VENDOR_SIGNATURES.items():
        if any(sig in lowered for sig in signatures):
            return vendor
    for term in _BLOCK_MESSAGE_TERMS:
        if term in lowered:
            return term
    return None


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
    return block_evidence(text) is not None


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
    "block_evidence",
    "has_real_content",
    "is_cf_challenge_body",
    "is_interstitial",
    "landed_on_challenge",
    "looks_like_block_message",
    "looks_like_content_free_shell",
    "looks_like_spa_shell",
    "looks_unhydrated",
]

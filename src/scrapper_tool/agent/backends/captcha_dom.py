"""DOM-level captcha detection + token injection (pure Playwright).

The net-new building block that makes the captcha solver cascade
(:mod:`scrapper_tool.agent.backends.captcha`) actually *do* something on a
live page. No browser-use / Crawl4AI coupling — it operates on a
Playwright-shaped ``Page`` and is driven by the shared page-hook consumers
(:mod:`scrapper_tool.agent.backends.page_hooks`).

Mechanism-aware by design (2026 best practice): Cloudflare Turnstile binds
its token to the browser environment that requested it, so a token solved
out-of-context frequently fails the environment check. Therefore
:func:`solve_on_page`:

1. Tries **stealth auto-pass first** (settle + re-check) — the most
   reliable, zero-cost path for Turnstile that a stealth browser
   (Camoufox) passes natively.
2. Falls back to a **solver token** only if the challenge persists, and
   for Turnstile logs a low-confidence warning because foreign-token
   injection is unreliable there.
3. Uses foreign-token injection for portable-token kinds (reCAPTCHA,
   hCaptcha) where the token travels across contexts.

Behavioral / DataDome-JS challenges that no token can satisfy are left in
place and surface upstream as ``blocked`` rather than a silent "pass".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from scrapper_tool._challenge import is_interstitial
from scrapper_tool._logging import get_logger
from scrapper_tool.errors import CaptchaSolveError

if TYPE_CHECKING:
    from scrapper_tool.agent.backends.captcha import CaptchaKind, CaptchaSolver
    from scrapper_tool.agent.backends.llm import LLMBackend

_logger = get_logger(__name__)

# One evaluate() call classifies the challenge, reads its sitekey, and collects
# whatever else that kind's solver needs. Returns
# ``{"kind": ..., "site_key": ..., "extra": {...}}`` or ``null``.
#
# Previously this had exactly three return paths, so ``detect_challenge`` could
# only ever yield turnstile / hcaptcha / recaptcha-v2 while ``CaptchaKind``
# declared ten. The other seven were unreachable by any automatic path, which
# made the paid tiers' advertised DataDome / AWS-WAF / FunCaptcha coverage
# unreachable in practice — a live run hit three DataDome walls that could not
# have been routed to a solver even with a key.
#
# Ordering is most-specific-first: reCAPTCHA v3 is only reported when no v2
# widget exists (the v3 script is present on many pages that also run v2), and
# the generic image case is last so a branded widget always wins.
#
# ``extra`` carries the per-kind parameters that a sitekey cannot express —
# DataDome needs the challenge URL, AWS WAF needs the `gokuProps` triple,
# GeeTest needs its challenge nonce. Without these the corresponding solver
# tasks are rejected by the provider even when the kind is correct.
_DETECT_JS = r"""
() => {
  const pick = (sel) => document.querySelector(sel);
  const attr = (el, name) => (el && el.getAttribute(name)) || '';
  const hit = (kind, site_key, extra) => ({ kind, site_key: site_key || '', extra: extra || {} });
  // Resolving a relative URL throws when the document has a non-hierarchical
  // base (about:blank, srdoc iframes, data: documents), and an uncaught throw
  // here aborts the WHOLE detection — one relative <img src> would make the page
  // look captcha-free. Degrade to the raw attribute instead.
  const abs = (u) => {
    try { return new URL(u, location.href).href; } catch (e) { return u || ''; }
  };
  const origin = (u) => {
    try { return new URL(u, location.href).origin; } catch (e) { return ''; }
  };

  // --- Cloudflare Turnstile ---
  let el = pick('.cf-turnstile[data-sitekey]') || pick('[data-sitekey][data-action]');
  if (el && el.className && el.className.indexOf('cf-turnstile') !== -1) {
    return hit('turnstile', attr(el, 'data-sitekey'), { action: attr(el, 'data-action') });
  }
  if (pick('iframe[src*="challenges.cloudflare.com"]')) {
    return hit('turnstile', attr(pick('.cf-turnstile[data-sitekey]'), 'data-sitekey'));
  }

  // --- hCaptcha ---
  el = pick('.h-captcha[data-sitekey]');
  if (el || pick('iframe[src*="hcaptcha.com"]')) {
    return hit('hcaptcha', attr(el, 'data-sitekey'));
  }

  // --- reCAPTCHA ---
  // Order matters and is not obvious: v3's invisible badge ALSO renders an
  // `api2/anchor` iframe, so testing that iframe first misreports every v3 page
  // as v2 with an empty sitekey (measured on 2captcha's v3 demo). The explicit
  // `.g-recaptcha[data-sitekey]` widget is the only unambiguous v2 marker, so it
  // goes first; the `render=` script parameter is the unambiguous v3 marker and
  // goes second; the bare iframe is a last-resort v2 guess.
  el = pick('.g-recaptcha[data-sitekey]');
  if (el) {
    return hit('recaptcha-v2', attr(el, 'data-sitekey'));
  }
  for (const s of document.querySelectorAll('script[src*="recaptcha/api.js"]')) {
    const m = (s.getAttribute('src') || '').match(/[?&]render=([^&]+)/);
    if (m && m[1] && m[1] !== 'explicit') {
      return hit('recaptcha-v3', decodeURIComponent(m[1]));
    }
  }
  const rcFrame = pick('iframe[src*="recaptcha/api2/anchor"]')
               || pick('iframe[src*="google.com/recaptcha"]');
  if (rcFrame) {
    // The anchor iframe carries the sitekey as `k=`, which is the only place it
    // appears when the host page renders the widget programmatically.
    const m = (rcFrame.getAttribute('src') || '').match(/[?&]k=([^&]+)/);
    return hit('recaptcha-v2', m ? decodeURIComponent(m[1]) : '');
  }

  // --- FunCaptcha / Arkose Labs ---
  el = pick('[data-pkey]') || pick('#FunCaptcha') || pick('#arkose');
  const arkoseFrame = pick('iframe[src*="arkoselabs.com"]')
                   || pick('iframe[src*="funcaptcha.com"]');
  // Arkose is normally bootstrapped by a script whose path carries the public
  // key (`.../v2/<pkey>/api.js`) and which loads before any iframe exists, so
  // looking only at iframes misses the widget during setup.
  const arkoseScript = pick('script[src*="arkoselabs.com"]')
                    || pick('script[src*="funcaptcha.com"]');
  if ((el && attr(el, 'data-pkey')) || arkoseFrame || arkoseScript) {
    let pkey = attr(el, 'data-pkey');
    if (!pkey && arkoseFrame) {
      const m = (arkoseFrame.getAttribute('src') || '').match(/[?&]public_key=([^&]+)/);
      if (m) pkey = decodeURIComponent(m[1]);
    }
    if (!pkey && arkoseScript) {
      const src = arkoseScript.getAttribute('src') || '';
      // Real Arkose public keys are UUID-shaped, e.g. 476068BF-9607-4799-B53D-966BE98E2B81.
      const m = src.match(/\/v2\/([0-9A-Za-z-]{16,})\/api\.js/)
             || src.match(/[?&]public_key=([^&]+)/);
      if (m) pkey = decodeURIComponent(m[1]);
    }
    const surlFrom = arkoseFrame || arkoseScript;
    // Arkose is the same product; report the branded name so the solver's
    // FunCaptcha task type is selected either way.
    return hit(attr(el, 'data-pkey') ? 'funcaptcha' : 'arkose', pkey, {
      surl: surlFrom ? origin(surlFrom.getAttribute('src')) : ''
    });
  }

  // --- GeeTest ---
  // Class names are suffixed per-instance (`geetest_captcha_f0a7e2ce`), so match
  // on the prefix. The id is NOT in any documented global — measured on a live v4
  // page, the only place it appears is the loader script's query string
  // (`gcaptcha4.geetest.com/load?...&captcha_id=<id>`), so parse it from there.
  if (pick('[class^="geetest_"]') || pick('[class*=" geetest_"]')
      || typeof window.initGeetest === 'function'
      || typeof window.initGeetest4 === 'function') {
    let key = '', version = typeof window.initGeetest4 === 'function' ? '4' : '3';
    let challenge = '';
    for (const s of document.querySelectorAll('script[src*="geetest.com"]')) {
      const src = s.getAttribute('src') || '';
      const v4 = src.match(/[?&]captcha_id=([^&]+)/);
      if (v4) { key = decodeURIComponent(v4[1]); version = '4'; break; }
      const v3 = src.match(/[?&]gt=([^&]+)/);
      if (v3) { key = decodeURIComponent(v3[1]); version = '3'; }
      const ch = src.match(/[?&]challenge=([^&]+)/);
      if (ch) challenge = decodeURIComponent(ch[1]);
    }
    const cfg = window.__geetest_config || window.gtConfig || {};
    return hit('geetest', key || cfg.gt || cfg.captchaId || '', {
      challenge: challenge || cfg.challenge || '',
      version: version
    });
  }

  // --- DataDome ---
  // Identified by the challenge iframe URL, not a sitekey; the solver needs the
  // full URL including its `initialCid`/`cid` query. Checked before AWS WAF
  // because an iframe URL match is the more specific signal of the two.
  const ddFrame = pick('iframe[src*="geo.captcha-delivery.com"]')
               || pick('iframe[src*="captcha-delivery.com"]');
  if (ddFrame) {
    return hit('datadome', '', {
      captchaUrl: abs(ddFrame.getAttribute('src'))
    });
  }

  // --- AWS WAF ---
  // `gokuProps` is what the AWS challenge script itself reads; a solver given
  // only the page URL cannot reconstruct it.
  //
  // Requires either a real awswaf resource or a fully-populated gokuProps. A
  // bare `window.gokuProps` is NOT enough: the global outlives the document that
  // set it (it survives same-context navigation and set_content), so keying off
  // its mere presence made every subsequent page on that tab report aws-waf and
  // swallowed the DataDome and image branches below.
  const goku = window.gokuProps;
  const wafRes = pick('script[src*="awswaf.com"]') || pick('iframe[src*="awswaf.com"]');
  if (wafRes || (goku && goku.key && goku.context)) {
    return hit('aws-waf', '', {
      awsKey: (goku && goku.key) || '',
      awsIv: (goku && goku.iv) || '',
      awsContext: (goku && goku.context) || '',
      awsChallengeJS: wafRes ? abs(wafRes.getAttribute('src')) : ''
    });
  }

  // --- Generic image captcha (last resort) ---
  // Only when an image sits next to an input that names itself a captcha, so an
  // ordinary decorative image cannot trip it.
  const field = pick('input[name*="captcha" i]') || pick('input[id*="captcha" i]');
  if (field) {
    const scope = field.closest('form') || document.body;
    const img = scope.querySelector(
      'img[src*="captcha" i], img[id*="captcha" i], img[class*="captcha" i]');
    if (img) {
      return hit('image', '', { image_url: abs(img.getAttribute('src')) });
    }
  }
  return null;
}
"""

# Fetch the captcha image *in page context* and return it base64-encoded.
# Done in-page rather than with an out-of-band httpx GET because these images are
# almost always session-bound: refetching from outside the browser either 403s or
# — worse — rotates the challenge, so the solver would be handed a different
# image than the one the form expects.
_IMAGE_B64_JS = r"""
async (url) => {
  const resp = await fetch(url, { credentials: 'include' });
  const buf = await resp.arrayBuffer();
  let binary = '';
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}
"""

# Read the challenge-response field for `kind`. Non-empty = the widget (or an
# injected token) has produced a credential the form will submit.
#
# This is the success signal, NOT "is the widget still in the DOM". A solved
# reCAPTCHA or hCaptcha leaves its widget in place — it just turns into a green
# tick — so re-running detection after a solve reports the challenge as still
# present and a real success reads as a failure. That mattered little while the
# only thing we did was settle for Turnstile (which does disappear), and matters
# a great deal now that tiers actually produce tokens.
_RESPONSE_FIELD_JS = r"""
(kind) => {
  const names = {
    turnstile: ['cf-turnstile-response', 'g-recaptcha-response'],
    hcaptcha: ['h-captcha-response', 'g-recaptcha-response'],
    'recaptcha-v2': ['g-recaptcha-response'],
    'recaptcha-v3': ['g-recaptcha-response'],
    funcaptcha: ['fc-token', 'verification-token', 'FunCaptcha-Token'],
    arkose: ['fc-token', 'verification-token', 'FunCaptcha-Token'],
    geetest: ['geetest_validate', 'geetest_seccode'],
  }[kind] || ['g-recaptcha-response'];
  for (const name of names) {
    for (const el of document.querySelectorAll('[name="' + name + '"]')) {
      if (el.value && el.value.length > 0) return el.value;
    }
  }
  return '';
}
"""

# Inject a solved token into the page's response field and best-effort
# submit. Parameterised by (kind, token).
_INJECT_JS = r"""
([kind, token]) => {
  const setField = (name) => {
    document.querySelectorAll('[name="' + name + '"]').forEach((n) => {
      n.value = token;
      n.dispatchEvent(new Event('input', { bubbles: true }));
      n.dispatchEvent(new Event('change', { bubbles: true }));
    });
  };
  if (kind === 'turnstile') {
    setField('cf-turnstile-response');
    setField('g-recaptcha-response');
  } else if (kind === 'hcaptcha') {
    setField('h-captcha-response');
    setField('g-recaptcha-response');
  } else if (kind === 'funcaptcha' || kind === 'arkose') {
    setField('fc-token');
    setField('verification-token');
    setField('FunCaptcha-Token');
  } else if (kind === 'geetest') {
    setField('geetest_challenge');
    setField('geetest_validate');
    setField('geetest_seccode');
  } else if (kind === 'image') {
    // The answer is text the user would have typed, so it goes in the field that
    // named itself a captcha rather than a fixed well-known response input.
    const field = document.querySelector('input[name*="captcha" i], input[id*="captcha" i]');
    if (field) {
      field.value = token;
      field.dispatchEvent(new Event('input', { bubbles: true }));
      field.dispatchEvent(new Event('change', { bubbles: true }));
    }
  } else {
    setField('g-recaptcha-response');
  }
  return true;
}
"""

# Kinds whose token is portable across browser contexts, so a solver-provided
# token injects cleanly. Turnstile is excluded on purpose: its token is bound to
# the environment that requested it. DataDome and AWS WAF are excluded because
# they are cleared by a *cookie* the solver returns, not a form field — injecting
# their token into the DOM does nothing.
_PORTABLE_KINDS = frozenset(
    {"hcaptcha", "recaptcha-v2", "recaptcha-v3", "funcaptcha", "arkose", "geetest", "image"}
)
# Kinds where the solver's result is a cookie to set, not a field to fill.
_COOKIE_KINDS = frozenset({"datadome", "aws-waf"})

# Kinds that begin as a clickable checkbox in an "anchor" iframe. Both of these
# frequently pass on the click alone when the fingerprint is good, and only fall
# back to an image grid when it isn't — so clicking is the cheapest tier there is
# and nothing in this module used to do it.
_CHECKBOX_KINDS = frozenset({"recaptcha-v2", "hcaptcha"})

# The anchor iframe holds the checkbox; the bframe holds the image grid that
# appears if the click is not accepted. Matching the anchor specifically matters:
# clicking inside the bframe would hit a tile, not the checkbox.
_ANCHOR_FRAME_PATTERNS: dict[str, tuple[str, ...]] = {
    "recaptcha-v2": ("recaptcha/api2/anchor", "recaptcha/enterprise/anchor"),
    "hcaptcha": ("hcaptcha.com/captcha", "newassets.hcaptcha.com"),
}
_CHECKBOX_SELECTORS: dict[str, tuple[str, ...]] = {
    "recaptcha-v2": ("#recaptcha-anchor", ".recaptcha-checkbox-border"),
    "hcaptcha": ("#checkbox", "#anchor .check", "div[role='checkbox']"),
}


class DetectedChallenge(NamedTuple):
    """A captcha widget found on the page, with everything its solver needs.

    ``extra`` holds the per-kind parameters a sitekey cannot express (DataDome's
    challenge URL, AWS WAF's ``gokuProps``, GeeTest's nonce). Empty values are
    stripped, so a solver can pass it straight through to a task payload.
    """

    kind: CaptchaKind
    site_key: str
    extra: dict[str, str]


async def detect_challenge_detail(page: Any) -> DetectedChallenge | None:
    """Full detection result, including the per-kind ``extra`` parameters.

    Prefer this over :func:`detect_challenge` when the result feeds a solver:
    DataDome, AWS WAF, GeeTest and image captchas cannot be solved from a
    ``(kind, site_key)`` pair alone.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = await evaluate(_DETECT_JS)
    except Exception as exc:  # page closed / navigation in flight
        _logger.debug("agent.captcha_dom.detect_failed", error=str(exc))
        return None
    if not isinstance(result, dict):
        return None
    kind = result.get("kind")
    if not kind:
        return None
    raw_extra = result.get("extra")
    extra = (
        {str(k): str(v) for k, v in raw_extra.items() if v} if isinstance(raw_extra, dict) else {}
    )
    return DetectedChallenge(kind, str(result.get("site_key") or ""), extra)


async def detect_challenge(page: Any) -> tuple[CaptchaKind, str] | None:
    """Return ``(kind, site_key)`` if a known captcha widget is present, else ``None``.

    Kept as the narrow public shape. See :func:`detect_challenge_detail` for the
    ``extra`` parameters that several kinds require in order to actually solve.
    """
    detail = await detect_challenge_detail(page)
    if detail is None:
        return None
    return detail.kind, detail.site_key


async def inject_token(page: Any, kind: CaptchaKind, token: str) -> None:
    """Inject a solved ``token`` into the page's challenge-response field."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return
    try:
        await evaluate(_INJECT_JS, [kind, token])
    except Exception as exc:
        _logger.warning("agent.captcha_dom.inject_failed", kind=kind, error=str(exc))


async def _settle_and_recheck(
    page: Any, settle_s: float, *, reload: bool, kind: CaptchaKind | None = None
) -> bool:
    """Wait for a challenge to clear on its own; optionally reload first.

    Returns whether the challenge is satisfied afterwards. With ``kind``, that
    means "token present **or** widget gone"; without it, only "widget gone" —
    the latter is right for a JS interstitial, which has no response field.
    """
    wait = getattr(page, "wait_for_timeout", None)
    if callable(wait):
        try:
            await wait(settle_s * 1000.0)
        except Exception as exc:  # pragma: no cover — defensive
            _logger.debug("agent.captcha_dom.settle_wait_failed", error=str(exc))
    if reload:
        reloader = getattr(page, "reload", None)
        if callable(reloader):
            try:
                await reloader()
            except Exception as exc:  # pragma: no cover — defensive
                _logger.debug("agent.captcha_dom.reload_failed", error=str(exc))
    if kind is not None:
        return await _is_solved(page, kind)
    return await detect_challenge(page) is None


async def read_response_token(page: Any, kind: CaptchaKind) -> str:
    """Return the challenge-response token currently on the page, or ``""``.

    The authoritative "did it work" signal. See :data:`_RESPONSE_FIELD_JS` for why
    widget-absence is the wrong question for every kind except Turnstile.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return ""
    try:
        return str(await evaluate(_RESPONSE_FIELD_JS, kind) or "")
    except Exception as exc:  # page closed / navigation in flight
        _logger.debug("agent.captcha_dom.read_token_failed", kind=kind, error=str(exc))
        return ""


async def _is_solved(page: Any, kind: CaptchaKind) -> bool:
    """Whether ``kind`` is satisfied — token present, or the widget itself gone.

    Both are accepted because the two families behave differently: Turnstile and
    JS interstitials clear themselves out of the DOM, while reCAPTCHA/hCaptcha
    keep their widget and signal success only through the response field.
    """
    if await read_response_token(page, kind):
        return True
    return await detect_challenge(page) is None


def find_anchor_frame(page: Any, kind: CaptchaKind) -> Any:
    """Return the checkbox ("anchor") iframe for ``kind``, or ``None``.

    Deliberately excludes the *bframe* — the sibling iframe holding the image
    grid — because a click landing there would hit a tile rather than the
    checkbox.
    """
    patterns = _ANCHOR_FRAME_PATTERNS.get(kind, ())
    if not patterns:
        return None
    for frame in getattr(page, "frames", None) or ():
        frame_url = str(getattr(frame, "url", "") or "")
        if "bframe" in frame_url:
            continue
        if any(pattern in frame_url for pattern in patterns):
            return frame
    return None


async def click_checkbox(page: Any, kind: CaptchaKind, *, settle_s: float = 8.0) -> bool:
    """Click the reCAPTCHA/hCaptcha checkbox. Returns whether it solved the challenge.

    The cheapest tier in the cascade and the one that was missing entirely: both
    kinds present a checkbox first, and on a good stealth fingerprint the click
    alone is often accepted, with the image grid appearing only when it isn't.
    Nothing here ever clicked, and ``CamoufoxAutoSolver.supported`` is
    ``{"turnstile"}``, so these kinds skipped tier 0 and went straight to a paid
    solver — paying for challenges that a click would have cleared.

    A ``False`` return is not a failure to escalate on: it usually means the grid
    has now appeared, which is precisely what the vision solver wants.
    """
    frame = find_anchor_frame(page, kind)
    if frame is None:
        _logger.debug("agent.captcha_dom.no_anchor_frame", kind=kind)
        return False
    for selector in _CHECKBOX_SELECTORS.get(kind, ()):
        try:
            element = await frame.wait_for_selector(selector, timeout=3000)
        except Exception as exc:  # selector absent in this variant
            _logger.debug(
                "agent.captcha_dom.checkbox_selector_miss",
                kind=kind,
                selector=selector,
                error=str(exc),
            )
            continue
        if element is None:
            continue
        try:
            await element.click()
        except Exception as exc:
            _logger.debug("agent.captcha_dom.checkbox_click_failed", kind=kind, error=str(exc))
            continue
        _logger.info("agent.captcha_dom.checkbox_clicked", kind=kind, selector=selector)
        # The token is minted asynchronously after the click is accepted, so poll
        # rather than sleeping the whole settle window — a good fingerprint is
        # usually through in well under a second.
        return await _await_token(page, kind, settle_s)
    return False


async def _await_token(page: Any, kind: CaptchaKind, timeout_s: float) -> bool:
    """Poll for the response token until ``timeout_s`` elapses."""
    wait = getattr(page, "wait_for_timeout", None)
    deadline = max(1, int(timeout_s / _TOKEN_POLL_S))
    for _ in range(deadline):
        if await read_response_token(page, kind):
            return True
        if callable(wait):
            try:
                await wait(_TOKEN_POLL_S * 1000.0)
            except Exception:  # pragma: no cover — page closed mid-poll
                break
        else:
            break
    return bool(await read_response_token(page, kind))


_TOKEN_POLL_S = 0.5


async def _fetch_image_b64(page: Any, image_url: str) -> str:
    """Base64 of the captcha image, fetched from inside the page. ``""`` on failure."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return ""
    try:
        return str(await evaluate(_IMAGE_B64_JS, image_url) or "")
    except Exception as exc:
        _logger.warning("agent.captcha_dom.image_fetch_failed", url=image_url, error=str(exc))
        return ""


async def _handle_widgetless_interstitial(page: Any, url: str, *, settle_s: float) -> bool:
    """Settle-and-reload a JS interstitial that has no captcha widget to solve.

    A Cloudflare "Just a moment…" page is **not** a captcha — there is no widget,
    so detection correctly returns ``None``. But the old code then returned
    immediately, which meant the stealth auto-pass tier was never invoked for the
    single most common wall on the web: a live run recorded tier 0 and tier 1 both
    "failing" a CF interstitial when in fact neither had been called.

    Settling is exactly the mechanism that clears these — the page runs its
    challenge JS and reloads itself — so it is worth the wait even though no
    solver is involved. When it does not clear, the honest answer is still "not
    handled", and the caller escalates.
    """
    html = ""
    content = getattr(page, "content", None)
    if callable(content):
        try:
            html = str(await content() or "")
        except Exception as exc:  # pragma: no cover — defensive
            _logger.debug("agent.captcha_dom.content_failed", error=str(exc))
    status = getattr(page, "status", None)
    vendor = is_interstitial(html, status if isinstance(status, int) else 200)
    if vendor is None:
        return False

    _logger.info("agent.captcha_dom.interstitial_no_widget", vendor=vendor, url=url)
    cleared = await _settle_and_recheck(page, settle_s, reload=True)
    if cleared:
        # `detect_challenge` finding nothing is not proof the wall is gone — it
        # never saw a widget in the first place. Re-read the document.
        if callable(content):
            try:
                html = str(await content() or "")
            except Exception as exc:  # pragma: no cover — defensive
                _logger.debug("agent.captcha_dom.content_failed", error=str(exc))
        cleared = is_interstitial(html, 200) is None
    _logger.info(
        "agent.captcha_dom.interstitial_settle_result", vendor=vendor, url=url, cleared=cleared
    )
    return cleared


async def solve_on_page(  # noqa: PLR0911 — one return per tier; flattening hurts
    page: Any,
    solver: CaptchaSolver,
    url: str,
    *,
    settle_s: float = 8.0,
    vision: LLMBackend | None = None,
) -> bool:
    """Detect and handle a captcha on ``page``. Returns whether one was handled.

    Mechanism-aware, cheapest-first:

    1. **settle** — a stealth browser clears most Turnstile challenges by waiting.
    2. **checkbox** — reCAPTCHA v2 / hCaptcha often pass on the click alone.
    3. **local vision** (when ``vision`` is given) — solve the image grid with a
       local VLM; free, and the page never leaves this machine.
    4. **solver token** — the paid tiers, injected only where the token is
       portable, or applied as a cookie for DataDome / AWS WAF.

    Never raises: solver failures are logged and reported as "not handled" so the
    agent loop continues and the cascade escalates.
    """
    detected = await detect_challenge_detail(page)
    if detected is None:
        return await _handle_widgetless_interstitial(page, url, settle_s=settle_s)
    kind, site_key, extra = detected
    _logger.info(
        "agent.captcha_dom.detected", kind=kind, url=url, has_site_key=bool(site_key), extra=extra
    )

    # 1) Stealth auto-pass — most reliable for Turnstile, costs nothing.
    if await _settle_and_recheck(page, settle_s, reload=False, kind=kind):
        _logger.info("agent.captcha_dom.cleared_by_settle", kind=kind, url=url)
        return True

    # 2) Free interaction — click the checkbox. reCAPTCHA v2 and hCaptcha both
    # start as a checkbox that frequently passes outright on a good stealth
    # fingerprint, escalating to an image grid only if it does not. Costs one
    # click and no API credit, so it belongs ahead of any solver.
    if kind in _CHECKBOX_KINDS and await click_checkbox(page, kind, settle_s=settle_s):
        _logger.info("agent.captcha_dom.cleared_by_checkbox", kind=kind, url=url)
        return True

    # 3) Local vision — the checkbox was refused, so an image grid is up now.
    # Free, and the page never leaves this machine, so it goes ahead of the paid
    # tier; a False here simply falls through to it.
    if vision is not None:
        from scrapper_tool.agent.backends.captcha_vision import (  # noqa: PLC0415
            SUPPORTED_KINDS,
            solve_grid,
        )

        if kind in SUPPORTED_KINDS and await solve_grid(page, kind, vision, settle_s=settle_s):
            _logger.info("agent.captcha_dom.cleared_by_vision", kind=kind, url=url)
            return True

    # An image captcha is identified by its pixels, not a key, so the bytes are
    # the payload. Fetched in page context — see _IMAGE_B64_JS.
    if kind == "image" and "image_url" in extra:
        body = await _fetch_image_b64(page, extra["image_url"])
        if body:
            extra = {**extra, "body": body}

    # 2) Ask the solver cascade for a token.
    try:
        token = await solver.solve(kind, site_key, url, extra=extra or None)
    except CaptchaSolveError as exc:
        _logger.warning("agent.captcha_dom.solver_failed", kind=kind, error=str(exc))
        return False

    if not token:
        # Solver only settled (tier-0 empty token) and it didn't clear.
        # A reload gives the stealth browser one more chance.
        cleared = await _settle_and_recheck(page, settle_s, reload=True, kind=kind)
        _logger.info("agent.captcha_dom.empty_token_recheck", kind=kind, cleared=cleared)
        return cleared

    # 3) Apply the result. What "apply" means is per-kind.
    if kind in _COOKIE_KINDS:
        # DataDome and AWS WAF clear via a cookie, not a form field. Setting it on
        # the context is the whole mechanism — injecting into the DOM would look
        # like it worked and change nothing.
        applied = await _set_clearance_cookie(page, kind, token, url)
        cleared = applied and await _settle_and_recheck(page, settle_s, reload=True)
        _logger.info(
            "agent.captcha_dom.cookie_applied", kind=kind, applied=applied, cleared=cleared
        )
        return applied

    if kind not in _PORTABLE_KINDS:
        _logger.warning(
            "agent.captcha_dom.foreign_token",
            kind=kind,
            detail="injecting an out-of-context token; may fail the environment check",
        )
    await inject_token(page, kind, token)
    # Report what actually happened. This used to return True unconditionally, so
    # a token that never landed in the response field — the normal outcome for a
    # foreign Turnstile token failing its environment check — was reported as a
    # solve, and the cascade stopped escalating on a challenge that was still up.
    solved = await _settle_and_recheck(page, 2.0, reload=False, kind=kind)
    _logger.info("agent.captcha_dom.injected", kind=kind, solved=solved)
    return solved


_CLEARANCE_COOKIE_NAMES: dict[str, str] = {"datadome": "datadome", "aws-waf": "aws-waf-token"}


async def _set_clearance_cookie(page: Any, kind: str, token: str, url: str) -> bool:
    """Set the clearance cookie a DataDome / AWS-WAF solve returns. False if we can't."""
    context = getattr(page, "context", None)
    add_cookies = getattr(context, "add_cookies", None)
    if not callable(add_cookies):
        _logger.warning("agent.captcha_dom.no_cookie_api", kind=kind)
        return False
    try:
        await add_cookies([{"name": _CLEARANCE_COOKIE_NAMES[kind], "value": token, "url": url}])
    except Exception as exc:
        _logger.warning("agent.captcha_dom.cookie_set_failed", kind=kind, error=str(exc))
        return False
    return True


def make_captcha_consumer(solver: CaptchaSolver, *, vision: LLMBackend | None = None) -> Any:
    """Build a page-hook consumer that solves captchas via ``solver``.

    Shape matches :mod:`scrapper_tool.agent.backends.page_hooks` consumers:
    ``async def (page, *, url) -> None``. Used by both E1 (``after_goto``)
    and E2 (``on_step_end``).
    """

    async def captcha_consumer(page: Any, *, url: str) -> None:
        await solve_on_page(page, solver, url, vision=vision)

    return captcha_consumer


__all__ = [
    "DetectedChallenge",
    "click_checkbox",
    "detect_challenge",
    "detect_challenge_detail",
    "find_anchor_frame",
    "inject_token",
    "make_captcha_consumer",
    "read_response_token",
    "solve_on_page",
]

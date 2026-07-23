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

from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger
from scrapper_tool.errors import CaptchaSolveError

if TYPE_CHECKING:
    from scrapper_tool.agent.backends.captcha import CaptchaKind, CaptchaSolver

_logger = get_logger(__name__)

# One evaluate() call classifies the challenge and reads its sitekey.
# Returns ``{"kind": ..., "site_key": ...}`` or ``null``.
_DETECT_JS = r"""
() => {
  const pick = (sel) => document.querySelector(sel);
  // Cloudflare Turnstile
  let el = pick('.cf-turnstile[data-sitekey]') || pick('[data-sitekey][data-action]');
  if (el && el.className && el.className.indexOf('cf-turnstile') !== -1) {
    return { kind: 'turnstile', site_key: el.getAttribute('data-sitekey') || '' };
  }
  if (document.querySelector('iframe[src*="challenges.cloudflare.com"]')) {
    const k = pick('.cf-turnstile[data-sitekey]');
    return { kind: 'turnstile', site_key: k ? (k.getAttribute('data-sitekey') || '') : '' };
  }
  // hCaptcha
  el = pick('.h-captcha[data-sitekey]');
  if (el || document.querySelector('iframe[src*="hcaptcha.com"]')) {
    return { kind: 'hcaptcha', site_key: el ? (el.getAttribute('data-sitekey') || '') : '' };
  }
  // reCAPTCHA v2
  el = pick('.g-recaptcha[data-sitekey]');
  if (el || document.querySelector('iframe[src*="google.com/recaptcha"]')) {
    return { kind: 'recaptcha-v2', site_key: el ? (el.getAttribute('data-sitekey') || '') : '' };
  }
  return null;
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
  } else {
    setField('g-recaptcha-response');
  }
  return true;
}
"""

_PORTABLE_KINDS = frozenset({"hcaptcha", "recaptcha-v2", "recaptcha-v3"})


async def detect_challenge(page: Any) -> tuple[CaptchaKind, str] | None:
    """Return ``(kind, site_key)`` if a known captcha widget is present, else ``None``."""
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
    site_key = result.get("site_key") or ""
    if not kind:
        return None
    return kind, str(site_key)


async def inject_token(page: Any, kind: CaptchaKind, token: str) -> None:
    """Inject a solved ``token`` into the page's challenge-response field."""
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return
    try:
        await evaluate(_INJECT_JS, [kind, token])
    except Exception as exc:
        _logger.warning("agent.captcha_dom.inject_failed", kind=kind, error=str(exc))


async def _settle_and_recheck(page: Any, settle_s: float, *, reload: bool) -> bool:
    """Wait for a challenge to clear on its own; optionally reload first.

    Returns ``True`` if no challenge is detected afterwards.
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
    return await detect_challenge(page) is None


async def solve_on_page(
    page: Any,
    solver: CaptchaSolver,
    url: str,
    *,
    settle_s: float = 8.0,
) -> bool:
    """Detect and handle a captcha on ``page``. Returns whether one was handled.

    Mechanism-aware: stealth-settle first, then solver token, injection
    only where the token is portable. Never raises — solver failures are
    logged and reported as "not handled" so the agent loop continues.
    """
    detected = await detect_challenge(page)
    if detected is None:
        return False
    kind, site_key = detected
    _logger.info("agent.captcha_dom.detected", kind=kind, url=url)

    # 1) Stealth auto-pass — most reliable for Turnstile, costs nothing.
    if await _settle_and_recheck(page, settle_s, reload=False):
        _logger.info("agent.captcha_dom.cleared_by_settle", kind=kind, url=url)
        return True

    # 2) Ask the solver cascade for a token.
    try:
        token = await solver.solve(kind, site_key, url)
    except CaptchaSolveError as exc:
        _logger.warning("agent.captcha_dom.solver_failed", kind=kind, error=str(exc))
        return False

    if not token:
        # Solver only settled (tier-0 empty token) and it didn't clear.
        # A reload gives the stealth browser one more chance.
        cleared = await _settle_and_recheck(page, settle_s, reload=True)
        _logger.info("agent.captcha_dom.empty_token_recheck", kind=kind, cleared=cleared)
        return cleared

    # 3) Inject the token. For Turnstile this is a low-confidence path
    # (environment-bound token); portable kinds inject cleanly.
    if kind == "turnstile" and "turnstile" not in _PORTABLE_KINDS:
        _logger.warning(
            "agent.captcha_dom.turnstile_foreign_token",
            detail="injecting an out-of-context Turnstile token; may fail env check",
        )
    await inject_token(page, kind, token)
    cleared = await _settle_and_recheck(page, 2.0, reload=False)
    _logger.info("agent.captcha_dom.injected", kind=kind, cleared=cleared)
    return True


def make_captcha_consumer(solver: CaptchaSolver) -> Any:
    """Build a page-hook consumer that solves captchas via ``solver``.

    Shape matches :mod:`scrapper_tool.agent.backends.page_hooks` consumers:
    ``async def (page, *, url) -> None``. Used by both E1 (``after_goto``)
    and E2 (``on_step_end``).
    """

    async def captcha_consumer(page: Any, *, url: str) -> None:
        await solve_on_page(page, solver, url)

    return captcha_consumer


__all__ = [
    "detect_challenge",
    "inject_token",
    "make_captcha_consumer",
    "solve_on_page",
]

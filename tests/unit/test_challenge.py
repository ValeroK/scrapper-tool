"""Unit tests for the shared bot-wall / content-quality detector.

The two headline cases are taken from real sites, because both are ones the
signal-only classifier gets wrong:

- ``store.mopar.com``: HTTP **403** with 1.35 MB of genuine DOM (anti-bot 403s the
  document, JS clears it, real page renders) -> must be treated as SUCCESS.
- ``one.co.il``: HTTP **200**, 419 KB, JSON-LD present, but headings are literal
  ``{displayTitle}`` placeholders -> must be FLAGGED as worth escalating (rendering
  the same URL yields 212 real headlines vs 4).

Getting the first backwards poisons proxy health on every good render. The second
is advisory only — measured against real pages, placeholders proved a weak
discriminator, so it hints at escalation rather than rejecting content.
"""

from __future__ import annotations

import pytest

from scrapper_tool._challenge import (
    has_real_content,
    is_cf_challenge_body,
    is_interstitial,
    looks_like_spa_shell,
    looks_unhydrated,
)

# A big, ordinary page with real heading text. Must exceed the 50 KB
# "challenge pages are tiny" threshold so the 403 case is exercised properly.
_REAL_PAGE = (
    "<html><head><title>2016 Jeep Wrangler | Mopar</title></head><body>"
    + ("<h2>Select Parts Category</h2><h3>Brakes</h3><h3>Filters</h3><p>x</p>" * 1000)
    + "</body></html>"
)

# Large page whose headings never rendered (the one.co.il shape). Must exceed the
# 30 KB SPA-shell cap to prove the hydration check isn't size-limited.
_UNHYDRATED_PAGE = (
    "<html><body>"
    + ("<h2>{displayTitle}</h2><h3>{subTitle}</h3><div>filler</div>" * 800)
    + "</body></html>"
)


# --- vendor interstitials -------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("<title>Just a moment...</title>", "cloudflare"),
        ("challenges.cloudflare.com/turnstile", "cloudflare"),
        ("redirecting to validate.perfdrive.com/x", "radware"),
        ("<h1>Are you for real</h1>", "radware"),
        ("support@shieldsquare.com", "radware"),
        ("geo.captcha-delivery.com/captcha", "datadome"),
        ("<div id=px-captcha></div>", "perimeterx"),
        ("Incapsula incident ID: 123", "incapsula"),
    ],
)
def test_detects_each_vendor(marker: str, expected: str) -> None:
    assert is_interstitial(f"<html><body>{marker}</body></html>", 200) == expected


def test_small_block_status_is_flagged_even_without_a_known_marker() -> None:
    assert is_interstitial("<html><body>denied</body></html>", 403) == "unknown"


def test_genuine_page_is_not_an_interstitial() -> None:
    assert is_interstitial(_REAL_PAGE, 200) is None


def test_empty_body_is_not_an_interstitial() -> None:
    assert is_interstitial("", 403) is None


# --- the Mopar case: 403 + real content -----------------------------------


def test_large_body_with_block_status_is_not_a_wall() -> None:
    """store.mopar.com serves real content under a 403. Size is the discriminator."""
    assert len(_REAL_PAGE) > 50_000
    assert is_interstitial(_REAL_PAGE, 403) is None


def test_has_real_content_true_for_403_with_real_dom() -> None:
    # The regression guard for the proxy-health bug: a good render under a 403
    # must NOT be counted as a block.
    assert has_real_content(_REAL_PAGE, 403) is True


# --- the one.co.il case: 200 + unhydrated ---------------------------------


def test_unhydrated_headings_detected() -> None:
    assert looks_unhydrated(_UNHYDRATED_PAGE) is True


def test_unhydrated_detection_is_not_size_capped() -> None:
    """The SPA-shell check caps at 30 KB; the real page was 419 KB and slipped past."""
    assert len(_UNHYDRATED_PAGE) > 30_000
    assert looks_like_spa_shell(_UNHYDRATED_PAGE) is False  # too big for that check
    assert looks_unhydrated(_UNHYDRATED_PAGE) is True  # but still caught


def test_hydration_is_advisory_not_a_rejection_gate() -> None:
    """`has_real_content` must NOT reject on hydration alone.

    Measured against real pages, placeholders are a weak discriminator: a fully
    rendered page still carried 5 leftover placeholders, and the unhydrated page
    still had 19 genuine headings. Gating on it would discard good content and
    penalise a healthy proxy — so hydration is a hint to escalate, not a reject.
    """
    assert looks_unhydrated(_UNHYDRATED_PAGE) is True  # flagged as worth escalating
    assert has_real_content(_UNHYDRATED_PAGE, 200) is True  # but NOT rejected


def test_unhydrated_uses_a_ratio_not_an_absolute_count() -> None:
    """A big rendered page with a few stray placeholders must not be flagged.

    Real data: one.co.il rendered = 5 placeholder headings out of 248 (ratio 0.02)
    and is good; unhydrated = 5 of 26 (ratio 0.19) and is bad. An absolute
    threshold of 3 would wrongly condemn the good page.
    """
    mostly_real = (
        "<html><body>"
        + ("<h2>Real Headline Here</h2>" * 200)
        + ("<h2>{leftover}</h2>" * 5)
        + "</body></html>"
    )
    assert looks_unhydrated(mostly_real) is False


def test_real_page_is_not_unhydrated() -> None:
    assert looks_unhydrated(_REAL_PAGE) is False


def test_single_stray_placeholder_is_tolerated() -> None:
    """One templated heading shouldn't condemn an otherwise-real page."""
    html = "<html><body><h1>Real Title</h1><h2>{leftover}</h2><h3>Also Real</h3></body></html>"
    assert looks_unhydrated(html) is False


def test_inline_js_braces_do_not_trip_hydration_check() -> None:
    """Detection is scoped to heading text, so ordinary JS objects are ignored."""
    html = "<html><body><h1>Real</h1><script>var a={foo};var b={bar};var c={baz};</script></body></html>"
    assert looks_unhydrated(html) is False


# --- Cloudflare-only variant (Pattern D's solve_cloudflare probe) ----------


def test_cf_probe_stays_cloudflare_specific() -> None:
    """Must NOT broaden: Scrapling's solve_cloudflare is CF-specific."""
    cf = "<html><title>Just a moment...</title></html>"
    radware = "<html><body>validate.perfdrive.com</body></html>"
    assert is_cf_challenge_body(cf, 200) is True
    assert is_cf_challenge_body(radware, 200) is False


def test_cf_probe_flags_small_block_status_bodies() -> None:
    assert is_cf_challenge_body("<html>tiny</html>", 503) is True


def test_spa_shell_detection_preserved() -> None:
    assert looks_like_spa_shell('<html><body><div id="__next"></div></body></html>') is True
    assert looks_like_spa_shell(_REAL_PAGE) is False

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

from pathlib import Path

import pytest

from scrapper_tool._challenge import (
    has_real_content,
    is_cf_challenge_body,
    is_interstitial,
    looks_like_content_free_shell,
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


# --- bot-walled HTTP 200 (live-validation bug #1) --------------------------
#
# Three bodies captured live from www.dickssportinggoods.com on 2026-08-12, one
# host serving all three, which is what makes them a useful calibration set: the
# TLS profile alone decides which you get (chrome124 -> the wall, chrome -> the
# shell). See docs/research/2026-07-live-validation.md.

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "challenge"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_akamai_behavioral_wall_served_as_200_is_detected() -> None:
    """The headline bug: a 200-status Akamai wall reported as real content.

    2,373 bytes of hidden tile-challenge container. Rendering the same URL gives
    330 KB, so accepting this stops the cascade escalating and hands the caller a
    bot wall. Was ``is_interstitial -> None`` / ``has_real_content -> True``.
    """
    html = _fixture("akamai_behavioral_200.html")
    assert is_interstitial(html, 200) == "akamai"
    assert has_real_content(html, 200) is False


def test_akamai_maintenance_wall_is_caught_but_stays_unnamed() -> None:
    """Same host and wall family, but this variant is honestly un-attributable.

    A soft-block styled as "Oops, Something Went Wrong." Its *only* Akamai-specific
    content is the sensor ``<script src>``, which the good shell above also
    carries — so there is no marker that names the vendor without flagging real
    pages. It is caught by the status gate as an anonymous ``unknown``, which is
    the correct outcome, and this test exists to pin that we did not "fix" it by
    reaching for the sensor path. The wording ("oops, something went wrong") is far
    too generic to promote to a signature.
    """
    html = _fixture("akamai_maintenance_403.html")
    assert is_interstitial(html, 403) == "unknown"
    assert has_real_content(html, 403) is False


def test_akamai_protected_real_page_is_not_flagged() -> None:
    """The control, and the reason the sensor script is not a signature.

    A genuine Angular shell from the same host, carrying the same Akamai sensor
    ``<script src>`` and a ``sec-overlay``/``sec-container`` pair. Only the
    challenge-specific ids distinguish it. Flagging this would mean flagging every
    Akamai-protected page on the internet.
    """
    html = _fixture("akamai_protected_real_shell_200.html")
    assert is_interstitial(html, 200) is None
    assert has_real_content(html, 200) is True


def test_content_free_shell_catches_unsignatured_200_wall() -> None:
    """A novel 200 wall with no vendor signature still must not pass as content."""
    html = '<html lang="en"><body><script src="/x.js"></script><div id="q"></div></body></html>'
    assert looks_like_content_free_shell(html) is True
    assert is_interstitial(html, 200) == "unknown"


@pytest.mark.parametrize(
    ("name", "html"),
    [
        # A real document has a title even when its body is JS-built.
        (
            "titled_spa_shell",
            '<html><head><title>Shop</title></head><body><script src="/a.js"></script><div id="root"></div></body></html>',
        ),
        # example.com: 559 B of genuine content, no script at all.
        (
            "no_script",
            "<html><head><title>Example Domain</title></head><body><h1>Example Domain</h1><p>This domain is for use in illustrative examples.</p></body></html>",
        ),
        # Short but genuinely readable.
        (
            "real_text",
            "<html><body><script src=/a.js></script><p>"
            + ("Real sentence about a product. " * 20)
            + "</p></body></html>",
        ),
    ],
)
def test_content_free_shell_does_not_flag_real_pages(name: str, html: str) -> None:
    assert looks_like_content_free_shell(html) is False, name


def test_content_free_shell_ignores_large_bodies() -> None:
    """Guard the cheap path: the fallback is for tiny documents only."""
    assert (
        looks_like_content_free_shell("<html><body><script></script>" + "<div></div>" * 5000)
        is False
    )


@pytest.mark.parametrize(
    ("name", "html"),
    [
        # The real regression: a page whose entire payload is a JSON-LD block and
        # whose <body> is filled in by JS. No title, no visible text, tiny — so it
        # matched the shell heuristic exactly, and flagging it made the cascade
        # skip Pattern D for a page that was never walled.
        (
            "json_ld_only",
            '<html><head><script type="application/ld+json">'
            '{"@type":"Product","name":"Widget"}</script></head><body></body></html>',
        ),
        (
            "microdata_only",
            '<html><body itemscope itemtype="https://schema.org/Product">'
            '<script src="/a.js"></script></body></html>',
        ),
        (
            "open_graph_only",
            '<html><head><meta property="og:title" content="Widget"></head>'
            "<body><script src=/a.js></script></body></html>",
        ),
    ],
)
def test_structured_data_is_never_a_content_free_shell(name: str, html: str) -> None:
    """A bot wall does not publish schema.org or Open Graph markup.

    Structured data is proof the response was meant as content, which is the
    escape hatch that keeps the titleless-shell heuristic from condemning pages
    that render their body client-side.
    """
    assert looks_like_content_free_shell(html) is False, name
    assert is_interstitial(html, 200) is None, name
    assert has_real_content(html, 200) is True, name

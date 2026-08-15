"""Real-browser tests for the captcha detection JS.

The rest of the captcha suite drives ``detect_challenge`` with a fake page whose
``evaluate`` returns canned dicts — useful for the orchestration logic, but it
never executes ``_DETECT_JS``, so the detector itself was effectively untested.
That is how it shipped with three return paths against ten declared
``CaptchaKind`` values, leaving seven unreachable by any automatic path.

These load synthetic markup into a real browser with ``set_content`` and run the
actual JS. No network is involved; the markup mirrors shapes captured from live
pages (see ``docs/research/2026-07-live-validation.md`` for which kinds are
additionally verified against the live sites).

Everything runs inside a single test because pytest-asyncio's ``auto`` mode gives
each test function its own event loop, so a browser shared through a
module-scoped async fixture deadlocks. One launch, one table, and every case is
evaluated before the assert so a failure reports all of them rather than only the
first.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from scrapper_tool._extras import browser_binary_present
from scrapper_tool.agent.backends.captcha_dom import detect_challenge_detail

pytestmark = pytest.mark.skipif(
    not browser_binary_present("camoufox"),
    reason="needs a Camoufox binary (`camoufox fetch`)",
)

# (label, body_html, expected_kind, expected_site_key, expected_extra_subset)
#
# ``expected_kind`` of None means "must detect nothing".
#
# Every ``<script>`` carrying a ``src`` is marked ``type="text/plain"`` on
# purpose. The browser does not fetch or execute a script of unknown type, while
# ``script[src*="…"]`` — which is how the detector finds all of these, see
# ``_DETECT_JS`` — matches on the attribute and is completely unaffected. The
# inline ``gokuProps`` script deliberately keeps no type, because that one must
# still run.
#
# Without it, a second remote script load in the same browser hangs
# `set_content` until it times out. Reproduced in the Linux container and
# isolated there: the hang follows *order*, not host or content — reversing
# `_CASES` moves it onto whichever remote-script cases now come later, each of
# which passes when run alone. Neither `route.abort()`, `route.fulfill()` nor
# `context.set_offline(True)` prevents it, so it is in Camoufox's script-loading
# path rather than anything the test can intercept. It never fired on Windows
# and took four CI cycles to pin down.
#
# Not fetching them is the honest fix regardless: it makes this module's "No
# network is involved" claim true, which it was not — these are the live vendor
# URLs, and a `<script src>` blocks DOMContentLoaded, so every run was really
# calling Google, Arkose, GeeTest and AWS from the test suite.
_CASES: list[tuple[str, str, str | None, str, dict[str, str]]] = [
    # --- kinds that already worked; regression guard ---
    (
        "turnstile",
        '<div class="cf-turnstile" data-sitekey="0x4AAA" data-action="login"></div>',
        "turnstile",
        "0x4AAA",
        {"action": "login"},
    ),
    (
        "hcaptcha",
        '<div class="h-captcha" data-sitekey="a5f74b19-9e45"></div>',
        "hcaptcha",
        "a5f74b19-9e45",
        {},
    ),
    (
        "recaptcha-v2 explicit widget",
        '<div class="g-recaptcha" data-sitekey="6Le-wvkS"></div>',
        "recaptcha-v2",
        "6Le-wvkS",
        {},
    ),
    # --- newly reachable ---
    (
        # When the widget is rendered programmatically the anchor iframe's `k=`
        # is the only place the sitekey appears.
        "recaptcha-v2 sitekey from anchor iframe",
        '<iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&k=6LeFRAMEKEY&co=x"></iframe>',
        "recaptcha-v2",
        "6LeFRAMEKEY",
        {},
    ),
    (
        # The ordering bug: v3's invisible badge ALSO renders an api2/anchor
        # iframe, so checking that first reported the live v3 demo as v2 with an
        # empty sitekey — which then routes to the wrong solver task with nothing
        # to solve it with.
        "recaptcha-v3 not misread as v2",
        '<script type="text/plain" src="https://www.google.com/recaptcha/api.js?render=6Lcyqq8oKEY"></script>'
        '<iframe src="https://www.google.com/recaptcha/api2/anchor?ar=1&k=6Lcyqq8oKEY"></iframe>',
        "recaptcha-v3",
        "6Lcyqq8oKEY",
        {},
    ),
    (
        # `render=explicit` means "v2, rendered by JS" — not a v3 key.
        "recaptcha render=explicit is still v2",
        '<script type="text/plain" src="https://www.google.com/recaptcha/api.js?render=explicit"></script>'
        '<div class="g-recaptcha" data-sitekey="6LeV2KEY"></div>',
        "recaptcha-v2",
        "6LeV2KEY",
        {},
    ),
    (
        "funcaptcha via data-pkey",
        '<div id="FunCaptcha" data-pkey="476068BF-9607-4799"></div>',
        "funcaptcha",
        "476068BF-9607-4799",
        {},
    ),
    (
        # Arkose bootstraps from a script whose path carries the key, before any
        # iframe exists — looking only at iframes misses it during setup.
        "arkose public key from script path",
        '<script type="text/plain" src="https://client-api.arkoselabs.com/v2/476068BF-9607-4799-B53D-966BE98E2B81/api.js"></script>',
        "arkose",
        "476068BF-9607-4799-B53D-966BE98E2B81",
        {"surl": "https://client-api.arkoselabs.com"},
    ),
    (
        # Markup shape and the id itself are taken from a live v4 page: the id is
        # in no global, only the loader's query string.
        "geetest v4 captcha_id from loader url",
        '<script type="text/plain" src="https://gcaptcha4.geetest.com/load?callback=geetest_1'
        '&captcha_id=e392e1d7fd421dc63325744d5a2b9c73&"></script>'
        '<div class="geetest_captcha_f0a7e2ce geetest_captcha"></div>',
        "geetest",
        "e392e1d7fd421dc63325744d5a2b9c73",
        {"version": "4"},
    ),
    (
        "geetest v3 gt parameter",
        '<script type="text/plain" src="https://api.geetest.com/get.php?gt=GT3KEY&challenge=CHAL1"></script>'
        '<div class="geetest_holder"></div>',
        "geetest",
        "GT3KEY",
        {"challenge": "CHAL1", "version": "3"},
    ),
    (
        # AWS WAF has no sitekey; the solver needs the gokuProps triple, which a
        # solver given only the page URL cannot reconstruct.
        "aws-waf collects gokuProps",
        "<script>window.gokuProps={key:'KEY1',iv:'IV1',context:'CTX1'};</script>"
        '<script type="text/plain" src="https://abc.us-east-1.captcha.awswaf.com/challenge.js"></script>',
        "aws-waf",
        "",
        {"awsKey": "KEY1", "awsIv": "IV1", "awsContext": "CTX1"},
    ),
    (
        # DataDome is identified by the iframe URL including its cid query.
        "datadome captures challenge url",
        '<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=ABC123&hash=X"></iframe>',
        "datadome",
        "",
        {},
    ),
    (
        "image captcha beside a captcha-named field",
        '<form><img src="/img/captcha.png"><input name="captcha_code"></form>',
        "image",
        "",
        {},
    ),
    # --- negatives ---
    ("ordinary page", "<h1>Products</h1><img src=/logo.png><form></form>", None, "", {}),
    (
        # The image branch is last and deliberately narrow — no captcha field, no
        # match, so a decorative image cannot trip it.
        "decorative image without a captcha field",
        '<form><img src="/captcha-banner.png"><input name="email"></form>',
        None,
        "",
        {},
    ),
    (
        # A JS interstitial is not a captcha. None is correct here; it is handled
        # by the settle path in solve_on_page, which the old code skipped
        # entirely — so tier 0 was never invoked for the commonest wall on the web.
        "cloudflare interstitial has no widget",
        "<h1>Just a moment...</h1><script>challenge()</script>",
        None,
        "",
        {},
    ),
]


_EXTERNAL = re.compile(r"^https?://")
"""Only real remote fetches. Deliberately NOT the ``**/*`` glob.

``**/*`` also matches the ``about:blank`` navigation ``set_content`` performs
internally, so aborting on it hung ``set_content`` itself — deterministically, on
all three matrix rows, which is how it was found. Anchoring on the scheme leaves
every internal navigation untouched while still catching every vendor URL in
``_CASES``, all of which are https.
"""


async def _abort(route: Any) -> None:
    """Fail every external request. See the call site for why.

    No ``resource_type == "document"`` exemption on purpose: an ``<iframe src>``
    request *is* a document, so exempting documents would let the reCAPTCHA and
    DataDome frames fetch for real — the single largest piece of the third-party
    traffic this is here to stop. ``_EXTERNAL`` already keeps ``about:blank`` out
    of scope, so nothing internal needs the exemption.
    """
    await route.abort()


async def test_detection_js_against_real_markup() -> None:
    from camoufox.async_api import AsyncCamoufox

    failures: list[str] = []
    extras: dict[str, dict[str, str]] = {}

    async with AsyncCamoufox(headless=True) as browser:
        for label, body, kind, site_key, extra_subset in _CASES:
            # A fresh page per case on purpose. `set_content` swaps the document
            # but keeps the same `window`, so globals a case sets (AWS WAF's
            # `gokuProps`) would leak into every later case and mask its result.
            page = await browser.new_page()
            # Block the network before setting content. These cases carry the
            # real vendor URLs (google.com/recaptcha, client-api.arkoselabs.com,
            # gcaptcha4.geetest.com, …), and a `<script src>` without async/defer
            # blocks DOMContentLoaded while an `<iframe src>` blocks load — so
            # without this the test actually fetched third-party captcha CDNs on
            # every run. That made it network-flaky (it exhausted a 30s budget on
            # one matrix row and a 90s budget on two others) and put real load on
            # third parties from a build, which is exactly what the header of
            # scripts/e2e/targets.yaml forbids.
            #
            # Aborting costs nothing under test: the detection JS reads DOM
            # attributes — src strings, sitekey params — and never needs the
            # resource behind them. With requests aborted, DOMContentLoaded fires
            # on the markup alone and the assertions are unchanged.
            await page.route(_EXTERNAL, _abort)
            await page.set_content(
                f"<!doctype html><html><body>{body}</body></html>",
                wait_until="domcontentloaded",
            )
            got: Any = await detect_challenge_detail(page)
            await page.close()

            if kind is None:
                if got is not None:
                    failures.append(f"{label}: expected no detection, got {got.kind!r}")
                continue
            if got is None:
                failures.append(f"{label}: expected kind={kind!r}, detected nothing")
                continue
            extras[label] = got.extra
            if got.kind != kind:
                failures.append(f"{label}: kind {got.kind!r} != {kind!r}")
            if got.site_key != site_key:
                failures.append(f"{label}: site_key {got.site_key!r} != {site_key!r}")
            for key, value in extra_subset.items():
                if got.extra.get(key) != value:
                    failures.append(f"{label}: extra[{key!r}] {got.extra.get(key)!r} != {value!r}")

    assert not failures, "detection mismatches:\n  " + "\n  ".join(failures)

    # URL-valued extras are resolved against the document, so assert on the tail
    # rather than pinning the base URL set_content happens to use.
    assert extras["aws-waf collects gokuProps"]["awsChallengeJS"].endswith("/challenge.js")
    assert "initialCid=ABC123" in extras["datadome captures challenge url"]["captchaUrl"]
    assert extras["image captcha beside a captcha-named field"]["image_url"].endswith("captcha.png")

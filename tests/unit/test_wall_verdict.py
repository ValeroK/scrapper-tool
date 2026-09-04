"""One question, one answer, everywhere.

Three times a new kind of wall was met with a new detector, and twice that
detector was then forgotten somewhere it mattered. At the time this was written
three of seven gates were two detectors behind: proxy health was marking a burned
IP healthy, the MCP surface was blind to both walls found that month, and the
captcha DOM probe could not see the wall it existed to clear.

Remembering seven call sites is not a strategy, so the guard below makes
forgetting fail the suite instead of shipping.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from scrapper_tool._challenge import classify_wall

_HOST = "https://www.vendor.test"
_PAGE = f"{_HOST}/parts/68001234AA"

_SIGNATURE_WALL = (
    "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"
)
_HOST_TITLED_WALL = (
    "<html><head><title>www.vendor.test</title></head><body>"
    '<img alt="Icon for www.vendor.test"><h1>www.vendor.test</h1></body></html>'
    "<script>" + ("var p=1;" * 2800) + "</script>"
)
_CAPTCHA_BODY = (
    "<html><head><title>Verification</title></head><body>"
    "<h1>Please confirm you are not a robot</h1></body></html>"
)
_REAL_PAGE = (
    '<html><head><script type="application/ld+json">{"@type":"Product","name":"W"}</script>'
    "</head><body><h1>Widget</h1><p>A real product page with real words on it.</p></body></html>"
)


class TestTheVerdict:
    """Every kind of evidence, through the one entry point."""

    def test_a_vendor_signature(self) -> None:
        verdict = classify_wall(_SIGNATURE_WALL, 200, requested_url=_PAGE, final_url=_PAGE)
        assert verdict.walled
        assert verdict.evidence == "cloudflare"

    def test_a_redirect_onto_a_challenge(self) -> None:
        verdict = classify_wall(
            _CAPTCHA_BODY, 200, requested_url=_PAGE, final_url=f"{_HOST}/captcha.html"
        )
        assert verdict.walled
        assert verdict.evidence == "redirect"

    def test_a_page_that_is_only_its_own_hostname(self) -> None:
        verdict = classify_wall(_HOST_TITLED_WALL, 200, requested_url=_PAGE, final_url=_PAGE)
        assert verdict.walled
        assert verdict.evidence == "host_titled_wall"

    def test_a_real_page_is_not_walled(self) -> None:
        verdict = classify_wall(_REAL_PAGE, 200, requested_url=_PAGE, final_url=_PAGE)
        assert not verdict.walled
        assert verdict.evidence is None

    def test_a_wall_always_names_its_evidence(self) -> None:
        """The 4.1.0 invariant, now structural rather than remembered."""
        for html, status in [
            (_SIGNATURE_WALL, 200),
            (_HOST_TITLED_WALL, 200),
            (_CAPTCHA_BODY, 403),
            (_REAL_PAGE, 200),
            ("", 200),
        ]:
            verdict = classify_wall(html, status, requested_url=_PAGE, final_url=_PAGE)
            assert not verdict.walled or verdict.evidence, verdict

    def test_it_works_with_no_urls_at_all(self) -> None:
        """Signature detection must not require a URL; some callers have none."""
        assert classify_wall(_SIGNATURE_WALL, 200).walled
        # The URL-dependent evidence simply cannot fire, which is correct.
        assert not classify_wall(_HOST_TITLED_WALL, 200).walled

    def test_a_signature_outranks_the_weaker_evidence(self) -> None:
        """Most precise evidence wins, so the reported cause is the useful one."""
        both = _SIGNATURE_WALL + _HOST_TITLED_WALL
        verdict = classify_wall(both, 200, requested_url=_PAGE, final_url=_PAGE)
        assert verdict.evidence == "cloudflare"


class TestEveryGateAgrees:
    """The regression that produced this file: gates drifting out of step."""

    @pytest.mark.parametrize(
        ("html", "status", "walled"),
        [
            (_SIGNATURE_WALL, 200, True),
            (_HOST_TITLED_WALL, 200, True),
            (_REAL_PAGE, 200, False),
        ],
    )
    def test_proxy_health_uses_the_same_verdict(self, html: str, status: int, walled: bool) -> None:
        """`has_real_content` drives proxy rotation and was two detectors behind.

        A proxy that walked into a host-titled wall was recorded healthy and
        stayed in rotation to burn the next request too.
        """
        from scrapper_tool._challenge import has_real_content

        assert has_real_content(html, status, requested_url=_PAGE, final_url=_PAGE) is not walled

    def test_the_render_vendor_probe_uses_the_same_verdict(self) -> None:
        from scrapper_tool import http_server

        assert (
            http_server._interstitial_vendor(
                _HOST_TITLED_WALL, 200, requested_url=_PAGE, final_url=_PAGE
            )
            == "host_titled_wall"
        )

    def test_diagnose_uses_the_same_verdict(self) -> None:
        from scrapper_tool import diagnose

        outcome, detail = diagnose._describe(_HOST_TITLED_WALL, 200, _PAGE, _PAGE)
        assert outcome == "challenge"
        assert "host_titled_wall" in detail


class TestNoGateMayBypassTheFacade:
    """The guard. This is the point of the whole exercise.

    A detector imported directly is a gate that can silently fall behind, which
    is how proxy health and the MCP surface ended up two releases stale. Adding a
    detector should be one edit to ``classify_wall``, and this fails if anyone
    routes around it.
    """

    #: Predicates that answer "is this a wall?" and therefore belong to the facade.
    #: ``is_cf_challenge_body`` is deliberately absent: it answers a different
    #: question -- "should Scrapling retry with its Cloudflare solver?" -- and
    #: broadening it would make that solver run on non-CF vendors.
    GATED = (
        "is_interstitial",
        "landed_on_challenge",
        "looks_like_host_titled_wall",
        "looks_like_content_free_shell",
    )

    def test_only_the_facade_calls_the_detectors(self) -> None:
        src = pathlib.Path(__file__).resolve().parent.parent.parent / "src" / "scrapper_tool"
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            if path.name == "_challenge.py":
                continue
            text = path.read_text(encoding="utf-8")
            for name in self.GATED:
                # An import, not a mention in a comment or a docstring.
                if re.search(rf"import\b[^\n]*\b{name}\b", text):
                    offenders.append(f"{path.relative_to(src)} imports {name}")
        assert not offenders, (
            "these modules bypass classify_wall and will fall behind the next "
            "detector added:\n  " + "\n  ".join(offenders)
        )

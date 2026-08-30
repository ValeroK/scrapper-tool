"""How a result describes its own failure.

One defect underneath every test here: the tool stated conclusions it had not
earned. It said ``blocked`` without evidence of blocking, said nothing at all
when it landed on a captcha, and never named the network path it was standing on.
Drawn from a field report where five separate symptoms were each read as vendor
hostility and none of them were.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from scrapper_tool import http_server
from scrapper_tool._challenge import landed_on_challenge


def _client(app: Any) -> AsyncClient:
    """Local ASGI client; test modules must not import each other."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


_PRODUCT = "https://vendor.test/parts/68001234AA"
_CAPTCHA_BODY = (
    "<html><head><title>Verification</title></head><body>"
    "<h1>Please confirm you are not a robot</h1><form action='/verify'></form>"
    "</body></html>"
)
_REAL_BODY = "<html><head><title>Widget</title></head><body><p>A real page.</p></body></html>"


class TestLandedOnChallenge:
    """The redirect signal, and everything it must NOT fire on.

    The benign cases are the real test surface. Ordinary redirects are everywhere
    -- scheme upgrades, trailing slashes, canonical hosts, locale prefixes -- and
    a rule that treats difference as evidence would escalate most of the web.
    """

    def test_a_named_challenge_path_is_enough(self) -> None:
        assert landed_on_challenge(_PRODUCT, "https://vendor.test/captcha.html", _CAPTCHA_BODY)

    @pytest.mark.parametrize(
        "path",
        ["/captcha.html", "/challenge", "/blocked", "/sorry/index", "/cdn-cgi/challenge/x"],
    )
    def test_every_known_challenge_shape(self, path: str) -> None:
        assert landed_on_challenge(_PRODUCT, f"https://vendor.test{path}", _CAPTCHA_BODY)

    def test_an_unnamed_path_needs_the_page_to_ask_for_a_human(self) -> None:
        """No path token, so the body has to carry the evidence."""
        assert landed_on_challenge(_PRODUCT, "https://vendor.test/gate", _CAPTCHA_BODY)
        assert not landed_on_challenge(_PRODUCT, "https://vendor.test/gate", _REAL_BODY)

    @pytest.mark.parametrize(
        ("requested", "final"),
        [
            ("http://vendor.test/parts/1", "https://vendor.test/parts/1"),
            ("https://vendor.test/parts/1", "https://vendor.test/parts/1/"),
            ("https://vendor.test/parts/1", "https://www.vendor.test/parts/1"),
            ("https://vendor.test/parts/1", "https://vendor.test/PARTS/1"),
        ],
    )
    def test_benign_redirects_never_fire(self, requested: str, final: str) -> None:
        """Scheme, host, trailing slash and case are not evidence of anything."""
        assert not landed_on_challenge(requested, final, _CAPTCHA_BODY)

    def test_a_large_body_is_not_a_challenge(self) -> None:
        """Interstitials are small. A moved page that is 60 KB is a moved page."""
        big = "<html><body>" + ("content " * 9000) + "are not a robot</body></html>"
        assert not landed_on_challenge(_PRODUCT, "https://vendor.test/p/1", big)

    def test_no_redirect_means_no_verdict(self) -> None:
        assert not landed_on_challenge(_PRODUCT, _PRODUCT, _CAPTCHA_BODY)

    def test_missing_inputs_are_safe(self) -> None:
        assert not landed_on_challenge("", "https://vendor.test/captcha", _CAPTCHA_BODY)
        assert not landed_on_challenge(_PRODUCT, "", _CAPTCHA_BODY)
        assert not landed_on_challenge(_PRODUCT, "https://vendor.test/captcha", "")


class TestFailureReason:
    """A cheap enum, so a caller can branch without parsing prose."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            ("Page.goto: Timeout 30000ms exceeded", "timeout"),
            ("the request timed out", "timeout"),
            ("[hostile] extra not installed", "extra_missing"),
            ("classifier rejected D output", "no_signal"),
            ("", "no_signal"),
            (None, "no_signal"),
            ("RuntimeError: scrapling exploded", "exception"),
        ],
    )
    def test_buckets(self, error: object, expected: str) -> None:
        assert http_server._failure_reason(error) == expected


class TestEgressReport:
    """Where we were standing when it failed."""

    @staticmethod
    def _req(proxy: str | None = None) -> Any:
        req = http_server.ScrapeRequest(url=_PRODUCT)
        if proxy:
            req.__dict__["_egress_proxy"] = proxy
        return req

    def test_direct_egress_reports_no_proxy(self) -> None:
        report = http_server._egress_report(self._req())
        assert report == {"via": "sidecar", "proxy": None}

    def test_a_proxy_is_named(self) -> None:
        report = http_server._egress_report(self._req("http://proxy.internal:8080"))
        assert report["proxy"] == "http://proxy.internal:8080"

    def test_proxy_credentials_never_reach_the_response(self) -> None:
        """A response body is what every reverse proxy in the path logs."""
        report = http_server._egress_report(self._req("http://user:hunter2@proxy.internal:8080"))
        assert "hunter2" not in str(report)
        assert "user" not in str(report)
        assert report["proxy"] == "http://proxy.internal:8080"

    def test_an_unparseable_proxy_degrades_to_a_mask(self) -> None:
        assert http_server._redact_proxy("not a url at all") == "***"


class TestCaptchaIsNeverAWin:
    """The reported false negative, end to end.

    ``mode="fetch"`` followed a redirect onto ``/captcha.html``, returned 4,419
    bytes of captcha, and reported ``blocked=False`` with
    ``challenge_detected=None``. A caller that trusts those flags parses a
    challenge page as content -- worse than a false block, because a false block
    costs one escalation while this corrupts the dataset silently.

    The extractors are not at fault: ``mode="fetch"`` accepts any 200 carrying a
    body, and a captcha page is a body. The classifier answers "did we extract a
    shape?"; it cannot answer "is this the page we asked for?".
    """

    @pytest.fixture()
    def app_no_auth(self) -> Any:
        return http_server._build_app(api_key=None, cors_origins=["*"])

    @staticmethod
    def _ladder_returns(monkeypatch: pytest.MonkeyPatch, *, text: str, final_url: str) -> None:
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = 200
        resp.text = text
        resp.url = final_url
        resp.headers = {"content-type": "text/html"}

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            return resp, "chrome150"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

    @pytest.mark.asyncio
    async def test_a_captcha_redirect_is_not_reported_as_a_fetch_win(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._ladder_returns(
            monkeypatch, text=_CAPTCHA_BODY, final_url="https://vendor.test/captcha.html"
        )

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": _PRODUCT, "mode": "fetch"})

        # 422 blocked, because landing on a captcha IS evidence of blocking --
        # which is precisely what this release reserves that word for.
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "blocked"
        assert body["blocked"] is True
        assert "prove we are human" in body["detail"]
        assert "captcha.html" in body["detail"]

    @pytest.mark.asyncio
    async def test_a_real_page_still_wins_normally(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The veto must not cost the common case anything."""
        product = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"Widget","sku":"X1",'
            '"offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        self._ladder_returns(monkeypatch, text=product, final_url=_PRODUCT)

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": _PRODUCT})

        body = resp.json()
        assert body["pattern_used"] == "a_b_c"
        assert body["blocked"] is False
        assert body["challenge_detected"] is None

    @pytest.mark.asyncio
    async def test_every_result_says_where_it_was_standing(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The field that turns a two-hour investigation into a glance."""
        product = (
            '<html><head><script type="application/ld+json">'
            '{"@context":"https://schema.org","@type":"Product","name":"W","sku":"X",'
            '"offers":{"@type":"Offer","price":"1.00","priceCurrency":"USD"}}'
            "</script></head><body></body></html>"
        )
        self._ladder_returns(monkeypatch, text=product, final_url=_PRODUCT)

        async with _client(app_no_auth) as client:
            body = (await client.post("/scrape", json={"url": _PRODUCT})).json()

        assert body["requested_url"] == _PRODUCT
        assert body["egress"] == {"via": "sidecar", "proxy": None}
        # `url` is where we finished; `requested_url` is what was asked for. A
        # caller can now compare them without trusting our verdict.
        assert body["url"] == _PRODUCT

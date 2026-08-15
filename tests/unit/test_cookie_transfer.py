"""Contract tests for the harvest -> jar -> apply path.

The individual primitives (`domain_matches`, `is_expired`, `cookies_for_url`)
have their own tests. What was untested is the **journey**: a captcha solve now
harvests the browser's whole context jar, and that jar goes on to be applied to
later tiers and, via a persistent profile, to later runs.

Harvesting the whole jar is deliberate — Cloudflare pairs `cf_clearance` with
`__cf_bm`, DataDome sets a session marker beside its own cookie, and a clearance
replayed without its siblings is frequently rejected. But it means the jar can
hold cookies for hosts other than the one being scraped, so "does anything leak"
stops being hypothetical and becomes a property worth pinning.

Two failure modes, both silent if they regress:

- **cross-domain leak** — sending site A's session to site B. A correctness bug
  and a privacy one, and nothing would error.
- **stale replay** — re-sending a clearance that expired ~30 minutes ago instead
  of re-solving. The request just quietly fails to be authenticated, which reads
  as "the site blocked us" and sends the reader hunting for a bypass.
"""

from __future__ import annotations

import time

import pytest

from scrapper_tool.cookies import CookieIn, cookies_for_url, from_playwright, merge

_NOW = time.time()
_HOUR = 3600.0


def _pw(
    name: str, domain: str, *, expires: float | None = None, path: str = "/", secure: bool = False
) -> dict[str, object]:
    """A cookie in the shape Playwright's `context.cookies()` returns."""
    entry: dict[str, object] = {
        "name": name,
        "value": f"{name}-value",
        "domain": domain,
        "path": path,
    }
    if expires is not None:
        entry["expires"] = expires
    if secure:
        entry["secure"] = True
    return entry


class TestDomainIsolation:
    """A clearance won on one host must not travel to another."""

    def test_harvested_jar_does_not_leak_across_domains(self) -> None:
        """The headline property.

        A browser that visited two hosts holds both hosts' cookies, and the
        captcha hook harvests the lot. Applying that jar to one host must send
        only that host's cookies.
        """
        jar = from_playwright(
            [
                _pw("cf_clearance", "target.example"),
                _pw("__cf_bm", "target.example"),
                _pw("session", "other.example"),
                _pw("tracking", "analytics.example"),
            ]
        )
        applied = cookies_for_url(jar, "https://target.example/page")
        assert {c.name for c in applied} == {"cf_clearance", "__cf_bm"}

    def test_siblings_travel_together(self) -> None:
        """The reason the whole jar is harvested rather than the named cookie.

        A `cf_clearance` replayed without `__cf_bm` is frequently rejected, so
        losing the sibling would defeat the point of harvesting at all.
        """
        jar = from_playwright(
            [_pw("cf_clearance", "target.example"), _pw("__cf_bm", "target.example")]
        )
        assert len(cookies_for_url(jar, "https://target.example/")) == 2

    def test_subdomain_cookie_reaches_the_parent_host_rule(self) -> None:
        """RFC 6265: a cookie for `example.com` is sent to `www.example.com`."""
        jar = from_playwright([_pw("clearance", "example.com")])
        assert len(cookies_for_url(jar, "https://www.example.com/")) == 1

    def test_sibling_subdomains_do_not_share(self) -> None:
        """`a.example.com` and `b.example.com` are different sites for this purpose."""
        jar = from_playwright([_pw("session", "a.example.com")])
        assert cookies_for_url(jar, "https://b.example.com/") == []

    def test_lookalike_suffix_is_not_a_match(self) -> None:
        """`notexample.com` must not receive `example.com`'s cookies.

        A naive `endswith` check passes every other test in this class and fails
        this one, which is the whole reason it is here.
        """
        jar = from_playwright([_pw("session", "example.com")])
        assert cookies_for_url(jar, "https://notexample.com/") == []

    def test_secure_cookie_is_not_sent_over_plain_http(self) -> None:
        jar = from_playwright([_pw("clearance", "target.example", secure=True)])
        assert cookies_for_url(jar, "http://target.example/") == []
        assert len(cookies_for_url(jar, "https://target.example/")) == 1


class TestExpiry:
    """A clearance outlives its usefulness; replaying a dead one wastes the request."""

    def test_expired_clearance_is_not_replayed(self) -> None:
        """The stale-replay failure mode.

        A cf_clearance lasts ~30 minutes while a persisted profile lasts
        indefinitely, so this is the *expected* state of any profile left
        overnight — not an edge case.
        """
        jar = from_playwright([_pw("cf_clearance", "target.example", expires=_NOW - _HOUR)])
        assert cookies_for_url(jar, "https://target.example/", now=_NOW) == []

    def test_live_clearance_is_replayed(self) -> None:
        jar = from_playwright([_pw("cf_clearance", "target.example", expires=_NOW + _HOUR)])
        assert len(cookies_for_url(jar, "https://target.example/", now=_NOW)) == 1

    def test_session_cookie_without_expiry_is_kept(self) -> None:
        """No `expires` means a session cookie, not an expired one."""
        jar = from_playwright([_pw("session", "target.example")])
        assert len(cookies_for_url(jar, "https://target.example/", now=_NOW)) == 1

    def test_expired_survivors_do_not_accumulate_in_the_jar(self) -> None:
        """`merge` drops expired entries, so a long-lived profile cannot grow forever."""
        fresh = from_playwright([_pw("a", "target.example", expires=_NOW + _HOUR)])
        stale = from_playwright([_pw("b", "target.example", expires=_NOW - _HOUR)])
        assert {c.name for c in merge(stale, fresh)} == {"a"}

    def test_a_refreshed_clearance_replaces_the_old_one(self) -> None:
        """Re-solving must not leave two `cf_clearance` entries to choose between."""
        old = from_playwright([_pw("cf_clearance", "target.example", expires=_NOW + _HOUR)])
        new = [CookieIn(name="cf_clearance", value="NEWER", domain="target.example", path="/")]
        merged = merge(old, new)
        assert len(merged) == 1
        # Values are SecretStr — cookie values are treated as credentials, so
        # they do not render in logs or reprs by accident.
        assert merged[0].value.get_secret_value() == "NEWER"


class TestApplySelection:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://target.example/", {"root"}),
            ("https://target.example/admin/panel", {"root", "scoped"}),
        ],
    )
    def test_path_scoping(self, url: str, expected: set[str]) -> None:
        jar = from_playwright(
            [_pw("root", "target.example"), _pw("scoped", "target.example", path="/admin")]
        )
        assert {c.name for c in cookies_for_url(jar, url)} == expected

    def test_unparseable_url_sends_nothing(self) -> None:
        """Errs toward sending nothing — the safe direction for a credential."""
        jar = from_playwright([_pw("clearance", "target.example")])
        assert cookies_for_url(jar, "not-a-url") == []

    def test_empty_jar_is_not_an_error(self) -> None:
        assert cookies_for_url([], "https://target.example/") == []

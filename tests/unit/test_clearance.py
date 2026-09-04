"""Winning a clearance once per domain instead of once per request.

Clearing a wall is the most expensive thing this tool does -- a local vision
solve was measured at roughly 70 s of inference -- and the cookie it buys was
discarded the moment the browser closed. The mechanism to keep it existed and was
opt-in, so in practice nobody kept it.

``_harvest_cookies`` records five reasons not to persist clearances in the recipe
store, and all five are correct. These tests pin the answers to them, because
they are what make this store a different thing rather than the same mistake
somewhere else.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from scrapper_tool.clearance import (
    clearance_dir_for,
    clearance_enabled,
    clearance_root,
    clearance_ttl_s,
    touch,
)

_URL = "https://www.vendor.test/parts/68001234AA"
_OTHER = "https://other.test/p"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never the real cache.

    The first run of this feature wrote eight fake domains into ``~/.cache``
    before conftest grew the same isolation.
    """
    monkeypatch.setenv("SCRAPPER_TOOL_CLEARANCE_DIR", str(tmp_path / "clearance"))


class TestIdentityIsTheGate:
    """The objection that matters most, and the reason this is safe at all."""

    def test_an_anonymous_request_gets_a_shared_profile(self) -> None:
        assert clearance_dir_for(_URL, has_caller_cookies=False) is not None

    def test_a_request_carrying_cookies_never_shares(self) -> None:
        """That request is acting as somebody; its profile is a session.

        The worst case of a *successful* read here is impersonating the wrong
        user, which is categorically worse than paying for another solve.
        """
        assert clearance_dir_for(_URL, has_caller_cookies=True) is None

    def test_different_domains_never_share_a_profile(self) -> None:
        a = clearance_dir_for(_URL, has_caller_cookies=False)
        b = clearance_dir_for(_OTHER, has_caller_cookies=False)
        assert a is not None and b is not None
        assert a != b

    def test_the_same_domain_reuses_one_profile(self) -> None:
        """The whole point: the second request inherits the first one's clearance."""
        first = clearance_dir_for(_URL, has_caller_cookies=False)
        second = clearance_dir_for("https://www.vendor.test/other/page", has_caller_cookies=False)
        assert first == second

    def test_www_shares_with_the_apex_but_other_subdomains_do_not(self) -> None:
        """Deliberately conservative, and consistent with the domain policy store.

        `www.` is stripped, so the two spellings of one site share a profile. A
        different subdomain gets its own: a clearance is issued for a host, and
        assuming it transfers across `shop.` and `api.` would be guessing about
        someone else's cookie scope in order to save a solve.
        """
        apex = clearance_dir_for("https://vendor.test/q", has_caller_cookies=False)
        www = clearance_dir_for("https://www.vendor.test/q", has_caller_cookies=False)
        shop = clearance_dir_for("https://shop.vendor.test/p", has_caller_cookies=False)
        assert apex == www
        assert shop != apex


class TestExpiry:
    """A clearance's own lifetime, not the recipe store's fourteen days."""

    def test_the_default_ttl_matches_a_clearance(self) -> None:
        assert clearance_ttl_s() == 1800.0

    def test_a_stale_profile_is_discarded_rather_than_reused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = clearance_dir_for(_URL, has_caller_cookies=False)
        assert path is not None
        (path / "cookies.sqlite").write_text("stale clearance", encoding="utf-8")

        # Age it past the TTL.
        old = time.time() - (clearance_ttl_s() + 60)
        os.utime(path, (old, old))

        again = clearance_dir_for(_URL, has_caller_cookies=False)
        assert again == path
        assert not (path / "cookies.sqlite").exists(), "a stale profile was reused"

    def test_a_fresh_profile_keeps_its_contents(self) -> None:
        path = clearance_dir_for(_URL, has_caller_cookies=False)
        assert path is not None
        (path / "cookies.sqlite").write_text("good clearance", encoding="utf-8")

        again = clearance_dir_for(_URL, has_caller_cookies=False)
        assert again == path
        assert (path / "cookies.sqlite").read_text(encoding="utf-8") == "good clearance"

    def test_touch_restarts_the_clock(self) -> None:
        """A domain under continuous crawl must not lose its clearance mid-walk."""
        path = clearance_dir_for(_URL, has_caller_cookies=False)
        assert path is not None
        old = time.time() - (clearance_ttl_s() - 5)
        os.utime(path, (old, old))
        before = path.stat().st_mtime

        touch(path)

        assert path.stat().st_mtime > before

    def test_a_garbage_ttl_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_CLEARANCE_TTL_S", "not-a-number")
        assert clearance_ttl_s() == 1800.0

    def test_a_zero_ttl_is_not_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Zero would mean "expire immediately", i.e. silently disable the feature."""
        monkeypatch.setenv("SCRAPPER_TOOL_CLEARANCE_TTL_S", "0")
        assert clearance_ttl_s() == 1800.0


class TestItDegradesRatherThanFails:
    """Reuse is an optimisation. Every failure has to be survivable."""

    def test_it_can_be_switched_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_CLEARANCE_REUSE", "0")
        assert clearance_enabled() is False
        assert clearance_dir_for(_URL, has_caller_cookies=False) is None

    def test_it_is_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SCRAPPER_TOOL_CLEARANCE_REUSE", raising=False)
        assert clearance_enabled() is True

    def test_an_underivable_domain_gets_no_profile(self) -> None:
        assert clearance_dir_for("not a url", has_caller_cookies=False) is None
        assert clearance_dir_for("", has_caller_cookies=False) is None

    def test_an_unwritable_root_degrades_to_no_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to the old per-request behaviour, never to an error."""
        blocker = tmp_path / "blocked"
        blocker.write_text("I am a file, not a directory", encoding="utf-8")
        monkeypatch.setenv("SCRAPPER_TOOL_CLEARANCE_DIR", str(blocker))
        assert clearance_dir_for(_URL, has_caller_cookies=False) is None


class TestItStaysOutOfTheSharedTemp:
    """One of the five recorded objections was a world-readable temp path."""

    def test_the_default_root_is_under_the_user_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCRAPPER_TOOL_CLEARANCE_DIR", raising=False)
        root = clearance_root()
        assert "scrapper-tool" in root.parts
        assert root.name == "clearance"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are advisory on Windows")
    def test_profiles_are_created_private(self) -> None:
        path = clearance_dir_for(_URL, has_caller_cookies=False)
        assert path is not None
        assert path.stat().st_mode & 0o077 == 0, "a clearance profile was group/world readable"

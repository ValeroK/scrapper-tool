"""Unit tests for the pre-flight URL guard.

The bulk of this file is one parametrized table, because the guard *is* a
classifier and a table is the honest way to test one — every rule the module
claims gets a row, and a regression names the exact case it broke.
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
import textwrap

import pytest

from scrapper_tool import _urlguard
from scrapper_tool._urlguard import (
    REFUSAL_REMEDIES,
    GuardPolicy,
    all_reasons,
    assert_url_allowed,
    check_host,
    check_url,
    guard_policy,
    url_guard_enabled,
)
from scrapper_tool.errors import ScrapingError, UrlNotAllowed


@pytest.fixture(autouse=True)
def _reset_disabled_latch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The "guard is off" warning is a process-lifetime latch; reset per test."""
    monkeypatch.setattr(_urlguard, "_warned_disabled", False)
    # Guard reads env at call time, so clear anything the ambient shell set.
    for name in (
        "SCRAPPER_TOOL_URL_GUARD",
        "SCRAPPER_TOOL_URL_GUARD_ALLOW",
        "SCRAPPER_TOOL_URL_GUARD_DNS",
    ):
        monkeypatch.delenv(name, raising=False)


# --- The classifier table --------------------------------------------------

REFUSED_CASES: list[tuple[str, str]] = [
    # scheme
    ("file:///etc/passwd", "scheme"),
    ("gopher://example.com/", "scheme"),
    ("data:text/html,<h1>x</h1>", "scheme"),
    ("ftp://example.com/f", "scheme"),
    ("//example.com/no-scheme", "scheme"),
    ("example.com/no-scheme", "scheme"),
    # userinfo — reads as one host, fetches another
    ("http://expected.com@169.254.169.254/", "userinfo"),
    ("https://user:pass@10.0.0.1/", "userinfo"),
    # loopback, in its many spellings
    ("http://127.0.0.1/", "loopback"),
    ("http://127.0.0.53/", "loopback"),
    ("http://[::1]/", "loopback"),
    ("http://localhost/", "special_tld"),
    ("http://localhost./", "special_tld"),
    ("http://LocalHost/", "special_tld"),
    # encoded IPv4 that ipaddress refuses but the resolver accepts
    ("http://2130706433/", "loopback"),
    ("http://0x7f000001/", "loopback"),
    ("http://0177.0.0.1/", "loopback"),
    ("http://127.1/", "loopback"),
    ("http://127.0.1/", "loopback"),
    # IPv4 embedded in IPv6 — stdlib properties all say False
    ("http://[::ffff:127.0.0.1]/", "loopback"),
    ("http://[::ffff:7f00:1]/", "loopback"),
    ("http://[::ffff:10.0.0.1]/", "private_ip"),
    # 6to4/NAT64 wrapping a private payload reports the payload's verdict —
    # "loopback" says what it actually points at, which "reserved" would not.
    ("http://[2002:7f00:1::]/", "loopback"),
    ("http://[64:ff9b::7f00:1]/", "reserved"),
    # ...but a wrapper around a *public* payload is still not fetchable.
    ("http://[2002:5db8:d822::]/", "reserved"),
    # private space
    ("http://10.0.0.1/", "private_ip"),
    ("http://172.16.0.1/", "private_ip"),
    ("http://192.168.1.1/", "private_ip"),
    ("http://[fc00::1]/", "private_ip"),
    ("http://[fd00::1]/", "private_ip"),
    # link-local and metadata
    ("http://169.254.1.1/", "link_local"),
    ("http://[fe80::1]/", "link_local"),
    ("http://169.254.169.254/latest/meta-data/", "metadata"),
    ("http://[fd00:ec2::254]/", "metadata"),
    ("http://100.100.100.200/", "metadata"),
    ("http://metadata.google.internal/", "metadata"),
    ("http://metadata/", "metadata"),
    # ranges is_private misses
    ("http://100.64.0.1/", "cgnat"),
    ("http://198.18.0.1/", "benchmark"),
    ("http://240.0.0.1/", "reserved"),
    ("http://255.255.255.255/", "reserved"),
    ("http://192.0.0.1/", "reserved"),
    # unspecified / multicast
    ("http://0.0.0.0/", "unspecified"),
    ("http://[::]/", "unspecified"),
    ("http://224.0.0.1/", "multicast"),
    ("http://[ff02::1]/", "multicast"),
    # special-use suffixes, refused before any resolver query
    ("http://printer.local/", "special_tld"),
    ("http://db.internal/", "special_tld"),
    ("http://svc.corp/", "special_tld"),
    ("http://nas.lan/", "special_tld"),
    ("http://x.home.arpa/", "special_tld"),
    ("http://abc.onion/", "special_tld"),
    # no host
    ("http:///path-only", "no_host"),
]

ALLOWED_CASES: list[str] = [
    "http://example.com/",
    "https://example.com/path?q=1#frag",
    "https://example.com:8443/x",
    "https://1.1.1.1/",
    "https://8.8.8.8/resolve",
    "https://[2606:4700:4700::1111]/",
    "https://sub.domain.example.co.uk/a/b",
    "https://xn--bcher-kva.com/",  # punycode passes through untouched
    # Reserved-for-non-resolution TLDs are allowed on purpose: they cannot
    # reach anything, and the test suite mocks against them.
    "https://example.test/path",
    "https://fixture.invalid/x",
]


@pytest.mark.parametrize(("url", "reason"), REFUSED_CASES)
def test_check_url_refuses(url: str, reason: str) -> None:
    verdict = check_url(url)
    assert not verdict.allowed, f"{url} should be refused"
    assert verdict.reason == reason, f"{url}: expected {reason}, got {verdict.reason}"
    assert verdict.remedy, f"{url}: refusal must carry a remedy"


@pytest.mark.parametrize("url", ALLOWED_CASES)
def test_check_url_allows(url: str) -> None:
    verdict = check_url(url)
    assert verdict.allowed, f"{url} should be allowed, got {verdict.reason}"
    assert verdict.reason == "ok"
    assert verdict.remedy == ""


def test_check_url_never_raises_on_garbage() -> None:
    for value in ("", "   ", "http://", ":", "http://[", "\x00", "h" * 5000, "://x"):
        verdict = check_url(value)
        assert isinstance(verdict.allowed, bool)


def test_idna_failure_is_refused() -> None:
    # A label of forbidden width cannot be IDNA-encoded.
    verdict = check_host("\u200b" * 80)
    assert not verdict.allowed
    assert verdict.reason == "idna"


# --- DNS ------------------------------------------------------------------


def _fake_getaddrinfo(*addresses: str):
    def _inner(host: str, port: object, **_kwargs: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    return _inner


@pytest.mark.asyncio
async def test_dns_resolution_refuses_private_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.1.2.3"))
    verdict = await _urlguard.resolve_and_check("https://sneaky.example.com/")
    assert not verdict.allowed
    assert verdict.reason == "private_ip"
    assert verdict.resolved == ("10.1.2.3",)


@pytest.mark.asyncio
async def test_dns_mixed_answers_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """One public and one private answer is a rebinding attack, not a typo."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34", "127.0.0.1"))
    verdict = await _urlguard.resolve_and_check("https://rebind.example.com/")
    assert not verdict.allowed
    assert verdict.reason == "loopback"


@pytest.mark.asyncio
async def test_dns_public_answer_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    verdict = await _urlguard.resolve_and_check("https://example.com/")
    assert verdict.allowed
    assert verdict.reason == "ok"


@pytest.mark.asyncio
async def test_dns_failure_allows_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolver blip must not become a 403 — it would make the guard flaky."""

    def _boom(*_args: object, **_kwargs: object) -> list[tuple]:
        raise socket.gaierror("temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    verdict = await _urlguard.resolve_and_check("https://down.example.com/")
    assert verdict.allowed
    assert verdict.reason == "unresolvable"
    await assert_url_allowed("https://down.example.com/")  # must not raise


@pytest.mark.asyncio
async def test_ip_literal_skips_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explode(*_args: object, **_kwargs: object) -> list[tuple]:
        msg = "resolution must not be attempted for an IP literal"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "getaddrinfo", _explode)
    assert (await _urlguard.resolve_and_check("https://1.1.1.1/")).allowed


@pytest.mark.asyncio
async def test_dns_disabled_skips_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_DNS", "0")

    def _explode(*_args: object, **_kwargs: object) -> list[tuple]:
        msg = "resolution must not be attempted when DNS checking is off"
        raise AssertionError(msg)

    monkeypatch.setattr(socket, "getaddrinfo", _explode)
    assert (await _urlguard.resolve_and_check("https://example.com/")).allowed


# --- Allowlist ------------------------------------------------------------


def test_allowlist_permits_exact_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_ALLOW", "fixtures.internal, other.example")
    assert check_url("http://fixtures.internal/page").allowed
    assert not check_url("http://db.internal/page").allowed


def test_allowlist_permits_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_ALLOW", "10.0.0.0/8")
    assert check_url("http://10.1.2.3/").allowed
    assert not check_url("http://192.168.1.1/").allowed


def test_allowlist_permits_single_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_ALLOW", "127.0.0.1")
    assert check_url("http://127.0.0.1:8000/fixture").allowed
    assert not check_url("http://127.0.0.2:8000/fixture").allowed


@pytest.mark.asyncio
async def test_allowlist_applies_after_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_ALLOW", "10.0.0.0/8")
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.1.2.3"))
    assert (await _urlguard.resolve_and_check("https://internal.example.com/")).allowed


# --- Disable switch -------------------------------------------------------


def test_guard_disabled_allows_everything_and_warns_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD", "0")
    assert not url_guard_enabled()
    with caplog.at_level("WARNING"):
        assert check_url("http://169.254.169.254/").allowed
        assert check_url("http://10.0.0.1/").allowed
        assert check_host("localhost").allowed
    records = [r for r in caplog.records if "urlguard.disabled" in r.getMessage()]
    assert len(records) == 1, "the disabled warning is a one-shot latch, not per-call"
    message = records[0].getMessage()
    assert "remedy=" in message


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "", "   "])
def test_guard_enabled_truthy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD", value)
    assert url_guard_enabled()


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "nope"])
def test_guard_disabled_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD", value)
    assert not url_guard_enabled()


# --- assert_* entry points ------------------------------------------------


@pytest.mark.asyncio
async def test_assert_url_allowed_raises_url_not_allowed() -> None:
    with pytest.raises(UrlNotAllowed) as excinfo:
        await assert_url_allowed("http://169.254.169.254/latest/meta-data/")
    assert "metadata" in str(excinfo.value)
    # Must stay inside the one hierarchy consumers key their breakers on.
    assert isinstance(excinfo.value, ScrapingError)


@pytest.mark.asyncio
async def test_assert_url_allowed_passes_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    await assert_url_allowed("https://example.com/")


# --- Drift tripwires ------------------------------------------------------


def test_refusal_remedies_covers_every_reason() -> None:
    """Every Reason needs a remedy, so a new rule cannot ship without a fix line.

    Same tripwire shape as the INSTALL_HINTS coverage test: the dict exists to
    stop these strings drifting, and that only holds if it is complete.
    """
    missing = [
        reason
        for reason in all_reasons()
        if reason not in {"ok", "unresolvable"} and not REFUSAL_REMEDIES.get(reason)
    ]
    assert not missing, f"Reason members with no remedy: {missing}"


def test_policy_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    assert guard_policy().enabled
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD", "0")
    assert not guard_policy().enabled


def test_explicit_policy_overrides_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD", "0")
    strict = GuardPolicy(enabled=True)
    assert not check_url("http://10.0.0.1/", policy=strict).allowed


def test_extra_networks_are_parseable() -> None:
    for network, reason in _urlguard._PARSED_EXTRA_NETWORKS:
        assert isinstance(network, ipaddress.IPv4Network | ipaddress.IPv6Network)
        assert reason in set(all_reasons())


class TestImportDiscipline:
    """The guard must stay importable everywhere, because everything imports it.

    ``doctor``, ``cli``, ``mcp``, ``http_server``, ``crawl.*``, ``ladder`` and
    ``http`` all depend on this module. The two that matter most: ``doctor``
    runs on a bare ``pip install scrapper-tool``, and ``mcp`` must never
    transitively pull in FastAPI. A heavy module sneaking into ``_urlguard``'s
    module-level imports would break both at once.
    """

    def test_importing_urlguard_pulls_in_no_heavy_dependency(self) -> None:
        script = textwrap.dedent(
            """
            import sys

            # Poison the extras-gated heavies. Core deps (httpx, curl-cffi,
            # selectolax, extruct, pydantic) are deliberately absent from this
            # list for the same reason as in test_extras: they exist in every
            # install, and ``scrapper_tool/__init__`` imports curl_cffi
            # transitively via ``scrapper_tool.http``.
            for name in (
                "fastapi", "uvicorn", "mcp", "crawl4ai", "scrapling", "camoufox",
                "patchright", "browser_use", "playwright", "rookiepy",
            ):
                sys.modules[name] = None

            from scrapper_tool import _urlguard

            # The classifier must still work, not just import.
            assert _urlguard.check_url("http://169.254.169.254/").reason == "metadata"
            assert _urlguard.check_url("https://example.com/").allowed
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout


class TestStrictTierMode:
    """`SCRAPPER_TOOL_URL_GUARD_STRICT` refuses tiers whose requests we cannot vet.

    The guard's coverage is not uniform: the httpx path is checked per hop, the
    curl_cffi ladder only with `..._STRICT_REDIRECTS`, and the browser/subprocess
    tiers not at all. Strict mode is the only configuration where the guard's
    promise is actually complete -- bought by losing those tiers, which on a
    hostile target means the scrape simply fails. That trade is the operator's.
    """

    def test_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """It removes capability, so it must never turn itself on."""
        monkeypatch.delenv("SCRAPPER_TOOL_URL_GUARD_STRICT", raising=False)
        assert _urlguard.url_guard_strict_enabled() is False
        for tier in _urlguard.UNINTERCEPTABLE_TIERS:
            _urlguard.assert_tier_allowed(tier)  # must not raise

    @pytest.mark.parametrize("tier", ["d", "render", "e1", "e2", "obscura"])
    def test_uninterceptable_tiers_are_refused(
        self, tier: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT", "1")
        with pytest.raises(UrlNotAllowed) as excinfo:
            _urlguard.assert_tier_allowed(tier, url="https://example.com/")
        assert excinfo.value.reason == "uninterceptable_tier"
        assert excinfo.value.remedy
        assert tier in str(excinfo.value)

    def test_interceptable_tiers_are_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT", "1")
        # A/B/C rides the guarded httpx transport, so strict mode has no quarrel.
        _urlguard.assert_tier_allowed("a_b_c")
        _urlguard.assert_tier_allowed("replay")

    def test_the_ladder_is_conditional_on_strict_redirects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one tier whose answer depends on another setting.

        Without per-hop vetting libcurl can be redirected into private space, so
        strict mode refuses it; with `..._STRICT_REDIRECTS=1` every hop is
        checked before it is issued and there is nothing left to object to.
        """
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT", "1")
        monkeypatch.delenv("SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS", raising=False)
        assert _urlguard.tier_is_interceptable("ladder") is False
        with pytest.raises(UrlNotAllowed):
            _urlguard.assert_tier_allowed("ladder")

        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS", "1")
        assert _urlguard.tier_is_interceptable("ladder") is True
        _urlguard.assert_tier_allowed("ladder")  # must not raise

    def test_every_uninterceptable_tier_says_why(self) -> None:
        """A refusal that does not explain itself is indistinguishable from a bug."""
        for tier, why in _urlguard.UNINTERCEPTABLE_TIERS.items():
            assert why, f"{tier} is refused with no reason given"
            assert len(why) > 20, f"{tier}'s reason is too terse to act on"

    def test_the_refusal_reuses_the_url_refusal_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same exception as a refused URL, so both surfaces map it for free.

        REST already turns UrlNotAllowed into a 403 with reason+remedy and MCP
        into an envelope with error_code; a bespoke exception would need both
        wired again for no gain to the caller, who sees the same thing either
        way: this request will not be made on your behalf.
        """
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT", "1")
        with pytest.raises(UrlNotAllowed) as excinfo:
            _urlguard.assert_tier_allowed("e2")
        assert excinfo.value.reason in set(_urlguard.all_reasons())
        assert _urlguard.REFUSAL_REMEDIES[excinfo.value.reason]


class TestStrictModeAtTheTierEntryPoints:
    """The helper being right is not enough; it has to be *called*.

    Each assertion below drives the real public entry point, so a guard that
    gets dropped from one tier during a refactor fails here rather than
    silently restoring the gap.
    """

    @pytest.fixture(autouse=True)
    def _strict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT", "1")
        monkeypatch.delenv("SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS", raising=False)

    @pytest.mark.asyncio
    async def test_ladder_refuses(self) -> None:
        from scrapper_tool.ladder import request_with_ladder

        with pytest.raises(UrlNotAllowed, match="ladder"):
            await request_with_ladder("GET", "https://example.test/x")

    @pytest.mark.asyncio
    async def test_render_refuses_before_launching_a_browser(self) -> None:
        """Refused up front — starting Camoufox first would only be slower."""
        from scrapper_tool.patterns.render import render_html

        with pytest.raises(UrlNotAllowed, match="render"):
            await render_html("https://example.test/x", settle_s=0)

    @pytest.mark.asyncio
    async def test_pattern_d_refuses(self) -> None:
        from scrapper_tool.patterns.d import hostile_client

        with pytest.raises(UrlNotAllowed, match="'d'"):
            async with hostile_client():
                pass

    @pytest.mark.asyncio
    async def test_e1_and_e2_refuse(self) -> None:
        pytest.importorskip("crawl4ai", reason="E1/E2 entry points need the [llm-agent] extra")
        from scrapper_tool.agent import agent_browse, agent_extract

        with pytest.raises(UrlNotAllowed, match="e1"):
            await agent_extract("https://example.test/x", {"type": "object"})
        with pytest.raises(UrlNotAllowed, match="e2"):
            await agent_browse("https://example.test/x", "do a thing")

    @pytest.mark.asyncio
    async def test_obscura_refuses_both_entry_points(self) -> None:
        from scrapper_tool.crawl.batch import batch_fetch, obscura_fetch

        with pytest.raises(UrlNotAllowed, match="obscura"):
            await batch_fetch(["https://example.test/x"])
        with pytest.raises(UrlNotAllowed, match="obscura"):
            await obscura_fetch("https://example.test/x")

    @pytest.mark.asyncio
    async def test_the_ladder_runs_again_once_hops_are_vetted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Strict mode is not a blanket ban; it objects to unvetted requests."""
        monkeypatch.setenv("SCRAPPER_TOOL_URL_GUARD_STRICT_REDIRECTS", "1")
        from scrapper_tool import ladder as ladder_module
        from scrapper_tool.ladder import request_with_ladder
        from scrapper_tool.testing import FakeCurlSession

        FakeCurlSession.reset()
        FakeCurlSession.STATUS_FOR_PROFILE = {ladder_module.IMPERSONATE_LADDER[0]: 200}
        monkeypatch.setattr(ladder_module, "_CurlCffiAsyncSession", FakeCurlSession)
        resp, _profile = await request_with_ladder("GET", "https://example.test/x")
        assert resp.status_code == 200

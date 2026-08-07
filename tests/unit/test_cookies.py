"""Unit tests for ``scrapper_tool.cookies`` and the browser-store shim.

The security-relevant assertions live here, because this is where a bug is a
credential-disclosure bug rather than a scraping bug:

- **Domain matching.** ``evil-example.com`` must never match ``example.com``.
  A naive ``endswith`` passes every other test in this file and fails that one.
- **Redaction.** No cookie *value* may appear in a redacted view, a repr, a
  model dump, or a log record.
- **File permissions.** Jars are ``0600`` in a ``0700`` directory, created
  exclusively rather than created-then-chmod'd.
- **The LGPL guard.** ``browser_cookie3`` must appear in no dependency list.

The browser-store shim is tested by injecting a fake module, so none of this
needs a real browser profile or an OS credential store — neither of which exists
in CI.
"""

from __future__ import annotations

import json
import logging
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from scrapper_tool import _browser_cookies
from scrapper_tool import cookies as cookies_mod
from scrapper_tool.cookies import CookieIn


def make_cookie(
    name: str = "session",
    value: str = "s3cret-value",
    domain: str = "example.com",
    **kwargs: Any,
) -> CookieIn:
    return CookieIn(name=name, value=SecretStr(value), domain=domain, **kwargs)


class TestDomainMatching:
    """The one that matters."""

    @pytest.mark.parametrize(
        ("cookie_domain", "host", "expected"),
        [
            ("example.com", "example.com", True),
            ("example.com", "sub.example.com", True),
            ("example.com", "deep.sub.example.com", True),
            (".example.com", "sub.example.com", True),
            ("example.com", "EXAMPLE.COM", True),
            # The attacks:
            ("example.com", "evil-example.com", False),
            ("example.com", "notexample.com", False),
            ("example.com", "example.com.evil.net", False),
            ("example.com", "example.co", False),
            # Degenerate input:
            ("", "example.com", False),
            ("example.com", "", False),
        ],
    )
    def test_domain_match(self, cookie_domain: str, host: str, expected: bool) -> None:
        assert cookies_mod.domain_matches(cookie_domain, host) is expected

    def test_a_naive_endswith_would_fail_this(self) -> None:
        """Guard the guard: state the exact bug this function prevents."""
        host = "evil-example.com"
        assert host.endswith("example.com")  # what a naive check would do
        assert cookies_mod.domain_matches("example.com", host) is False

    @pytest.mark.parametrize(
        ("cookie_path", "request_path", "expected"),
        [
            ("/", "/anything", True),
            ("/foo", "/foo", True),
            ("/foo", "/foo/bar", True),
            ("/foo", "/foobar", False),
            ("/foo/", "/foo/bar", True),
        ],
    )
    def test_path_match(self, cookie_path: str, request_path: str, expected: bool) -> None:
        assert cookies_mod.path_matches(cookie_path, request_path) is expected


class TestNormalizeDomain:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (".Example.COM", "example.com"),
            ("  example.com  ", "example.com"),
            ("example.com:8443", "example.com"),
            ("example.com", "example.com"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert cookies_mod.normalize_domain(raw) == expected


class TestValidateDomainArg:
    def test_accepts_a_normal_host(self) -> None:
        assert cookies_mod.validate_domain_arg("App.Example.com") == "app.example.com"

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "*.example.com", "localhost", "https://example.com/x", "example..com"],
    )
    def test_rejects(self, bad: str) -> None:
        with pytest.raises(ValueError, match="--domain"):
            cookies_mod.validate_domain_arg(bad)


class TestSelection:
    def test_cookies_for_url_filters_by_domain(self) -> None:
        jar = [
            make_cookie(name="good", domain="example.com"),
            make_cookie(name="bad", domain="evil-example.com"),
        ]
        picked = cookies_mod.cookies_for_url(jar, "https://sub.example.com/x")
        assert [c.name for c in picked] == ["good"]

    def test_secure_cookies_are_withheld_from_http(self) -> None:
        jar = [make_cookie(secure=True)]
        assert cookies_mod.cookies_for_url(jar, "http://example.com/") == []
        assert len(cookies_mod.cookies_for_url(jar, "https://example.com/")) == 1

    def test_expired_cookies_are_dropped(self) -> None:
        jar = [make_cookie(expires=time.time() - 60)]
        assert cookies_mod.cookies_for_url(jar, "https://example.com/") == []

    def test_session_cookies_are_never_expired(self) -> None:
        assert cookies_mod.is_expired(make_cookie(expires=None)) is False

    def test_unparseable_url_yields_nothing(self) -> None:
        """Fail closed: an unusable URL must not mean 'send everything'."""
        assert cookies_mod.cookies_for_url([make_cookie()], "not a url") == []


class TestMerge:
    def test_later_wins_on_same_identity(self) -> None:
        old = make_cookie(value="old")
        new = make_cookie(value="new")
        merged = cookies_mod.merge([old], [new])
        assert len(merged) == 1
        assert merged[0].value.get_secret_value() == "new"

    def test_same_name_different_path_are_distinct_cookies(self) -> None:
        merged = cookies_mod.merge([make_cookie(path="/a")], [make_cookie(path="/b")])
        assert len(merged) == 2

    def test_expired_are_dropped_on_merge(self) -> None:
        merged = cookies_mod.merge([make_cookie(expires=time.time() - 1)], [])
        assert merged == []


class TestConversions:
    def test_to_playwright_uses_camelcase_keys(self) -> None:
        entry = cookies_mod.to_playwright([make_cookie(http_only=True, same_site="Lax")])[0]
        assert entry["httpOnly"] is True
        assert entry["sameSite"] == "Lax"
        assert "http_only" not in entry
        assert entry["value"] == "s3cret-value"

    def test_to_playwright_omits_expires_for_session_cookies(self) -> None:
        entry = cookies_mod.to_playwright([make_cookie(expires=None)])[0]
        assert "expires" not in entry

    def test_storage_state_has_empty_origins(self) -> None:
        """origins carries localStorage, which no tier here reads."""
        state = cookies_mod.to_storage_state([make_cookie()])
        assert state["origins"] == []
        assert len(state["cookies"]) == 1

    def test_cookie_header_round_trips_names_and_values(self) -> None:
        header = cookies_mod.to_cookie_header(
            [make_cookie(name="a", value="1"), make_cookie(name="b", value="2")]
        )
        assert header == "a=1; b=2"

    def test_netscape_marks_secure_and_expiry(self) -> None:
        text = cookies_mod.to_netscape([make_cookie(expires=1800000000.0, secure=True)])
        row = next(line for line in text.splitlines() if not line.startswith("#"))
        fields = row.split("\t")
        assert fields[0] == ".example.com"
        assert fields[3] == "TRUE"
        assert fields[4] == "1800000000"

    def test_playwright_round_trip_preserves_fields(self) -> None:
        original = make_cookie(path="/x", http_only=True, secure=False, expires=1800000000.0)
        restored = cookies_mod.from_playwright(cookies_mod.to_playwright([original]))[0]
        assert restored.name == original.name
        assert restored.value.get_secret_value() == original.value.get_secret_value()
        assert restored.path == "/x"
        assert restored.http_only is True
        assert restored.secure is False


class TestFromBrowserStore:
    def test_maps_rookiepy_snake_case_rows(self) -> None:
        """Field names verified against rookiepy 0.5.6's own to_cookiejar."""
        rows = [
            {
                "domain": ".example.com",
                "path": "/",
                "secure": True,
                "expires": 1800000000,
                "name": "sid",
                "value": "abc",
                "http_only": True,
                "same_site": "lax",
            }
        ]
        cookie = cookies_mod.from_browser_store(rows)[0]
        assert cookie.domain == "example.com"
        assert cookie.http_only is True
        assert cookie.same_site == "Lax"
        assert cookie.expires == 1800000000

    def test_accepts_camelcase_rows_too(self) -> None:
        rows = [
            {
                "domain": "example.com",
                "name": "sid",
                "value": "abc",
                "httpOnly": True,
                "sameSite": "Strict",
            }
        ]
        cookie = cookies_mod.from_browser_store(rows)[0]
        assert cookie.http_only is True
        assert cookie.same_site == "Strict"

    def test_zero_expiry_means_session_not_1970(self) -> None:
        rows = [{"domain": "example.com", "name": "s", "value": "v", "expires": 0}]
        assert cookies_mod.from_browser_store(rows)[0].expires is None

    def test_malformed_rows_are_skipped_not_fatal(self) -> None:
        rows = [
            {"domain": "example.com", "name": "", "value": "v"},
            {"domain": "example.com", "value": "v"},
            {"domain": "example.com", "name": "ok", "value": "v"},
        ]
        assert [c.name for c in cookies_mod.from_browser_store(rows)] == ["ok"]


class TestRedaction:
    def test_redact_never_includes_the_value(self) -> None:
        redacted = cookies_mod.redact([make_cookie(value="TOP-SECRET")])
        assert "TOP-SECRET" not in json.dumps(redacted)
        assert redacted[0]["value_len"] == len("TOP-SECRET")

    def test_redact_keeps_what_makes_it_debuggable(self) -> None:
        redacted = cookies_mod.redact([make_cookie(name="sid", domain="example.com")])[0]
        assert redacted["name"] == "sid"
        assert redacted["domain"] == "example.com"

    def test_repr_is_masked_by_secretstr(self) -> None:
        """_logging renders with repr, so this is the stdlib log path."""
        assert "TOP-SECRET" not in repr(make_cookie(value="TOP-SECRET"))

    def test_model_dump_json_is_masked(self) -> None:
        """An accidental echo-back in a response body must be safe."""
        assert "TOP-SECRET" not in make_cookie(value="TOP-SECRET").model_dump_json()

    def test_no_value_reaches_a_log_record(self, caplog: pytest.LogCaptureFixture) -> None:
        from scrapper_tool._logging import get_logger

        logger = get_logger("test.cookies")
        with caplog.at_level(logging.DEBUG):
            logger.info(
                "cookies.applied", cookies=cookies_mod.redact([make_cookie(value="TOP-SECRET")])
            )
        assert "TOP-SECRET" not in caplog.text


class TestJarPersistence:
    def test_save_creates_0600_file_in_0700_dir(self, tmp_path: Path) -> None:
        target = cookies_mod.save_cookies([make_cookie()], "example.com", directory=tmp_path)
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700

    def test_round_trip_through_disk(self, tmp_path: Path) -> None:
        original = [make_cookie(name="sid", value="v1"), make_cookie(name="csrf", value="v2")]
        cookies_mod.save_cookies(original, "example.com", directory=tmp_path)
        loaded = cookies_mod.load_cookies("example.com", directory=tmp_path)
        assert {c.name for c in loaded} == {"sid", "csrf"}
        assert {c.value.get_secret_value() for c in loaded} == {"v1", "v2"}

    def test_refuses_to_clobber_without_overwrite(self, tmp_path: Path) -> None:
        cookies_mod.save_cookies([make_cookie()], "example.com", directory=tmp_path)
        with pytest.raises(FileExistsError, match="--force"):
            cookies_mod.save_cookies([make_cookie()], "example.com", directory=tmp_path)

    def test_overwrite_replaces_and_keeps_0600(self, tmp_path: Path) -> None:
        cookies_mod.save_cookies([make_cookie(value="old")], "example.com", directory=tmp_path)
        target = cookies_mod.save_cookies(
            [make_cookie(value="new")], "example.com", directory=tmp_path, overwrite=True
        )
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        loaded = cookies_mod.load_cookies("example.com", directory=tmp_path)
        assert loaded[0].value.get_secret_value() == "new"

    def test_missing_jar_returns_empty_not_error(self, tmp_path: Path) -> None:
        assert cookies_mod.load_cookies("never-saved.com", directory=tmp_path) == []

    def test_never_widens_an_existing_file(self, tmp_path: Path) -> None:
        """O_EXCL means we create fresh; we must never chmod someone else's file open."""
        victim = tmp_path / "example.com.json"
        victim.write_text("{}")
        victim.chmod(0o644)
        with pytest.raises(FileExistsError):
            cookies_mod.save_cookies([make_cookie()], "example.com", directory=tmp_path)
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644

    def test_jar_dir_honours_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCRAPPER_TOOL_COOKIE_DIR", str(tmp_path / "jars"))
        assert cookies_mod.cookie_jar_dir() == tmp_path / "jars"


class TestModelValidation:
    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            CookieIn(name="a", value=SecretStr("v"), domain="example.com", bogus=1)

    def test_rejects_empty_name(self) -> None:
        with pytest.raises(ValidationError):
            CookieIn(name="", value=SecretStr("v"), domain="example.com")

    def test_domain_is_normalized_on_construction(self) -> None:
        assert make_cookie(domain=".Example.COM").domain == "example.com"

    def test_secure_defaults_true(self) -> None:
        """Defaulting to insecure would silently downgrade a cookie's protection."""
        assert make_cookie().secure is True


class _FakeBackend:
    """Stands in for rookiepy: same call shape, no browser."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, fail: bool = False) -> None:
        self._rows = rows or []
        self._fail = fail
        self.calls: list[tuple[str, list[str]]] = []

    def firefox(self, domains: list[str]) -> list[dict[str, Any]]:
        self.calls.append(("firefox", domains))
        if self._fail:
            msg = "keyring locked"
            raise RuntimeError(msg)
        return self._rows


class TestBrowserCookieShim:
    def test_reads_rows_via_the_named_browser(self) -> None:
        backend = _FakeBackend([{"domain": "example.com", "name": "s", "value": "v"}])
        rows = _browser_cookies.read_browser_cookies(
            "example.com", browser="firefox", backend=backend
        )
        assert rows[0]["name"] == "s"
        assert backend.calls == [("firefox", ["example.com"])]

    def test_falls_through_the_default_order(self) -> None:
        backend = _FakeBackend([{"domain": "example.com", "name": "s", "value": "v"}])
        rows = _browser_cookies.read_browser_cookies("example.com", backend=backend)
        assert len(rows) == 1

    def test_backend_failure_is_reported_not_swallowed(self) -> None:
        backend = _FakeBackend(fail=True)
        with pytest.raises(_browser_cookies.BrowserCookieError, match="keyring locked"):
            _browser_cookies.read_browser_cookies("example.com", browser="firefox", backend=backend)

    def test_unknown_browser_name_is_an_error_not_a_silent_empty(self) -> None:
        backend = _FakeBackend()
        with pytest.raises(_browser_cookies.BrowserCookieError, match="no usable browser"):
            _browser_cookies.read_browser_cookies(
                "example.com", browser="netscape-navigator", backend=backend
            )

    def test_resolve_backend_raises_with_an_install_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "rookiepy", None)
        import importlib.util

        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
        with pytest.raises(_browser_cookies.BrowserCookieError, match=r"scrapper-tool\[cookies\]"):
            _browser_cookies.resolve_backend()

    def test_browser_order_names_exist_in_rookiepy_if_installed(self) -> None:
        """Catch a typo'd browser name against the real library when present."""
        rookiepy = pytest.importorskip("rookiepy")
        for name in _browser_cookies._BROWSER_ORDER:
            assert hasattr(rookiepy, name), f"rookiepy has no reader named {name!r}"


class TestLgplGuard:
    """browser_cookie3 is LGPL and must never enter this project's tree."""

    def test_browser_cookie3_is_in_no_dependency_list(self) -> None:
        import tomllib

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

        declared: list[str] = list(data["project"].get("dependencies", []))
        for extra_deps in data["project"].get("optional-dependencies", {}).values():
            declared.extend(extra_deps)
        for group_deps in data.get("dependency-groups", {}).values():
            declared.extend(d for d in group_deps if isinstance(d, str))

        offenders = [d for d in declared if "browser_cookie3" in d or "browser-cookie3" in d]
        assert offenders == [], f"LGPL dependency declared: {offenders}"

    def test_browser_cookie3_is_not_in_the_lockfile(self) -> None:
        lock = Path(__file__).resolve().parents[2] / "uv.lock"
        if not lock.is_file():  # pragma: no cover — lockfile is committed
            pytest.skip("no uv.lock")
        assert 'name = "browser-cookie3"' not in lock.read_text(encoding="utf-8")

    def test_the_cookies_extra_declares_only_the_mit_backend(self) -> None:
        import tomllib

        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        extra = data["project"]["optional-dependencies"]["cookies"]
        assert all("rookiepy" in dep for dep in extra), extra

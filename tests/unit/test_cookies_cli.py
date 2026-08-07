"""Unit tests for ``scrapper-tool cookies`` — the extraction CLI.

Covers the exit-code contract (0 found / 1 none / 2 usage / 3 no backend), the
consent gate, and the two things that would turn this command into a
credential leak: printing values without being asked to, and writing a jar
somebody else can read.

The browser store is always faked. Nothing here touches a real profile.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from scrapper_tool import _cookies_cli
from scrapper_tool import cli as cli_module

_ROWS = [
    {
        "domain": ".example.com",
        "path": "/",
        "name": "session",
        "value": "TOP-SECRET",
        "expires": 1800000000,
        "secure": True,
        "http_only": True,
    },
    {
        "domain": "example.com",
        "path": "/",
        "name": "csrf",
        "value": "csrf-value",
        "expires": 0,
        "secure": True,
        "http_only": False,
    },
]


@pytest.fixture(autouse=True)
def _not_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI runs in a container; the guard would otherwise short-circuit every test."""
    monkeypatch.setattr(_cookies_cli, "_in_container", lambda: False)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _cookies_cli, "read_browser_cookies", lambda domain, browser=None: list(_ROWS)
    )


@pytest.fixture
def jar_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "jars"
    monkeypatch.setenv("SCRAPPER_TOOL_COOKIE_DIR", str(target))
    return target


def run(argv: list[str]) -> int:
    return cli_module.main(["cookies", *argv])


@pytest.mark.usefixtures("fake_store")
class TestExport:
    def test_writes_a_0600_jar_and_exits_zero(
        self, jar_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run(["export", "--domain", "example.com", "--yes"]) == 0
        written = jar_dir / "example.com.json"
        assert written.is_file()
        assert stat.S_IMODE(written.stat().st_mode) == 0o600
        assert "Wrote 2 cookies" in capsys.readouterr().out

    def test_hides_values_by_default(
        self, jar_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(["export", "--domain", "example.com", "--yes"])
        out = capsys.readouterr().out
        assert "TOP-SECRET" not in out
        assert "values hidden" in out

    def test_print_values_requires_yes(self, jar_dir: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run(["export", "--domain", "example.com", "--print-values"])
        assert excinfo.value.code == 2

    def test_print_values_with_yes_reveals(
        self, jar_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(["export", "--domain", "example.com", "--print-values", "--yes"])
        assert "TOP-SECRET" in capsys.readouterr().out

    def test_json_mode_never_contains_values(
        self, jar_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(["export", "--domain", "example.com", "--yes", "--json"])
        out = capsys.readouterr().out
        payload = json.loads(out[: out.index("Wrote")] if "Wrote" in out else out)
        assert payload["count"] == 2
        assert "TOP-SECRET" not in json.dumps(payload)

    def test_refuses_to_clobber_without_force(self, jar_dir: Path) -> None:
        assert run(["export", "--domain", "example.com", "--yes"]) == 0
        assert run(["export", "--domain", "example.com", "--yes"]) == 2

    def test_force_replaces(self, jar_dir: Path) -> None:
        assert run(["export", "--domain", "example.com", "--yes"]) == 0
        assert run(["export", "--domain", "example.com", "--yes", "--force"]) == 0

    def test_netscape_format_writes_a_cookie_file(self, tmp_path: Path) -> None:
        out = tmp_path / "cookies.txt"
        code = run(
            [
                "export",
                "--domain",
                "example.com",
                "--yes",
                "--format",
                "netscape",
                "--out",
                str(out),
            ]
        )
        assert code == 0
        assert out.read_text().startswith("# Netscape HTTP Cookie File")
        assert stat.S_IMODE(out.stat().st_mode) == 0o600

    def test_header_format_is_pasteable(self, tmp_path: Path) -> None:
        out = tmp_path / "h.txt"
        run(["export", "--domain", "example.com", "--yes", "--format", "header", "--out", str(out)])
        assert out.read_text().strip() == "session=TOP-SECRET; csrf=csrf-value"

    def test_rejects_a_bad_domain(self, jar_dir: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            run(["export", "--domain", "*.example.com", "--yes"])
        assert excinfo.value.code == 2

    def test_non_tty_without_yes_aborts_without_writing(
        self, jar_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A piped invocation must not silently export a credential."""
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        assert run(["export", "--domain", "example.com"]) == 0
        assert not (jar_dir / "example.com.json").exists()


class TestNoCookies:
    def test_exit_one_when_nothing_matches(
        self, monkeypatch: pytest.MonkeyPatch, jar_dir: Path
    ) -> None:
        monkeypatch.setattr(_cookies_cli, "read_browser_cookies", lambda d, browser=None: [])
        assert run(["export", "--domain", "example.com", "--yes"]) == 1

    def test_rows_for_another_domain_do_not_leak_into_the_export(
        self, monkeypatch: pytest.MonkeyPatch, jar_dir: Path
    ) -> None:
        """A backend with looser matching must not widen what we write."""
        monkeypatch.setattr(
            _cookies_cli,
            "read_browser_cookies",
            lambda d, browser=None: [{"domain": "evil-example.com", "name": "x", "value": "v"}],
        )
        assert run(["export", "--domain", "example.com", "--yes"]) == 1


class TestBackendUnavailable:
    def test_exit_three_when_no_backend(
        self, monkeypatch: pytest.MonkeyPatch, jar_dir: Path
    ) -> None:
        from scrapper_tool._browser_cookies import BrowserCookieError

        def _boom(domain: str, browser: str | None = None) -> Any:
            raise BrowserCookieError("no backend installed")

        monkeypatch.setattr(_cookies_cli, "read_browser_cookies", _boom)
        assert run(["export", "--domain", "example.com", "--yes"]) == 3

    def test_container_short_circuits_with_a_host_side_pointer(
        self, monkeypatch: pytest.MonkeyPatch, jar_dir: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(_cookies_cli, "_in_container", lambda: True)
        monkeypatch.setattr("sys.platform", "linux")
        assert run(["export", "--domain", "example.com", "--yes"]) == 3
        assert "host" in capsys.readouterr().err


@pytest.mark.usefixtures("fake_store")
class TestSeedProfile:
    def test_writes_storage_state_into_a_0700_dir(self, tmp_path: Path) -> None:
        profile = tmp_path / "profile"
        assert (
            run(["seed-profile", "--domain", "example.com", "--profile-dir", str(profile), "--yes"])
            == 0
        )
        state = profile / "storage_state.json"
        assert state.is_file()
        assert stat.S_IMODE(profile.stat().st_mode) == 0o700
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        payload = json.loads(state.read_text())
        assert payload["origins"] == []
        assert len(payload["cookies"]) == 2

    def test_refuses_a_live_browser_profile(self, tmp_path: Path) -> None:
        profile = tmp_path / "live"
        profile.mkdir()
        (profile / "cookies.sqlite").write_text("")
        assert (
            run(["seed-profile", "--domain", "example.com", "--profile-dir", str(profile), "--yes"])
            == 2
        )

    def test_refuses_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert (
            run(
                ["seed-profile", "--domain", "example.com", "--profile-dir", str(tmp_path), "--yes"]
            )
            == 2
        )

    def test_seed_profile_never_prints_values(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        run(
            [
                "seed-profile",
                "--domain",
                "example.com",
                "--profile-dir",
                str(tmp_path / "p"),
                "--yes",
            ]
        )
        assert "TOP-SECRET" not in capsys.readouterr().out


class TestNotExposedOverMcp:
    def test_mcp_registers_no_cookie_extraction_tool(self) -> None:
        """An agent that can dump the browser cookie store is the thing not to build."""
        source = Path(_cookies_cli.__file__).resolve().parents[0] / "mcp.py"
        text = source.read_text(encoding="utf-8")
        assert "read_browser_cookies" not in text
        assert "_browser_cookies" not in text

"""Unit tests for ``scrapper_tool._extras`` — shared capability probes.

Covers:
- Package probes report False (not raise) when the optional import is absent.
- ``playwright_browsers_root`` honours ``$PLAYWRIGHT_BROWSERS_PATH``.
- ``browser_binary_present`` across every backend + both Chromium layouts.
- ``obscura_endpoint_reachable`` against a real listening socket and a dead port.
- ``agent_runnable`` is the conjunction of extra-installed and binary-present.
- ``check_browser_module`` returns 'unknown' for an unsupported backend.
- ``probe_llm`` short-circuits for unprobeable backends.
- ``INSTALL_HINTS`` covers every capability doctor can report as broken.
- **Import discipline**: importing ``_extras`` pulls in no heavy dependency.

No ``importorskip`` anywhere: absence is forced with
``monkeypatch.setitem(sys.modules, ..., None)``, which makes ``import x`` raise
``ImportError``. That keeps these tests meaningful on a machine where the
extras *are* installed — which is exactly the CI matrix row we run.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from scrapper_tool import _extras

if TYPE_CHECKING:
    from pathlib import Path


class TestPackageProbes:
    """Probes that answer 'is this Python package importable?'."""

    def test_hostile_available_false_when_scrapling_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "scrapling", None)
        assert _extras.hostile_available() is False

    def test_agent_available_false_when_agent_package_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", None)
        assert _extras.agent_available() is False

    def test_crawl4ai_available_false_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "crawl4ai", None)
        assert _extras.crawl4ai_available() is False

    def test_geoip2_available_false_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "geoip2", None)
        assert _extras.geoip2_available() is False

    def test_probes_return_bool_not_truthy(self) -> None:
        """Callers serialize these straight into JSON — they must be real bools."""
        for probe in (
            _extras.hostile_available,
            _extras.agent_available,
            _extras.crawl4ai_available,
            _extras.geoip2_available,
            _extras.cookie_backend_available,
        ):
            assert isinstance(probe(), bool), probe.__name__


class TestCookieBackendProbe:
    def test_true_when_only_rookiepy_is_importable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
            if name == "rookiepy":
                return object()  # a non-None spec is all the probe inspects
            if name == "browser_cookie3":
                return None
            return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        assert _extras.cookie_backend_available() is True

    def test_true_when_only_the_lgpl_backend_is_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """browser_cookie3 is never a declared dep, but we use it if it's already there."""
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
            if name == "rookiepy":
                return None
            if name == "browser_cookie3":
                return object()
            return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        assert _extras.cookie_backend_available() is True

    def test_false_when_neither_backend_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib.util

        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name: str, *args: object, **kwargs: object) -> object:
            if name in {"rookiepy", "browser_cookie3"}:
                return None
            return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
        assert _extras.cookie_backend_available() is False


class TestPlaywrightBrowsersRoot:
    def test_honours_env_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        assert _extras.playwright_browsers_root() == tmp_path

    def test_defaults_under_home_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        root = _extras.playwright_browsers_root()
        assert root.parts[-2:] == (".cache", "ms-playwright")


class TestBrowserBinaryPresent:
    """The probe that distinguishes 'module installed' from 'actually launchable'."""

    def test_empty_root_is_false_for_every_backend(self, tmp_path: Path) -> None:
        for browser in ("patchright", "camoufox", "scrapling"):
            assert _extras.browser_binary_present(browser, root=tmp_path) is False

    def test_finds_chromium_modern_layout(self, tmp_path: Path) -> None:
        binary = tmp_path / "chromium-1234" / "chrome-linux64" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.touch()
        assert _extras.browser_binary_present("patchright", root=tmp_path) is True

    def test_finds_chromium_legacy_layout(self, tmp_path: Path) -> None:
        binary = tmp_path / "chromium-1234" / "chrome-linux" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.touch()
        assert _extras.browser_binary_present("patchright", root=tmp_path) is True

    def test_finds_headless_shell_variant(self, tmp_path: Path) -> None:
        binary = tmp_path / "chromium_headless_shell-99" / "chrome-linux64" / "headless_shell"
        binary.parent.mkdir(parents=True)
        binary.touch()
        assert _extras.browser_binary_present("patchright", root=tmp_path) is True

    def test_camoufox_falls_back_to_playwright_firefox(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "camoufox", None)
        binary = tmp_path / "firefox-1234" / "firefox" / "firefox"
        binary.parent.mkdir(parents=True)
        binary.touch()
        assert _extras.browser_binary_present("camoufox", root=tmp_path) is True

    def test_directory_named_like_the_binary_does_not_count(self, tmp_path: Path) -> None:
        """A *directory* at the binary path must not be reported as launchable."""
        fake = tmp_path / "chromium-1234" / "chrome-linux64" / "chrome"
        fake.mkdir(parents=True)
        assert _extras.browser_binary_present("patchright", root=tmp_path) is False

    def test_unknown_browser_is_false(self, tmp_path: Path) -> None:
        assert _extras.browser_binary_present("vacuumdriver", root=tmp_path) is False

    def test_obscura_defers_to_endpoint_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_extras, "obscura_endpoint_reachable", lambda: True)
        assert _extras.browser_binary_present("obscura", root=tmp_path) is True


class TestObscuraEndpointReachable:
    def test_true_against_a_real_listening_socket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            monkeypatch.setenv("SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL", f"http://127.0.0.1:{port}")
            assert _extras.obscura_endpoint_reachable(timeout_s=2.0) is True

    def test_false_against_a_closed_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Bind then immediately close so the port is near-certainly free.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        monkeypatch.setenv("SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL", f"http://127.0.0.1:{port}")
        assert _extras.obscura_endpoint_reachable(timeout_s=0.25) is False


class TestAgentRunnable:
    def test_false_when_extra_missing_even_if_binary_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = tmp_path / "chromium-1" / "chrome-linux64" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.touch()
        monkeypatch.setattr(_extras, "agent_available", lambda: False)
        assert _extras.agent_runnable("patchright", root=tmp_path) is False

    def test_false_when_binary_missing_even_if_extra_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_extras, "agent_available", lambda: True)
        assert _extras.agent_runnable("patchright", root=tmp_path) is False

    def test_true_only_when_both(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        binary = tmp_path / "chromium-1" / "chrome-linux64" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.touch()
        monkeypatch.setattr(_extras, "agent_available", lambda: True)
        assert _extras.agent_runnable("patchright", root=tmp_path) is True


class TestCheckBrowserModule:
    def test_unknown_for_unsupported_backend(self) -> None:
        assert _extras.check_browser_module("vacuumdriver") == "unknown"

    def test_missing_when_module_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "patchright", None)
        assert _extras.check_browser_module("patchright") == "missing"


class TestProbeLlm:
    @pytest.mark.asyncio
    async def test_returns_none_pair_for_unprobeable_backends(self) -> None:
        class _Cfg:
            llm = "vllm"

        assert await _extras.probe_llm(_Cfg()) == (None, None)


class TestInstallHints:
    def test_every_hint_is_a_runnable_looking_command(self) -> None:
        for key, hint in _extras.INSTALL_HINTS.items():
            assert hint.strip() == hint, key
            assert hint, key

    def test_covers_the_capabilities_doctor_can_fault(self) -> None:
        """Doctor builds its Fixes block from these keys — a missing key is a silent gap."""
        required = {
            "hostile",
            "llm-agent",
            "cookies",
            "camoufox-binary",
            "geoip2",
            "ollama",
        }
        assert required <= set(_extras.INSTALL_HINTS)


class TestImportDiscipline:
    """``_extras`` must stay importable on a bare install.

    ``doctor`` runs on ``pip install scrapper-tool`` with no extras, so a heavy
    module sneaking into ``_extras``'s module-level imports would break the one
    command whose whole job is to explain a broken install.
    """

    def test_importing_extras_pulls_in_no_heavy_dependency(self) -> None:
        script = textwrap.dedent(
            """
            import sys

            # Poison the *optional* deps: any module-level import of these
            # inside _extras raises rather than silently succeeding on a dev box
            # where they happen to be installed. Core dependencies (httpx,
            # curl-cffi, selectolax, extruct, pydantic) are deliberately absent
            # from this list — they are present in every install, including the
            # bare one doctor has to run on, and ``scrapper_tool/__init__``
            # imports curl_cffi transitively via ``scrapper_tool.http``.
            for name in (
                "fastapi", "uvicorn", "crawl4ai", "scrapling", "camoufox",
                "patchright", "browser_use", "playwright", "rookiepy",
            ):
                sys.modules[name] = None

            import scrapper_tool._extras as ex

            # The probes must still *run* — returning False, not exploding.
            assert ex.hostile_available() is False
            assert ex.crawl4ai_available() is False
            assert isinstance(ex.playwright_browsers_root().as_posix(), str)
            assert ex.check_browser_module("patchright") == "missing"
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

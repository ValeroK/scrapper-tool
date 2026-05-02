"""Unit tests for ``scrapper_tool.http_server``.

The HTTP sidecar requires the ``[http]`` extra (FastAPI + uvicorn). The
entire test module is skipped when the extra is not installed.

Tests use ``httpx.AsyncClient`` with FastAPI's ``ASGITransport`` so no
real server is started — the in-process app instance is what we
exercise. Real network calls (``request_with_ladder``, ``agent_extract``,
``agent_browse``) are monkeypatched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip the whole module if FastAPI / uvicorn are not installed.
pytest.importorskip(
    "fastapi",
    reason="HTTP server tests require the [http] extra (pip install scrapper-tool[http]).",
)
pytest.importorskip("uvicorn")

from httpx import ASGITransport, AsyncClient

from scrapper_tool import (
    __version__,
    http_server,
)

# --- Fixtures -------------------------------------------------------------


@pytest.fixture()
def app_no_auth() -> Any:
    return http_server._build_app(api_key=None, cors_origins=["*"], serve_docs=True)


@pytest.fixture()
def app_with_key() -> Any:
    return http_server._build_app(api_key="test-secret", cors_origins=["*"])


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _make_response(
    *,
    status_code: int = 200,
    text: str = "<html>ok</html>",
    url: str = "https://example.com/",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock response that mimics httpx.Response / curl_cffi response shape."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.url = url
    resp.headers = headers or {"content-type": "text/html"}
    resp.json = MagicMock(return_value={})
    return resp


_PRODUCT_HTML = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Widget",
 "sku":"X1","offers":{"@type":"Offer","price":"19.99","priceCurrency":"USD"}}
</script></head><body></body></html>"""


# --- Operational endpoints ------------------------------------------------


class TestHealth:
    @pytest.mark.asyncio
    async def test_returns_ok(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestVersion:
    @pytest.mark.asyncio
    async def test_returns_version_and_capabilities(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/version")
        assert resp.status_code == 200
        body = resp.json()
        assert body["version"] == __version__
        assert "A" in body["patterns"]
        assert "E" in body["patterns"]
        assert "agent_available" in body
        assert "hostile_available" in body


class TestReady:
    @pytest.mark.asyncio
    async def test_returns_status_object(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] in {"ready", "degraded", "not_ready"}
        assert body["version"] == __version__
        assert "checks" in body
        assert "agent_installed" in body["checks"]


# --- /fetch ---------------------------------------------------------------


class TestFetch:
    @pytest.mark.asyncio
    async def test_success_runs_pattern_b_and_c_by_default(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://example.com/p"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status_code"] == 200
        assert body["profile"] == "chrome133a"
        assert body["product"] is not None
        assert body["product"]["name"] == "Widget"
        assert body["product"]["price"] == "19.99"
        assert body["microdata_price"] is None  # no <meta itemprop="price"> in fixture
        assert body["blocked"] is False
        assert "headers" in body

    @pytest.mark.asyncio
    async def test_extract_structured_false_skips_pattern_b_c(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/fetch", json={"url": "https://example.com/p", "extract_structured": False}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["product"] is None
        assert body["json_ld"] is None

    @pytest.mark.asyncio
    async def test_blocked_returns_422(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("all profiles 403")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://blocked.com"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "blocked"
        assert body["blocked"] is True


# --- /scrape --------------------------------------------------------------


class TestScrape:
    @pytest.mark.asyncio
    async def test_auto_succeeds_on_a_b_c(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://example.com/p"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "a_b_c"
        assert body["pattern_attempts"] == ["a_b_c"]
        assert body["product"] is not None
        assert body["product"]["name"] == "Widget"
        assert body["blocked"] is False

    @pytest.mark.asyncio
    async def test_auto_escalates_to_e1_when_blocked(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        # Mock the agent layer
        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "Protected", "price": 49.99}
        fake_result.final_url = "https://protected.com/p"
        fake_result.rendered_markdown = "# Protected"
        fake_result.screenshots = None
        fake_result.tokens_used = 100
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0

        agent_extract_mock = AsyncMock(return_value=fake_result)
        agent_module = MagicMock()
        agent_module.AgentConfig = MagicMock()
        agent_module.AgentConfig.from_env = MagicMock(
            return_value=MagicMock(merged=lambda **_: MagicMock())
        )
        agent_module.agent_extract = agent_extract_mock

        import sys

        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={"url": "https://protected.com/p", "schema_json": {"name": "str"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["pattern_attempts"] == ["a_b_c", "e1"]
        assert body["data"]["name"] == "Protected"

    @pytest.mark.asyncio
    async def test_fully_blocked_returns_422(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import AgentBlockedError, BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("all profiles 403")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        agent_extract_mock = AsyncMock(side_effect=AgentBlockedError("e1 blocked"))
        agent_browse_mock = AsyncMock(side_effect=AgentBlockedError("e2 blocked"))
        agent_module = MagicMock()
        agent_module.AgentConfig = MagicMock()
        agent_module.AgentConfig.from_env = MagicMock(
            return_value=MagicMock(merged=lambda **_: MagicMock())
        )
        agent_module.agent_extract = agent_extract_mock
        agent_module.agent_browse = agent_browse_mock

        import sys

        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://blocked.com"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "blocked"
        assert "All patterns blocked" in body["detail"]


# --- /extract — agent extra not installed -> 503 -------------------------


class TestExtractAgentMissing:
    @pytest.mark.asyncio
    async def test_returns_503_when_agent_extra_missing(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the agent module not importable.
        import sys

        # Remove any cached agent module.
        for name in list(sys.modules):
            if name == "scrapper_tool.agent" or name.startswith("scrapper_tool.agent."):
                monkeypatch.delitem(sys.modules, name, raising=False)

        # Force the import inside the handler to fail.
        import builtins

        original_import = builtins.__import__

        def patched_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "scrapper_tool.agent":
                raise ImportError("scrapper_tool.agent not installed (test simulation)")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", patched_import)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/extract",
                json={"url": "https://example.com", "schema_json": {"x": "str"}},
            )
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "configuration_error"
        assert "[llm-agent]" in body["detail"]


# --- Auth -----------------------------------------------------------------


class TestAuth:
    @pytest.mark.asyncio
    async def test_health_unauth_when_key_set(self, app_with_key: Any) -> None:
        async with _client(app_with_key) as client:
            resp = await client.get("/health")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_fetch_rejected_without_key(self, app_with_key: Any) -> None:
        async with _client(app_with_key) as client:
            resp = await client.post("/fetch", json={"url": "https://example.com"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_fetch_accepted_with_correct_key(
        self, app_with_key: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text="<html></html>", url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_with_key) as client:
            resp = await client.post(
                "/fetch",
                json={"url": "https://example.com"},
                headers={"X-API-Key": "test-secret"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_fetch_rejected_with_wrong_key(self, app_with_key: Any) -> None:
        async with _client(app_with_key) as client:
            resp = await client.post(
                "/fetch",
                json={"url": "https://example.com"},
                headers={"X-API-Key": "wrong"},
            )
        assert resp.status_code == 401


# --- ConfigurationError mapping ------------------------------------------


class TestConfigurationError:
    @pytest.mark.asyncio
    async def test_maps_to_503(self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from scrapper_tool.errors import ConfigurationError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise ConfigurationError("patchright binary not found")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://example.com"})
        assert resp.status_code == 503
        body = resp.json()
        assert body["error"] == "configuration_error"
        assert "patchright" in body["detail"]


# --- Override merging ------------------------------------------------------


class TestBuildOverrides:
    def test_skips_none_and_default_headful(self) -> None:
        class Req:
            browser = "patchright"
            model = None
            timeout_s = 60.0
            max_steps = None
            headful = False

        result = http_server._build_overrides(Req())
        assert result == {"browser": "patchright", "timeout_s": 60.0}

    def test_keeps_headful_when_true(self) -> None:
        class Req:
            browser = None
            model = "qwen3-vl:8b"
            timeout_s = None
            max_steps = 30
            headful = True

        result = http_server._build_overrides(Req())
        assert result == {"model": "qwen3-vl:8b", "max_steps": 30, "headful": True}


# --- OpenAPI spec --------------------------------------------------------


class TestOpenAPI:
    @pytest.mark.asyncio
    async def test_openapi_json_served(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec["info"]["version"] == __version__
        # Verify all expected paths
        paths = spec.get("paths", {})
        for path in ("/health", "/version", "/ready", "/scrape", "/fetch", "/extract", "/browse"):
            assert path in paths, f"OpenAPI spec missing {path}"

    @pytest.mark.asyncio
    async def test_docs_served_by_default(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/docs")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_docs_disabled_when_serve_docs_false(self) -> None:
        app = http_server._build_app(api_key=None, cors_origins=["*"], serve_docs=False)
        async with _client(app) as client:
            resp = await client.get("/docs")
        assert resp.status_code == 404


# --- /extract and /browse with mocked agent ------------------------------


def _mock_agent_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    extract_result: Any = None,
    browse_result: Any = None,
    extract_side_effect: BaseException | None = None,
    browse_side_effect: BaseException | None = None,
) -> None:
    """Install a mock 'scrapper_tool.agent' module."""
    import sys

    agent_module = MagicMock()
    agent_module.AgentConfig = MagicMock()
    agent_module.AgentConfig.from_env = MagicMock(
        return_value=MagicMock(merged=lambda **_: MagicMock())
    )
    if extract_side_effect:
        agent_module.agent_extract = AsyncMock(side_effect=extract_side_effect)
    else:
        agent_module.agent_extract = AsyncMock(return_value=extract_result)
    if browse_side_effect:
        agent_module.agent_browse = AsyncMock(side_effect=browse_side_effect)
    else:
        agent_module.agent_browse = AsyncMock(return_value=browse_result)
    monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)


def _fake_agent_result(mode: str = "extract", *, blocked: bool = False) -> MagicMock:
    r = MagicMock()
    r.mode = mode
    r.data = {"name": "Widget"}
    r.final_url = "https://example.com/p"
    r.rendered_markdown = "# Widget"
    r.screenshots = None
    r.actions = []
    r.tokens_used = 100
    r.steps_used = 1 if mode == "extract" else 5
    r.blocked = blocked
    r.error = "blocked" if blocked else None
    r.duration_s = 1.0
    r.model_dump = MagicMock(
        return_value={
            "mode": mode,
            "data": r.data,
            "final_url": r.final_url,
            "rendered_markdown": r.rendered_markdown,
            "screenshots": None,
            "actions": [],
            "tokens_used": r.tokens_used,
            "blocked": blocked,
            "error": r.error,
            "duration_s": r.duration_s,
            "steps_used": r.steps_used,
        }
    )
    return r


class TestExtractEndpoint:
    @pytest.mark.asyncio
    async def test_calls_agent_extract_and_returns_result(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_agent_module(monkeypatch, extract_result=_fake_agent_result("extract"))

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/extract",
                json={
                    "url": "https://example.com/p",
                    "schema_json": {"name": "str"},
                    "model": "qwen3-vl:8b",
                    "browser": "patchright",
                    "timeout_s": 60.0,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "extract"
        assert body["data"]["name"] == "Widget"


class TestBrowseEndpoint:
    @pytest.mark.asyncio
    async def test_calls_agent_browse_and_returns_result(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_agent_module(monkeypatch, browse_result=_fake_agent_result("browse"))

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/browse",
                json={
                    "url": "https://example.com/login",
                    "instruction": "Log in and grab the dashboard",
                    "schema_json": {"items": "list"},
                    "max_steps": 10,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "browse"


class TestScrapeBrowseFallback:
    @pytest.mark.asyncio
    async def test_scrape_falls_through_to_e2_when_e1_blocked(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import AgentBlockedError, BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _mock_agent_module(
            monkeypatch,
            extract_side_effect=AgentBlockedError("e1 blocked"),
            browse_result=_fake_agent_result("browse"),
        )

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://protected.com/p"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e2"
        assert body["pattern_attempts"] == ["a_b_c", "e1", "e2"]

    @pytest.mark.asyncio
    async def test_scrape_mode_extract_skips_a_b_c(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _mock_agent_module(monkeypatch, extract_result=_fake_agent_result("extract"))

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://example.com/p",
                    "mode": "extract",
                    "schema_json": {"name": "str"},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert "a_b_c" not in body["pattern_attempts"]

    @pytest.mark.asyncio
    async def test_scrape_mode_fetch_returns_a_b_c_only(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text="<html>plain</html>", url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape", json={"url": "https://example.com/p", "mode": "fetch"}
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "a_b_c"


# --- Readiness probes (mock httpx for LLM probe) ------------------------


class TestReadinessProbes:
    @pytest.mark.asyncio
    async def test_check_browser_module_unknown_for_unsupported(self) -> None:
        # 'unknown' branch
        result = http_server._check_browser_module("vacuumdriver")
        assert result == "unknown"

    @pytest.mark.asyncio
    async def test_probe_llm_returns_none_for_unknown_backend(self) -> None:
        cfg = MagicMock(llm="llama_cpp", ollama_url="http://localhost", model="model")
        reachable, available = await http_server._probe_llm(cfg)
        assert reachable is None
        assert available is None


# --- main() / CLI --------------------------------------------------------


class TestCliMain:
    def test_help_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc_info:
            http_server.main(["--help"])
        # argparse exits 0 on --help
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "scrapper-tool-serve" in captured.out

    def test_main_calls_uvicorn_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_mock = MagicMock()
        # Patch the uvicorn lookup inside main()
        import sys

        fake_uvicorn = MagicMock()
        fake_uvicorn.run = run_mock
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        # Also clear any HTTP_API_KEY so we test the no-auth path
        monkeypatch.delenv("SCRAPPER_TOOL_HTTP_API_KEY", raising=False)
        monkeypatch.delenv("SCRAPPER_TOOL_HTTP_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("SCRAPPER_TOOL_HTTP_DOCS", raising=False)

        exit_code = http_server.main(["--port", "5793"])
        assert exit_code == 0
        run_mock.assert_called_once()
        kwargs = run_mock.call_args.kwargs
        assert kwargs["port"] == 5793

    def test_main_with_api_key_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        run_mock = MagicMock()
        import sys

        fake_uvicorn = MagicMock()
        fake_uvicorn.run = run_mock
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
        monkeypatch.setenv("SCRAPPER_TOOL_HTTP_API_KEY", "secret123")
        monkeypatch.setenv("SCRAPPER_TOOL_HTTP_DOCS", "0")
        monkeypatch.setenv("SCRAPPER_TOOL_HTTP_CORS_ORIGINS", "https://app.example.com")

        exit_code = http_server.main([])
        assert exit_code == 0
        run_mock.assert_called_once()


# --- Exception handlers --------------------------------------------------


class TestExceptionHandlers:
    @pytest.mark.asyncio
    async def test_agent_timeout_maps_to_504(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import AgentTimeoutError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise AgentTimeoutError("agent loop exceeded timeout")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://slow.com"})
        assert resp.status_code == 504
        assert resp.json()["error"] == "agent_timeout"

    @pytest.mark.asyncio
    async def test_agent_llm_maps_to_502(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import AgentLLMError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise AgentLLMError("Ollama unreachable")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://example.com"})
        assert resp.status_code == 502
        assert resp.json()["error"] == "llm_unreachable"

    @pytest.mark.asyncio
    async def test_vendor_http_error_maps_to_502(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import VendorHTTPError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise VendorHTTPError("upstream returned 503 after 3 retries")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://example.com"})
        assert resp.status_code == 502
        assert resp.json()["error"] == "vendor_http_error"

    @pytest.mark.asyncio
    async def test_agent_error_maps_to_500(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import AgentError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise AgentError("unspecified agent failure")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://example.com"})
        assert resp.status_code == 500
        assert resp.json()["error"] == "agent_error"

    @pytest.mark.asyncio
    async def test_scraping_error_maps_to_500(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import ScrapingError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise ScrapingError("generic scraping failure")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/fetch", json={"url": "https://example.com"})
        assert resp.status_code == 500
        assert resp.json()["error"] == "scraping_error"


# --- More /scrape paths -------------------------------------------------


class TestScrapeForcedModes:
    @pytest.mark.asyncio
    async def test_mode_fetch_propagates_blocked_error(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("403 from all profiles")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape", json={"url": "https://blocked.com", "mode": "fetch"}
            )
        assert resp.status_code == 422
        assert resp.json()["error"] == "blocked"

    @pytest.mark.asyncio
    async def test_mode_extract_propagates_blocked(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import AgentBlockedError

        _mock_agent_module(monkeypatch, extract_side_effect=AgentBlockedError("e1 blocked"))

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://protected.com",
                    "mode": "extract",
                    "schema_json": {"name": "str"},
                },
            )
        assert resp.status_code == 422


# --- Probe LLM (ollama path) --------------------------------------------


class TestLLMProbe:
    @pytest.mark.asyncio
    async def test_probe_ollama_reachable_with_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Mock httpx.AsyncClient to return a model list
        import httpx

        class FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"models": [{"name": "qwen3-vl:8b"}, {"name": "llama3:8b"}]}

        class FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def get(self, url: str) -> FakeResponse:
                return FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        cfg = MagicMock(llm="ollama", ollama_url="http://localhost:11434", model="qwen3-vl:8b")
        reachable, available = await http_server._probe_llm(cfg)
        assert reachable is True
        assert available is True

    @pytest.mark.asyncio
    async def test_probe_ollama_unreachable_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        class FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def get(self, url: str) -> Any:
                raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        cfg = MagicMock(llm="ollama", ollama_url="http://localhost:11434", model="qwen3-vl:8b")
        reachable, available = await http_server._probe_llm(cfg)
        assert reachable is False
        assert available is False

    @pytest.mark.asyncio
    async def test_probe_openai_compat_reachable_with_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        class FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"data": [{"id": "google/gemma-4-e4b"}]}

        class FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def get(self, url: str) -> FakeResponse:
                return FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        cfg = MagicMock(
            llm="openai_compat",
            ollama_url="http://localhost:6543",
            model="google/gemma-4-e4b",
        )
        reachable, available = await http_server._probe_llm(cfg)
        assert reachable is True
        assert available is True


# --- Browser module checks ----------------------------------------------


class TestBrowserModuleCheck:
    def test_patchright_present_or_missing(self) -> None:
        # patchright ships with [llm-agent]/[full]. Other matrix entries
        # (dev,agent,http; dev,hostile,agent,http) won't have it.
        result = http_server._check_browser_module("patchright")
        assert result in {"ok", "missing"}

    def test_camoufox_present_or_missing(self) -> None:
        # Camoufox is in [llm-agent]/[full]; missing in lighter matrix entries.
        result = http_server._check_browser_module("camoufox")
        assert result in {"ok", "missing"}

    def test_scrapling_present_or_missing(self) -> None:
        # Scrapling is in [hostile]/[full]; missing in lighter matrix entries.
        result = http_server._check_browser_module("scrapling")
        assert result in {"ok", "missing"}

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
        # Force the [hostile] extra to look absent so the cascade skips
        # Pattern D and falls through directly to E1 (the original
        # pre-1.1.3 behaviour this test was written against).
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

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
        # Pin Pattern D out of the cascade so this stays a 3-attempt path
        # (a_b_c -> e1 -> e2) — see TestScrapeWithPatternD for the D-included path.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

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
        # Skip Pattern D so the chain remains a_b_c -> e1 -> e2.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)
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

            async def get(self, url: str, **kwargs: Any) -> FakeResponse:
                return FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        cfg = MagicMock(
            llm="openai_compat",
            ollama_url="http://localhost:6543",
            model="google/gemma-4-e4b",
            llm_api_key=None,
        )
        reachable, available = await http_server._probe_llm(cfg)
        assert reachable is True
        assert available is True

    @pytest.mark.asyncio
    async def test_probe_openai_compat_sends_auth_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_probe_llm must forward Authorization: Bearer when llm_api_key is set."""
        import httpx
        from pydantic import SecretStr

        captured_headers: dict[str, str] = {}

        class FakeResponse:
            status_code = 200

            def json(self) -> dict[str, Any]:
                return {"data": [{"id": "gpt-4o"}]}

        class FakeClient:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> FakeClient:
                return self

            async def __aexit__(self, *args: Any) -> None:
                return None

            async def get(self, url: str, **kwargs: Any) -> FakeResponse:
                captured_headers.update(kwargs.get("headers") or {})
                return FakeResponse()

        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        cfg = MagicMock(
            llm="openai_compat",
            ollama_url="http://api.example.com",
            model="gpt-4o",
            llm_api_key=SecretStr("sk-test-key"),
        )
        reachable, available = await http_server._probe_llm(cfg)
        assert reachable is True
        assert available is True
        assert captured_headers.get("Authorization") == "Bearer sk-test-key"


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


# --- v1.1.2: agent_runnable / on-disk binary probe -------------------------


class TestAgentRunnable:
    """``/ready`` reports ``agent_runnable`` separately from ``agent_installed``.

    Pre-1.1.2 the published image declared ``agent_installed=true`` (Python
    extra importable) while the Firefox binary was not on disk, leaving
    ``/scrape mode=auto`` 500-ing on every E1/E2 escalation. ``agent_runnable``
    closes that gap by probing the on-disk binary.
    """

    def test_browser_binary_present_returns_false_for_empty_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Empty cache dir → no firefox/chromium binary on disk → False.
        monkeypatch.setattr(http_server, "_playwright_browsers_root", lambda: tmp_path)
        assert http_server._browser_binary_present("patchright") is False
        assert http_server._browser_binary_present("camoufox") is False
        assert http_server._browser_binary_present("scrapling") is False

    def test_browser_binary_present_finds_chromium_for_patchright(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Synthesize the canonical Patchright Chromium layout — Playwright
        # writes ``chrome-linux64/`` on Linux x64 since revision ~1100.
        chrome_path = tmp_path / "chromium-1208" / "chrome-linux64" / "chrome"
        chrome_path.parent.mkdir(parents=True)
        chrome_path.write_text("#!/bin/sh\n# fake chromium\n")
        monkeypatch.setattr(http_server, "_playwright_browsers_root", lambda: tmp_path)
        assert http_server._browser_binary_present("patchright") is True

    def test_browser_binary_present_finds_chromium_legacy_layout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Older Playwright images used ``chrome-linux/`` (no ``64``);
        # the probe accepts both so old caches still register.
        chrome_path = tmp_path / "chromium-1100" / "chrome-linux" / "chrome"
        chrome_path.parent.mkdir(parents=True)
        chrome_path.write_text("#!/bin/sh\n# fake chromium (legacy layout)\n")
        monkeypatch.setattr(http_server, "_playwright_browsers_root", lambda: tmp_path)
        assert http_server._browser_binary_present("patchright") is True

    def test_browser_binary_present_finds_firefox_for_camoufox_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Camoufox can run with Playwright Firefox under ms-playwright/firefox-*.
        ff_path = tmp_path / "firefox-1509" / "firefox" / "firefox"
        ff_path.parent.mkdir(parents=True)
        ff_path.write_text("#!/bin/sh\n# fake firefox\n")
        monkeypatch.setattr(http_server, "_playwright_browsers_root", lambda: tmp_path)
        assert http_server._browser_binary_present("camoufox") is True

    def test_browser_binary_present_unknown_browser_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        monkeypatch.setattr(http_server, "_playwright_browsers_root", lambda: tmp_path)
        # Unknown browser → conservative False (forces /ready degraded so
        # the misconfiguration surfaces, doesn't silently pass).
        assert http_server._browser_binary_present("not-a-real-browser") is False

    @pytest.mark.asyncio
    async def test_ready_includes_agent_runnable(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # Empty cache → agent_runnable=false → /ready status=degraded
        # (when LLM is unreachable, which it will be in CI).
        monkeypatch.setattr(http_server, "_playwright_browsers_root", lambda: tmp_path)
        async with _client(app_no_auth) as client:
            resp = await client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert "agent_runnable" in body["checks"], (
            "v1.1.2: /ready must report agent_runnable separately from agent_installed"
        )
        # When the Python extra IS installed but no binary on disk,
        # agent_installed should be True and agent_runnable should be False.
        if body["checks"]["agent_installed"] is True:
            assert body["checks"]["agent_runnable"] is False

    @pytest.mark.asyncio
    async def test_ready_status_degraded_when_agent_not_runnable(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        # The whole point of the v1.1.2 change: empty browser cache must
        # NOT yield status=ready, no matter how healthy the rest looks.
        monkeypatch.setattr(http_server, "_playwright_browsers_root", lambda: tmp_path)
        async with _client(app_no_auth) as client:
            resp = await client.get("/ready")
        body = resp.json()
        if body["checks"]["agent_installed"] is True:
            assert body["status"] != "ready", (
                "agent_runnable=false must downgrade status to 'degraded' (or 'not_ready')"
            )


# --- v1.1.2: /scrape mode=auto over-escalation fix --------------------------


class TestScrapeAutoNoOverescalation:
    """v1.1.2 — schema_json + readable A/B/C output is success, not escalation.

    Pre-1.1.2 ``mode=auto`` always escalated to E1 when ``schema_json`` was
    set, regardless of whether A/B/C had returned a readable page with
    structured signal. That conflated "blocked" with "schema didn't match"
    and burned LLM budget on pages the caller could parse locally.
    """

    @pytest.mark.asyncio
    async def test_auto_with_schema_does_not_escalate_when_page_readable(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A/B/C returns a 200 page with JSON-LD. Pre-1.1.2: escalates to E1.
        # v1.1.2: stays on a_b_c (schema_json + page_readable + has_any_signal).
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://example.com/p",
                    "schema_json": {"name": "str", "price": "float"},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "a_b_c", (
            "v1.1.2: schema_json + readable A/B/C output is success, not escalation. "
            "Set force_llm_extract=true for the old always-escalate behaviour."
        )
        assert body["pattern_attempts"] == ["a_b_c"]
        # The structured signal that justified the success classification.
        assert body["json_ld"] is not None or body["product"] is not None

    @pytest.mark.asyncio
    async def test_auto_with_schema_and_force_llm_extract_does_escalate(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Opt-in to legacy behaviour: force_llm_extract=true → E1 even when
        # A/B/C had a readable page.
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        # Skip Pattern D so the assertion stays a 2-step cascade.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        # Mock the agent layer so we can observe the escalation.
        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "Widget", "price": 19.99}
        fake_result.final_url = "https://example.com/p"
        fake_result.rendered_markdown = "# Widget"
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 0.5

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
                json={
                    "url": "https://example.com/p",
                    "schema_json": {"name": "str"},
                    "force_llm_extract": True,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e1", (
            "force_llm_extract=true must restore pre-1.1.2 escalation behaviour"
        )
        assert body["pattern_attempts"] == ["a_b_c", "e1"]

    @pytest.mark.asyncio
    async def test_auto_with_schema_escalates_when_a_b_c_returns_blank_page(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A/B/C returns a 200 page with NO structured signal (blank HTML).
        # The page is readable but has nothing the caller can post-process,
        # so escalation to E1 IS warranted.
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return (
                _make_response(text="<html><body>nothing here</body></html>", url=url),
                "chrome133a",
            )

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        # Skip Pattern D — this test only exercises the v1.1.2
        # blank-page-escalates rule, not the new D step.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"x": 1}
        fake_result.final_url = "https://example.com/empty"
        fake_result.rendered_markdown = "empty"
        fake_result.screenshots = None
        fake_result.tokens_used = 10
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 0.3

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
                json={"url": "https://example.com/empty", "schema_json": {"x": "int"}},
            )
        assert resp.status_code == 200
        body = resp.json()
        # No JSON-LD, no microdata, no auto-detected product, no force flag →
        # escalation IS the right call.
        assert body["pattern_used"] == "e1"


# --- v1.1.3: Pattern D in the auto-cascade ---------------------------------


class _FakeScraplingResponse:
    """Stand-in for Scrapling's StealthyFetcher response object."""

    def __init__(self, *, html: str, status: int = 200, url: str = "https://hostile.com/p"):
        self.html_content = html
        self.status = status
        self.url = url


class _FakeFetcher:
    """Async context manager mimicking ``scrapper_tool.patterns.d.hostile_client``."""

    def __init__(self, response: Any | BaseException) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeFetcher:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def async_fetch(self, url: str, **kwargs: Any) -> Any:
        # Accept any kwargs (solve_cloudflare, network_idle, user_data_dir, ...).
        # Tests that need to assert on kwargs subclass and override.
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _install_fake_hostile_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: Any | BaseException,
) -> None:
    """Replace patterns.d.hostile_client with a fake yielding ``response``."""
    import scrapper_tool.patterns.d as d_mod

    def fake_hostile_client(**kwargs: Any) -> _FakeFetcher:
        return _FakeFetcher(response)

    monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)
    # Force the import-probe to True so the cascade actually invokes D.
    monkeypatch.setattr(http_server, "_hostile_available", lambda: True)


class TestScrapeWithPatternD:
    """v1.1.3 — auto cascade now invokes Pattern D between A/B/C and E1.

    Pre-1.1.3 the cascade documented A/B/C -> D -> E1 -> E2 in every doc
    and error message but actually ran A/B/C -> E1 -> E2; Pattern D was
    unreachable from /scrape and auto_scrape. This class pins the new
    behaviour: D is invoked when [hostile] is installed, skipped silently
    (with hostile_skipped=true on the response) when it isn't, and the
    cascade falls through to E1 if D itself fails.
    """

    @pytest.mark.asyncio
    async def test_d_succeeds_when_a_b_c_blocked_and_hostile_installed(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("all profiles 403")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(
            monkeypatch,
            response=_FakeScraplingResponse(html=_PRODUCT_HTML),
        )

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://hostile.com/p"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "d"
        assert body["pattern_attempts"] == ["a_b_c", "d"]
        assert body["product"] is not None
        assert body["product"]["name"] == "Widget"
        assert body["hostile_skipped"] is False

    @pytest.mark.asyncio
    async def test_d_skipped_when_hostile_extra_missing(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        # Pretend [hostile] isn't installed — D step must skip silently
        # (no append to pattern_attempts) and surface hostile_skipped=true.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "Protected"}
        fake_result.final_url = "https://protected.com/p"
        fake_result.rendered_markdown = "# Protected"
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://protected.com/p"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["pattern_attempts"] == ["a_b_c", "e1"], (
            "When [hostile] is missing, the D step appends nothing to attempts"
        )
        assert body["hostile_skipped"] is True

    @pytest.mark.asyncio
    async def test_d_failure_falls_through_to_e1(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        # D itself raises (Scrapling can't solve, network error, etc.) →
        # cascade must record "d" in attempts and continue to E1.
        _install_fake_hostile_client(
            monkeypatch,
            response=RuntimeError("scrapling: cloudflare turnstile unsolvable"),
        )

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "Salvaged"}
        fake_result.final_url = "https://hostile.com/p"
        fake_result.rendered_markdown = "# Salvaged"
        fake_result.screenshots = None
        fake_result.tokens_used = 200
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 2.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://hostile.com/p"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["pattern_attempts"] == ["a_b_c", "d", "e1"]
        assert body["hostile_skipped"] is False

    @pytest.mark.asyncio
    async def test_force_llm_extract_short_circuits_past_d(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A/B/C returns a readable page with structured signal but
        # force_llm_extract=true makes A/B/C "fail" the success classifier.
        # Pattern D is also readable here, so without the short-circuit it
        # would succeed and return pattern_used="d". The intent of
        # force_llm_extract is to reach the LLM — D must inherit the same
        # opt-out and let the cascade reach E1.
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(
            monkeypatch,
            response=_FakeScraplingResponse(html=_PRODUCT_HTML),
        )

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "Widget", "price": 19.99}
        fake_result.final_url = "https://example.com/p"
        fake_result.rendered_markdown = "# Widget"
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 0.5
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://example.com/p",
                    "schema_json": {"name": "str"},
                    "force_llm_extract": True,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e1", (
            "force_llm_extract=true must skip both A/B/C success AND D success "
            "and reach the LLM — D inherits the same opt-out."
        )
        # A/B/C completed, D fetched + extracted but the classifier rejected
        # both, so attempts records both before reaching E1.
        assert body["pattern_attempts"] == ["a_b_c", "d", "e1"]

    @pytest.mark.asyncio
    async def test_mode_fetch_does_not_invoke_d(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mode=fetch is the explicit "cheap path" contract: A/B/C only,
        # no Pattern D, no Pattern E. Even when [hostile] is installed.
        # If A/B/C is blocked under mode=fetch, the request fails; we do
        # NOT silently pull in Scrapling.
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("403")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        # Even though [hostile] is "installed", mode=fetch must not call it.
        _install_fake_hostile_client(
            monkeypatch,
            response=_FakeScraplingResponse(html=_PRODUCT_HTML),
        )

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape", json={"url": "https://blocked.com", "mode": "fetch"}
            )
        # mode=fetch propagates BlockedError → 422.
        assert resp.status_code == 422
        body = resp.json()
        assert body["error"] == "blocked"

    @pytest.mark.asyncio
    async def test_ready_warnings_when_hostile_missing(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # /ready must surface a 'hostile_not_installed' warning when the
        # extra is absent so operators see Pattern D will be skipped.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        async with _client(app_no_auth) as client:
            resp = await client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["checks"]["hostile_installed"] is False
        warnings = body["checks"].get("warnings") or []
        assert any("hostile_not_installed" in w for w in warnings), (
            f"expected a hostile_not_installed warning in {warnings}"
        )

    @pytest.mark.asyncio
    async def test_ready_no_hostile_warning_when_installed(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(http_server, "_hostile_available", lambda: True)

        async with _client(app_no_auth) as client:
            resp = await client.get("/ready")
        body = resp.json()
        assert body["checks"]["hostile_installed"] is True
        warnings = body["checks"].get("warnings") or []
        assert not any("hostile_not_installed" in w for w in warnings)


# --- v1.2.0: is_structured response field ----------------------------------


class TestIsStructuredField:
    """v1.2.0 — every /scrape response carries the sidecar's success verdict.

    Pre-1.2.0 every downstream consumer had to derive "is this a real payload
    or LLM narration of failure?" from response shape. The sidecar already
    classifies internally via ``_classify_extraction_success`` (for A/B/C and
    D) and via ``_is_e_tier_structured`` (for E1/E2). This class pins the
    contract: ``is_structured: true`` iff the sidecar accepted the page.
    """

    def test_helper_truth_table(self) -> None:
        # Pure-function test — no fixtures.
        assert http_server._is_e_tier_structured({"name": "x"}, False) is True
        assert http_server._is_e_tier_structured({"_raw": "..."}, False) is False
        assert http_server._is_e_tier_structured(None, False) is False
        assert http_server._is_e_tier_structured({"name": "x"}, True) is False
        # List payloads (LLM returned an array of records) are structured.
        assert http_server._is_e_tier_structured([{"a": 1}], False) is True
        # Empty dict is technically structured — has no _raw marker.
        assert http_server._is_e_tier_structured({}, False) is True

    @pytest.mark.asyncio
    async def test_a_b_c_success_carries_is_structured_true(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://example.com/p"})
        body = resp.json()
        assert body["pattern_used"] == "a_b_c"
        assert body["is_structured"] is True

    @pytest.mark.asyncio
    async def test_d_success_carries_is_structured_true(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://hostile.com/p"})
        body = resp.json()
        assert body["pattern_used"] == "d"
        assert body["is_structured"] is True

    @pytest.mark.asyncio
    async def test_e1_with_real_data_is_structured_true(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "Real Widget", "price": 9.99}
        fake_result.final_url = "https://protected.com/p"
        fake_result.rendered_markdown = "# Widget"
        fake_result.screenshots = None
        fake_result.tokens_used = 100
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={"url": "https://protected.com/p", "schema_json": {"name": "str"}},
            )
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["is_structured"] is True

    @pytest.mark.asyncio
    async def test_blocked_response_is_unstructured(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import AgentBlockedError, BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)
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
        # Fully-blocked path returns 422 via AgentBlockedError handler — body
        # there doesn't carry is_structured, but the contract is documented as
        # is_structured=false on any soft-failure path that DOES return 200.
        # This pins the 422 case as the expected terminus.
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_e1_with_raw_marker_is_unstructured(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # E1 returned data but it's the LLM-narration sentinel: {"_raw": "..."}.
        # Must surface as is_structured=false even though blocked=false.
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"_raw": "I could not extract structured data."}
        fake_result.final_url = "https://protected.com/p"
        fake_result.rendered_markdown = "# Page"
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 0.5
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={"url": "https://protected.com/p", "schema_json": {"name": "str"}},
            )
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["blocked"] is False
        assert body["is_structured"] is False, (
            "data with {_raw: ...} marker is LLM narration, not a structured payload"
        )


# --- v1.2.0: mode=hostile ---------------------------------------------------


class TestModeHostile:
    """v1.2.0 — mode=hostile invokes Pattern D directly, skipping A/B/C.

    For vendors recon-classified as hostile (Cloudflare Turnstile, Akamai EVA,
    DataDome) where A/B/C is known to fail. Saves ~2-3s per call (4 doomed
    profile attempts skipped) and cleans up pattern_attempts telemetry.
    """

    @pytest.mark.asyncio
    async def test_mode_hostile_invokes_d_directly_skips_a_b_c(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No ladder mock at all — if A/B/C is invoked the test will time out
        # making a real network call. The lack of a mock IS the assertion.
        _install_fake_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={"url": "https://hostile.com/p", "mode": "hostile"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "d"
        assert body["pattern_attempts"] == ["d"], "no A/B/C noise"
        assert body["is_structured"] is True
        assert body["hostile_skipped"] is False

    @pytest.mark.asyncio
    async def test_mode_hostile_falls_back_to_e1_by_default(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D fails (classifier rejects an unhydrated SPA shell). With
        # hostile_fallback=True (default), cascade reaches E1.
        _install_fake_hostile_client(
            monkeypatch,
            response=_FakeScraplingResponse(html="<html><body>shell</body></html>"),
        )

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "via_e1"}
        fake_result.final_url = "https://hostile.com/p"
        fake_result.rendered_markdown = "# E1"
        fake_result.screenshots = None
        fake_result.tokens_used = 100
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://hostile.com/p",
                    "mode": "hostile",
                    "schema_json": {"name": "str"},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["pattern_attempts"] == ["d", "e1"], "no a_b_c entry"
        assert body["is_structured"] is True

    @pytest.mark.asyncio
    async def test_mode_hostile_no_fallback_raises_on_d_failure(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D itself raises; hostile_fallback=False -> AgentBlockedError -> 422.
        _install_fake_hostile_client(
            monkeypatch,
            response=RuntimeError("scrapling: turnstile unsolvable"),
        )

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://hostile.com/p",
                    "mode": "hostile",
                    "hostile_fallback": False,
                },
            )
        assert resp.status_code == 422
        assert resp.json()["error"] == "blocked"

    @pytest.mark.asyncio
    async def test_mode_hostile_no_fallback_no_extra_returns_503(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [hostile] missing + hostile_fallback=False -> ConfigurationError -> 503.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://hostile.com/p",
                    "mode": "hostile",
                    "hostile_fallback": False,
                },
            )
        assert resp.status_code == 503
        body = resp.json()
        assert "hostile" in body["detail"].lower()

    @pytest.mark.asyncio
    async def test_mode_hostile_fallback_when_extra_missing(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [hostile] missing but hostile_fallback=True (default) -> falls back to E1.
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "fallback"}
        fake_result.final_url = "https://hostile.com/p"
        fake_result.rendered_markdown = None
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 0.5
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={"url": "https://hostile.com/p", "mode": "hostile"},
            )
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["hostile_skipped"] is True
        # No "d" entry because the D step was skipped (extra missing).
        assert body["pattern_attempts"] == ["e1"]


# --- v1.2.0: pattern_d_network_idle knob -----------------------------------


class _CapturingFetcher(_FakeFetcher):
    """Fake fetcher that records kwargs of the most recent async_fetch call."""

    captured_kwargs: dict[str, Any] = {}

    async def async_fetch(self, url: str, **kwargs: Any) -> Any:
        type(self).captured_kwargs.clear()
        type(self).captured_kwargs.update(kwargs)
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _install_capturing_hostile_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: Any,
) -> type[_CapturingFetcher]:
    """Install a kwarg-capturing fake hostile_client; return the captor class."""
    import scrapper_tool.patterns.d as d_mod

    _CapturingFetcher.captured_kwargs = {}

    def fake_hostile_client(**_kwargs: Any) -> _CapturingFetcher:
        return _CapturingFetcher(response)

    monkeypatch.setattr(d_mod, "hostile_client", fake_hostile_client)
    monkeypatch.setattr(http_server, "_hostile_available", lambda: True)
    return _CapturingFetcher


class TestPatternDNetworkIdle:
    """v1.2.0 — pattern_d_network_idle forwards through to Scrapling.

    SPA-rendered hostile vendors (Tasca, RevolutionParts dealers behind CF)
    need network_idle=True to capture hydrated HTML rather than the SPA shell.
    """

    @pytest.mark.asyncio
    async def test_d_default_does_not_set_network_idle(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # v1.4.0 contract: network_idle defaults to False; the cascade
        # auto-detects SPA shells and retries with network_idle=True only
        # when needed. Fixture HTML doesn't trigger SPA detection so the
        # captured kwargs should keep network_idle=False.
        # Pre-1.4.0 (v1.2.0/v1.3.0) also pinned solve_cloudflare=True
        # always — v1.4.0 changed that default to "auto" detection
        # (first pass without solver, retry with solver if CF body
        # detected). The assertion now pins the auto-default behavior.
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        captor = _install_capturing_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            await client.post("/scrape", json={"url": "https://hostile.com/p"})
        # v1.4.0 default: first-pass solver=False (auto-CF detection
        # decides whether a second pass with the solver is needed).
        # Fixture HTML doesn't look like a CF challenge, so no retry,
        # so the captured (last) call shows solve_cloudflare=False.
        assert captor.captured_kwargs.get("solve_cloudflare") is False
        assert captor.captured_kwargs.get("network_idle") is False, (
            "Default must keep network_idle=False to preserve cold-call latency"
        )

    @pytest.mark.asyncio
    async def test_d_forwards_network_idle_when_set(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        captor = _install_capturing_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            await client.post(
                "/scrape",
                json={
                    "url": "https://tasca.com/search?q=ABC123",
                    "pattern_d_network_idle": True,
                },
            )
        assert captor.captured_kwargs.get("network_idle") is True

    @pytest.mark.asyncio
    async def test_mode_hostile_also_forwards_network_idle(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # mode=hostile path calls _do_d_step too — must honor network_idle.
        captor = _install_capturing_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            await client.post(
                "/scrape",
                json={
                    "url": "https://tasca.com/search?q=XYZ",
                    "mode": "hostile",
                    "pattern_d_network_idle": True,
                },
            )
        assert captor.captured_kwargs.get("network_idle") is True


# --- v1.3.0: shared CF clearance via per-cascade user_data_dir -------------


class TestSharedProfileDir:
    """v1.3.0 - cascade allocates per-request user_data_dir for shared CF clearance.

    Default: ephemeral mkdtemp + rmtree on every exit path.
    Opt-in: persist_browser_profile_dir lets the caller own the lifecycle.
    """

    @pytest.mark.asyncio
    async def test_ephemeral_dir_created_and_cleaned_on_success(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        created: list[str] = []
        cleaned: list[str] = []

        def fake_mkdtemp(prefix: str = "") -> str:
            d = tmp_path / (prefix + "fake-" + str(len(created)))
            d.mkdir()
            path = str(d)
            created.append(path)
            return path

        def fake_rmtree(path: Any, ignore_errors: bool = False) -> None:
            cleaned.append(str(path))

        monkeypatch.setattr(http_server.tempfile, "mkdtemp", fake_mkdtemp)
        monkeypatch.setattr(http_server.shutil, "rmtree", fake_rmtree)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: True)

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            await client.post("/scrape", json={"url": "https://example.com/p"})

        assert len(created) == 1, "exactly one ephemeral dir created"
        assert cleaned == created, "every created dir was cleaned"

    @pytest.mark.asyncio
    async def test_ephemeral_dir_cleaned_on_exception_path(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from scrapper_tool.errors import AgentBlockedError, BlockedError

        created: list[str] = []
        cleaned: list[str] = []

        def fake_mkdtemp(prefix: str = "") -> str:
            d = tmp_path / (prefix + "fake")
            d.mkdir()
            path = str(d)
            created.append(path)
            return path

        def fake_rmtree(path: Any, ignore_errors: bool = False) -> None:
            cleaned.append(str(path))

        monkeypatch.setattr(http_server.tempfile, "mkdtemp", fake_mkdtemp)
        monkeypatch.setattr(http_server.shutil, "rmtree", fake_rmtree)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: True)

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(monkeypatch, response=RuntimeError("d failed"))
        agent_module = MagicMock()
        agent_module.AgentConfig = MagicMock()
        agent_module.AgentConfig.from_env = MagicMock(
            return_value=MagicMock(merged=lambda **_: MagicMock())
        )
        agent_module.agent_extract = AsyncMock(side_effect=AgentBlockedError("e1"))
        agent_module.agent_browse = AsyncMock(side_effect=AgentBlockedError("e2"))
        import sys

        monkeypatch.setitem(sys.modules, "scrapper_tool.agent", agent_module)

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://hostile.com/p"})
        assert resp.status_code == 422
        assert cleaned == created, "cleanup ran even though /scrape errored"

    @pytest.mark.asyncio
    async def test_d_step_receives_user_data_dir(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        captor = _install_capturing_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            await client.post("/scrape", json={"url": "https://hostile.com/p"})

        assert "user_data_dir" in captor.captured_kwargs, (
            "D step must forward the cascade-resolved user_data_dir"
        )
        assert "scrapper-cascade-" in captor.captured_kwargs["user_data_dir"]

    @pytest.mark.asyncio
    async def test_e1_inherits_user_data_dir_from_cascade(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from scrapper_tool.errors import BlockedError

        captured_overrides: dict[str, Any] = {}
        original_build = http_server._build_overrides

        def spy_build(req: Any) -> dict[str, Any]:
            result = original_build(req)
            captured_overrides.clear()
            captured_overrides.update(result)
            return result

        monkeypatch.setattr(http_server, "_build_overrides", spy_build)

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(monkeypatch, response=RuntimeError("d also fails"))

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "via_e1"}
        fake_result.final_url = "https://hostile.com/p"
        fake_result.rendered_markdown = None
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            await client.post(
                "/scrape",
                json={"url": "https://hostile.com/p", "schema_json": {"name": "str"}},
            )

        assert "user_data_dir" in captured_overrides, (
            "E1 escalation must inherit the cascade user_data_dir for shared CF clearance"
        )
        assert "scrapper-cascade-" in captured_overrides["user_data_dir"]

    @pytest.mark.asyncio
    async def test_caller_provided_dir_not_cleaned_up(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        cleaned: list[str] = []
        monkeypatch.setattr(
            http_server.shutil,
            "rmtree",
            lambda path, ignore_errors=False: cleaned.append(str(path)),
        )
        called_mkdtemp: list[str] = []
        monkeypatch.setattr(
            http_server.tempfile,
            "mkdtemp",
            lambda prefix="": called_mkdtemp.append(prefix) or "/UNUSED",
        )
        monkeypatch.setattr(http_server, "_hostile_available", lambda: True)

        caller_dir = str(tmp_path / "vendor-amayama-profile")

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            await client.post(
                "/scrape",
                json={
                    "url": "https://example.com/p",
                    "persist_browser_profile_dir": caller_dir,
                },
            )

        assert called_mkdtemp == [], "no ephemeral dir when caller supplied one"
        assert caller_dir not in cleaned, (
            "caller-provided dir must NOT be cleaned up by the sidecar"
        )

    @pytest.mark.asyncio
    async def test_no_dir_when_hostile_extra_missing(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)

        called_mkdtemp: list[str] = []
        monkeypatch.setattr(
            http_server.tempfile,
            "mkdtemp",
            lambda prefix="": called_mkdtemp.append(prefix) or "/UNUSED",
        )

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)

        async with _client(app_no_auth) as client:
            await client.post("/scrape", json={"url": "https://example.com/p"})

        assert called_mkdtemp == [], "ephemeral dir must NOT be allocated when D cannot run anyway"

    @pytest.mark.asyncio
    async def test_resolved_profile_dir_does_not_leak_across_requests(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from scrapper_tool.errors import BlockedError

        seen_dirs: list[str | None] = []

        original_d = http_server._do_d_step

        async def spy_d(req: Any, attempts: list[str], start: float):
            seen_dirs.append(req.__dict__.get("_resolved_profile_dir"))
            return await original_d(req, attempts, start)

        monkeypatch.setattr(http_server, "_do_d_step", spy_d)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: True)

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            await client.post("/scrape", json={"url": "https://hostile1.com/p"})
            await client.post("/scrape", json={"url": "https://hostile2.com/p"})

        assert len(seen_dirs) == 2
        assert all(d is not None for d in seen_dirs)
        assert seen_dirs[0] != seen_dirs[1], "each /scrape call must allocate its own ephemeral dir"

    @pytest.mark.asyncio
    async def test_ready_user_data_dir_supported_present(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/ready")
        body = resp.json()
        assert "user_data_dir_supported" in body["checks"], (
            "v1.3.0: /ready must report whether installed agent libs accept user_data_dir"
        )


# --- v1.4.0 — extractor registry + CSS dispatch ---------------------------


class TestExtractorRegistry:
    def test_all_names_includes_builtins(self) -> None:
        from scrapper_tool._extractors import all_names

        names = all_names()
        for required in ("css", "json_ld_product", "microdata_price", "open_graph"):
            assert required in names, f"missing built-in extractor: {required}"

    def test_css_extractor_returns_rows(self) -> None:
        from scrapper_tool._extractors import get

        html = (
            "<html><body>"
            '<div class="row"><h3>A</h3><span class="price">9.99</span></div>'
            '<div class="row"><h3>B</h3><span class="price">19.99</span></div>'
            "</body></html>"
        )
        schema = {
            "baseSelector": "div.row",
            "fields": [
                {"name": "title", "selector": "h3", "type": "text"},
                {"name": "price", "selector": "span.price", "type": "text"},
            ],
        }
        result = get("css").extract(html, options={"schema": schema})
        assert result.has_signal is True
        assert result.data == [
            {"title": "A", "price": "9.99"},
            {"title": "B", "price": "19.99"},
        ]

    def test_css_extractor_returns_empty_when_no_matches(self) -> None:
        from scrapper_tool._extractors import get

        schema = {
            "baseSelector": "div.does-not-exist",
            "fields": [{"name": "title", "selector": "h3", "type": "text"}],
        }
        result = get("css").extract("<html></html>", options={"schema": schema})
        assert result.has_signal is False
        assert result.data is None

    def test_css_extractor_drops_rows_with_required_field_missing(self) -> None:
        from scrapper_tool._extractors import get

        # First row has no price; should be dropped (price required).
        html = (
            "<html><body>"
            '<div class="row"><h3>A</h3></div>'
            '<div class="row"><h3>B</h3><span class="price">19.99</span></div>'
            "</body></html>"
        )
        schema = {
            "baseSelector": "div.row",
            "fields": [
                {"name": "title", "selector": "h3", "type": "text"},
                {"name": "price", "selector": "span.price", "type": "text"},
            ],
        }
        result = get("css").extract(html, options={"schema": schema})
        assert result.has_signal is True
        assert result.data == [{"title": "B", "price": "19.99"}]

    def test_css_extractor_attribute_field(self) -> None:
        from scrapper_tool._extractors import get

        html = '<html><body><a class="link" href="/widgets/1">Widget</a></body></html>'
        schema = {
            "baseSelector": "a.link",
            "fields": [
                {"name": "url", "selector": "a", "type": "attribute", "attribute": "href"},
                {"name": "title", "selector": "a", "type": "text"},
            ],
        }
        # Note: outer 'a.link' matches the row; inner 'a' selector matches the
        # row itself when the row IS an <a>. This is the common pattern for
        # link-based row selectors.
        result = get("css").extract(html, options={"schema": schema})
        assert result.has_signal is True
        assert result.data == [{"url": "/widgets/1", "title": "Widget"}]

    def test_open_graph_extractor_picks_up_product_tags(self) -> None:
        from scrapper_tool._extractors import get

        html = (
            "<html><head>"
            '<meta property="og:title" content="Test Widget">'
            '<meta property="og:product:price:amount" content="49.99">'
            '<meta property="og:product:price:currency" content="USD">'
            "</head></html>"
        )
        result = get("open_graph").extract(html)
        assert result.has_signal is True
        assert result.data is not None
        assert result.data["title"] == "Test Widget"
        assert result.data["price"] == "49.99"
        assert result.data["currency"] == "USD"


class TestCssExtractInD:
    """v1.4.0 — Pattern D applies a CSS schema to its HTML when supplied.

    This is the change that flips Tasca / Megazip / RevolutionParts dealers
    from "D defeats CF but extraction returns empty" to operational.
    """

    @pytest.mark.asyncio
    async def test_css_schema_extracts_rows_from_d_html(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        product_html = (
            "<html><body>"
            '<div class="result-card"><h3>Widget A</h3>'
            '<span class="price">19.99</span>'
            '<a href="/p/widget-a">link</a></div>'
            '<div class="result-card"><h3>Widget B</h3>'
            '<span class="price">29.99</span>'
            '<a href="/p/widget-b">link</a></div>'
            "</body></html>"
        )
        _install_fake_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=product_html)
        )

        css_schema = {
            "baseSelector": "div.result-card",
            "fields": [
                {"name": "title", "selector": "h3", "type": "text"},
                {"name": "price", "selector": "span.price", "type": "text"},
                {"name": "url", "selector": "a", "type": "attribute", "attribute": "href"},
            ],
        }
        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://hostile.com/search?q=widget",
                    "schema_json": css_schema,
                },
            )
        body = resp.json()
        assert body["pattern_used"] == "d"
        assert body["is_structured"] is True
        # CSS extractor's output is the canonical ``data``.
        assert body["data"] == [
            {"title": "Widget A", "price": "19.99", "url": "/p/widget-a"},
            {"title": "Widget B", "price": "29.99", "url": "/p/widget-b"},
        ]

    @pytest.mark.asyncio
    async def test_pydantic_schema_does_not_trigger_css_path(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pydantic JSON schema -> D doesn't run CSS extractor; falls back to
        # B/C (which finds nothing here) and the cascade escalates.
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(
            monkeypatch,
            response=_FakeScraplingResponse(html="<html><body>plain</body></html>"),
        )

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "via_e1"}
        fake_result.final_url = "https://hostile.com/p"
        fake_result.rendered_markdown = None
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={
                    "url": "https://hostile.com/p",
                    "schema_json": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
            )
        body = resp.json()
        # Pydantic schema -> D's CSS path skipped; cascade escalates to E1.
        assert body["pattern_used"] == "e1"


class TestIntermediateRawText:
    """v1.4.0 — D's HTML is always exposed via intermediate_raw_text."""

    @pytest.mark.asyncio
    async def test_intermediate_raw_text_present_when_d_wins(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        _install_fake_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://hostile.com/p"})
        body = resp.json()
        assert body["pattern_used"] == "d"
        assert body["intermediate_raw_text"] == _PRODUCT_HTML
        assert body["raw_text"] == _PRODUCT_HTML

    @pytest.mark.asyncio
    async def test_intermediate_raw_text_present_when_d_rejected_then_e1_wins(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D fetches HTML but classifier rejects (no signal); E1 takes over.
        # intermediate_raw_text must still carry D's HTML so adapters can
        # fall back to their own in-process parser.
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        d_html = "<html><body>SPA shell — no LD+JSON, no microdata</body></html>"
        _install_fake_hostile_client(monkeypatch, response=_FakeScraplingResponse(html=d_html))

        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "via_e1"}
        fake_result.final_url = "https://hostile.com/p"
        fake_result.rendered_markdown = "# Page"
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={"url": "https://hostile.com/p", "schema_json": {"name": "str"}},
            )
        body = resp.json()
        assert body["pattern_used"] == "e1"
        assert body["intermediate_raw_text"] == d_html
        # raw_text is None for E-tier wins; intermediate_raw_text is D's HTML.
        assert body["raw_text"] is None


class TestEscalationLog:
    """v1.4.0 — structured per-step reasons replace opaque pattern_attempts."""

    @pytest.mark.asyncio
    async def test_a_b_c_won_log_entry(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        async with _client(app_no_auth) as client:
            resp = await client.post("/scrape", json={"url": "https://example.com/p"})
        body = resp.json()
        assert body["pattern_used"] == "a_b_c"
        assert len(body["escalation_log"]) == 1
        entry = body["escalation_log"][0]
        assert entry["step"] == "a_b_c"
        assert entry["outcome"] == "won"
        assert entry["reason"] == "ok"
        assert "duration_s" in entry

    @pytest.mark.asyncio
    async def test_d_skipped_log_entry_when_extra_missing(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        monkeypatch.setattr(http_server, "_hostile_available", lambda: False)
        fake_result = MagicMock()
        fake_result.mode = "extract"
        fake_result.data = {"name": "via_e1"}
        fake_result.final_url = "https://hostile.com/p"
        fake_result.rendered_markdown = None
        fake_result.screenshots = None
        fake_result.tokens_used = 50
        fake_result.steps_used = 1
        fake_result.blocked = False
        fake_result.error = None
        fake_result.duration_s = 1.0
        _mock_agent_module(monkeypatch, extract_result=fake_result)

        async with _client(app_no_auth) as client:
            resp = await client.post(
                "/scrape",
                json={"url": "https://hostile.com/p", "schema_json": {"name": "str"}},
            )
        body = resp.json()
        steps = [e["step"] for e in body["escalation_log"]]
        assert "a_b_c" in steps
        assert "d" in steps
        assert "e1" in steps
        d_entry = next(e for e in body["escalation_log"] if e["step"] == "d")
        assert d_entry["outcome"] == "skipped"
        assert d_entry["reason"] == "extra_missing"


class TestAutoCFAndAutoSPA:
    """v1.4.0 — D auto-detects CF challenges and SPA shells."""

    @pytest.mark.asyncio
    async def test_auto_cf_skips_solver_when_no_challenge(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Default solve_cloudflare="auto"; fixture HTML doesn't look like
        # a CF challenge; so the captured (only) call has solve_cloudflare=False.
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        captor = _install_capturing_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            await client.post("/scrape", json={"url": "https://hostile.com/p"})
        # Auto-default = first probe with solve=False; no CF body so no retry.
        assert captor.captured_kwargs.get("solve_cloudflare") is False

    @pytest.mark.asyncio
    async def test_explicit_solve_cloudflare_true_passes_through(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from scrapper_tool.errors import BlockedError

        async def fake_ladder(method: str, url: str, **kwargs: Any) -> Any:
            raise BlockedError("blocked")

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        captor = _install_capturing_hostile_client(
            monkeypatch, response=_FakeScraplingResponse(html=_PRODUCT_HTML)
        )

        async with _client(app_no_auth) as client:
            await client.post(
                "/scrape",
                json={"url": "https://hostile.com/p", "solve_cloudflare": True},
            )
        assert captor.captured_kwargs.get("solve_cloudflare") is True


# --- v1.4.0 — /metrics Prometheus endpoint --------------------------------


class TestMetricsEndpoint:
    @pytest.mark.asyncio
    async def test_metrics_returns_prometheus_text_format(self, app_no_auth: Any) -> None:
        async with _client(app_no_auth) as client:
            resp = await client.get("/metrics")
        # Either 200 (prometheus-client installed) or 503 (not installed).
        assert resp.status_code in (200, 503)
        if resp.status_code == 200:
            body = resp.text
            # Standard Prometheus exposition starts with metric metadata
            # comments. Specific to our registry: we always have at least
            # the four counter / two histogram families defined.
            assert "scrapper_pattern_used" in body
            assert "scrapper_responses_structured" in body

    @pytest.mark.asyncio
    async def test_metrics_records_pattern_used_after_scrape(
        self, app_no_auth: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Run a /scrape that wins on A/B/C, then /metrics should show
        # scrapper_pattern_used_total{pattern="a_b_c"} >= 1.
        async def fake_ladder(method: str, url: str, **kwargs: Any) -> tuple[Any, str]:
            return _make_response(text=_PRODUCT_HTML, url=url), "chrome133a"

        monkeypatch.setattr("scrapper_tool.ladder.request_with_ladder", fake_ladder)
        async with _client(app_no_auth) as client:
            await client.post("/scrape", json={"url": "https://example.com/p"})
            metrics_resp = await client.get("/metrics")
        if metrics_resp.status_code != 200:
            pytest.skip("prometheus-client not installed in this build")
        body = metrics_resp.text
        # Counter line shape: scrapper_pattern_used_total{pattern="a_b_c"} <N>
        assert 'scrapper_pattern_used_total{pattern="a_b_c"}' in body

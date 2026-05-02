"""Pattern REST HTTP sidecar — FastAPI server for scrapper-tool.

Exposes the full A-E capability stack over plain JSON/HTTP so any service
can call scrapper-tool without an MCP library or Python SDK. Designed to
run as a Docker sidecar on port 5792 alongside the consumer container.

Endpoints
---------
GET  /health       Liveness probe (always 200)
GET  /ready        Readiness — probes Ollama, model availability, browser binary
GET  /version      Version + capabilities (which extras are installed)
POST /scrape       **Primary endpoint** — auto-escalating ladder A/B/C → E1 → E2
POST /fetch        Pattern A/B/C with optional Pattern B/C structured extraction
POST /extract      Pattern E1 (Crawl4AI + LLM, single call)
POST /browse       Pattern E2 (browser-use multi-step agent loop)
GET  /docs         Swagger UI (served unless SCRAPPER_TOOL_HTTP_DOCS=0)
GET  /redoc        ReDoc UI
GET  /openapi.json Raw OpenAPI 3.1 spec

Configuration (env vars)
------------------------
SCRAPPER_TOOL_HTTP_HOST           default: 0.0.0.0
SCRAPPER_TOOL_HTTP_PORT           default: 5792
SCRAPPER_TOOL_HTTP_API_KEY        optional — when set, X-API-Key is required on /fetch etc.
SCRAPPER_TOOL_HTTP_CORS_ORIGINS   default: * (comma-separated list)
SCRAPPER_TOOL_HTTP_LOG_LEVEL      default: info
SCRAPPER_TOOL_HTTP_DOCS           default: 1 (0 disables /docs and /redoc)

All ``SCRAPPER_TOOL_AGENT_*`` and ``SCRAPPER_TOOL_CAPTCHA_*`` env vars are
forwarded automatically to ``AgentConfig.from_env()`` — no duplication.
"""

from __future__ import annotations

import argparse
import os
import time
import warnings
from typing import TYPE_CHECKING, Any, Literal

# Pydantic v2 emits a UserWarning when a field name shadows a BaseModel
# attribute (``schema_json`` is one — it's the deprecated JSON-Schema
# classmethod). We use that exact field name as our request-body schema
# parameter for clarity to API callers, so the shadowing is intentional.
warnings.filterwarnings(
    "ignore",
    message=r'Field name "schema_json" in ".*" shadows an attribute in parent "BaseModel"',
    category=UserWarning,
)

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402 — after warnings filter

from scrapper_tool import __version__  # noqa: E402
from scrapper_tool._logging import get_logger  # noqa: E402
from scrapper_tool.errors import (  # noqa: E402
    AgentBlockedError,
    AgentError,
    AgentLLMError,
    AgentTimeoutError,
    BlockedError,
    ConfigurationError,
    ScrapingError,
    VendorHTTPError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


# ---------------------------------------------------------------------------
# Request / response models (module-scope so OpenAPI codegen sees stable names)
# ---------------------------------------------------------------------------


class FetchRequest(BaseModel):
    """Body of POST /fetch."""

    url: str = Field(..., description="Target URL", examples=["https://example.com"])
    method: str = Field("GET", description="HTTP method")
    headers: dict[str, str] | None = Field(None, description="Extra request headers")
    timeout: float = Field(10.0, description="Per-request timeout (seconds)")
    proxy: str | None = Field(None, description="Optional proxy URL (http://user:pass@host:port)")
    extract_structured: bool = Field(
        True,
        description=(
            "Run Pattern B (JSON-LD/microdata) + Pattern C (microdata price) on the "
            "response HTML. Adds `product`, `json_ld`, `microdata_price` fields."
        ),
    )


class ScrapeRequest(BaseModel):
    """Body of POST /scrape (the primary endpoint)."""

    # protected_namespaces=() suppresses pydantic's warning about field
    # names that collide with BaseModel methods (schema_json is one).
    model_config = ConfigDict(protected_namespaces=())

    url: str = Field(..., description="Target URL", examples=["https://example.com/product/123"])
    schema_json: dict[str, Any] | list[Any] | str | None = Field(  # type: ignore[assignment]
        None,
        description=(
            "What shape to extract. JSON Schema dict, list-of-fields, or "
            "natural-language string. If None, returns auto-detected ProductOffer "
            "from JSON-LD/microdata when A/B/C succeeds."
        ),
    )
    instruction: str | None = Field(None, description="Optional extraction guidance for the LLM")
    mode: Literal["auto", "fetch", "extract", "browse"] = Field(
        "auto",
        description=(
            "auto: full ladder (A/B/C → E1 → E2). fetch/extract/browse: force a specific pattern."
        ),
    )
    browser: str | None = Field(None, description="Override SCRAPPER_TOOL_AGENT_BROWSER")
    model: str | None = Field(None, description="Override SCRAPPER_TOOL_AGENT_MODEL")
    timeout_s: float | None = Field(None, description="Override AgentConfig.timeout_s")
    max_steps: int | None = Field(None, description="Override AgentConfig.max_steps (E2 only)")
    headful: bool = Field(False, description="Run browser headful (debugging)")


class ExtractRequest(BaseModel):
    """Body of POST /extract."""

    model_config = ConfigDict(protected_namespaces=())

    url: str
    schema_json: dict[str, Any] | list[Any] | str  # type: ignore[assignment]
    instruction: str | None = None
    browser: str | None = None
    model: str | None = None
    timeout_s: float | None = None
    headful: bool = False


class BrowseRequest(BaseModel):
    """Body of POST /browse."""

    model_config = ConfigDict(protected_namespaces=())

    url: str
    instruction: str
    schema_json: dict[str, Any] | list[Any] | None = None  # type: ignore[assignment]
    browser: str | None = None
    model: str | None = None
    max_steps: int | None = None
    timeout_s: float | None = None
    headful: bool = False


_logger = get_logger(__name__)

_HTTP_OK = 200

_NOT_INSTALLED = (
    "scrapper-tool REST server requires the [http] extra:\n"
    "    pip install 'scrapper-tool[http]'\n"
    "    uv add 'scrapper-tool[http]'"
)
_AGENT_NOT_INSTALLED = (
    "Pattern E endpoints require the [llm-agent] extra:\n    pip install 'scrapper-tool[llm-agent]'"
)


def _require_fastapi() -> None:
    """Raise :class:`ConfigurationError` with install hint if FastAPI is absent."""
    try:
        import fastapi  # noqa: F401, PLC0415
        import uvicorn  # noqa: F401, PLC0415
    except ImportError as exc:
        raise ConfigurationError(_NOT_INSTALLED) from exc


def _agent_available() -> bool:
    """Return True if the ``[llm-agent]`` extra is installed."""
    try:
        import scrapper_tool.agent  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _hostile_available() -> bool:
    """Return True if the ``[hostile]`` extra (Scrapling) is installed."""
    try:
        import scrapling  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _build_app(
    *,
    api_key: str | None = None,
    cors_origins: list[str] | None = None,
    serve_docs: bool = True,
) -> Any:
    """Build and return the FastAPI application.

    Separated from :func:`main` so tests call ``_build_app()`` directly
    against an ``httpx.ASGITransport`` without spawning uvicorn.

    Parameters
    ----------
    api_key
        When set, every /fetch /scrape /extract /browse request must
        include ``X-API-Key: <api_key>``. /health, /ready, /version,
        /docs, /redoc, /openapi.json are always unauthenticated.
    cors_origins
        Allowed CORS origins. ``["*"]`` for open access.
    serve_docs
        When False, /docs and /redoc are not registered (production).
    """
    from fastapi import Depends, FastAPI, HTTPException, Request, Security, status  # noqa: PLC0415
    from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415
    from fastapi.responses import JSONResponse, Response  # noqa: PLC0415
    from fastapi.security import APIKeyHeader  # noqa: PLC0415

    cors_origins = cors_origins or ["*"]

    app = FastAPI(
        title="scrapper-tool REST sidecar",
        version=__version__,
        description=(
            "REST sidecar for scrapper-tool. Exposes the full A-E capability stack "
            "over plain JSON/HTTP. The /scrape endpoint runs the full A/B/C → E1 → E2 "
            "auto-escalation ladder server-side so callers don't need per-pattern logic."
        ),
        docs_url="/docs" if serve_docs else None,
        redoc_url="/redoc" if serve_docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def _check_api_key(key: str | None = Security(_api_key_header)) -> None:
        if api_key is None:
            return
        if key != api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-API-Key header.",
            )

    # ---- Exception handlers ---------------------------------------------

    @app.exception_handler(ConfigurationError)
    async def _h_config(_req: Request, exc: ConfigurationError) -> Response:
        return JSONResponse(
            status_code=503,
            content={"error": "configuration_error", "detail": str(exc)},
        )

    @app.exception_handler(AgentTimeoutError)
    async def _h_timeout(_req: Request, exc: AgentTimeoutError) -> Response:
        return JSONResponse(status_code=504, content={"error": "agent_timeout", "detail": str(exc)})

    @app.exception_handler(AgentLLMError)
    async def _h_llm(_req: Request, exc: AgentLLMError) -> Response:
        return JSONResponse(
            status_code=502, content={"error": "llm_unreachable", "detail": str(exc)}
        )

    @app.exception_handler(AgentBlockedError)
    async def _h_agent_blocked(_req: Request, exc: AgentBlockedError) -> Response:
        return JSONResponse(
            status_code=422,
            content={"error": "blocked", "detail": str(exc), "blocked": True},
        )

    @app.exception_handler(BlockedError)
    async def _h_blocked(_req: Request, exc: BlockedError) -> Response:
        return JSONResponse(
            status_code=422,
            content={"error": "blocked", "detail": str(exc), "blocked": True},
        )

    @app.exception_handler(VendorHTTPError)
    async def _h_vendor(_req: Request, exc: VendorHTTPError) -> Response:
        return JSONResponse(
            status_code=502, content={"error": "vendor_http_error", "detail": str(exc)}
        )

    @app.exception_handler(AgentError)
    async def _h_agent(_req: Request, exc: AgentError) -> Response:
        return JSONResponse(status_code=500, content={"error": "agent_error", "detail": str(exc)})

    @app.exception_handler(ScrapingError)
    async def _h_scraping(_req: Request, exc: ScrapingError) -> Response:
        return JSONResponse(
            status_code=500, content={"error": "scraping_error", "detail": str(exc)}
        )

    # ---- Endpoints ------------------------------------------------------

    @app.get(
        "/health",
        operation_id="health",
        tags=["operational"],
        summary="Liveness probe",
        description="Always returns 200 if the process is up. Use for orchestrator liveness.",
    )
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/version",
        operation_id="version",
        tags=["operational"],
        summary="Version + installed-extras info",
    )
    async def version() -> dict[str, Any]:
        return {
            "version": __version__,
            "patterns": ["A", "B", "C", "D", "E"],
            "agent_available": _agent_available(),
            "hostile_available": _hostile_available(),
        }

    @app.get(
        "/ready",
        operation_id="ready",
        tags=["operational"],
        summary="Readiness with detailed component checks",
        description=(
            "Returns ready / degraded / not_ready in body (always HTTP 200). "
            "Body distinguishes 'sidecar crashed' (no response) from "
            "'sidecar up but LLM unavailable' (degraded)."
        ),
    )
    async def ready() -> dict[str, Any]:
        return await _readiness_payload()

    @app.post(
        "/fetch",
        operation_id="fetch",
        tags=["scraping"],
        summary="Pattern A/B/C — TLS-impersonation ladder fetch",
        description=(
            "Walks the impersonation ladder (chrome133a / chrome124 / safari18_0 / firefox135) "
            "until a profile returns non-403/503. With extract_structured=true (default), "
            "also runs Pattern B (extruct JSON-LD / microdata) and Pattern C (microdata price)."
        ),
    )
    async def fetch(req: FetchRequest, _: None = Depends(_check_api_key)) -> dict[str, Any]:
        return await _do_fetch(req)

    @app.post(
        "/scrape",
        operation_id="scrape",
        tags=["scraping"],
        summary="Auto-escalating scrape (PRIMARY endpoint)",
        description=(
            "Runs Pattern A/B/C → E1 → E2 in sequence and returns the first one that succeeds. "
            "Use for 95% of scraping tasks; the response includes pattern_used so callers can see "
            "which pattern produced the data."
        ),
    )
    async def scrape(req: ScrapeRequest, _: None = Depends(_check_api_key)) -> dict[str, Any]:
        return await _do_scrape(req)

    @app.post(
        "/extract",
        operation_id="extract",
        tags=["agent"],
        summary="Pattern E1 — Crawl4AI + LLM extraction (1 LLM call)",
        description=(
            "Renders the URL with a stealth browser, then makes a single LLM call to extract "
            "structured JSON matching schema_json. Faster and more reliable than /browse for "
            "non-interactive pages. Requires the [llm-agent] extra."
        ),
    )
    async def extract_endpoint(
        req: ExtractRequest, _: None = Depends(_check_api_key)
    ) -> dict[str, Any]:
        return await _do_extract(req)

    @app.post(
        "/browse",
        operation_id="browse",
        tags=["agent"],
        summary="Pattern E2 — browser-use multi-step agent loop",
        description=(
            "Multi-step LLM-driven agent loop. Use ONLY for interactive flows "
            "(login, paginate, dynamic forms). Slower and more expensive than "
            "/extract. Requires the [llm-agent] extra."
        ),
    )
    async def browse_endpoint(
        req: BrowseRequest, _: None = Depends(_check_api_key)
    ) -> dict[str, Any]:
        return await _do_browse(req)

    return app


# ---------------------------------------------------------------------------
# Endpoint implementations (free functions so they can be unit-tested directly)
# ---------------------------------------------------------------------------


def _build_overrides(req: Any) -> dict[str, Any]:
    """Build :class:`AgentConfig` override kwargs from a request body.

    Filters out ``None`` and the default ``headful=False`` so callers don't
    accidentally override env-set defaults.
    """
    candidates = {
        "browser": getattr(req, "browser", None),
        "model": getattr(req, "model", None),
        "timeout_s": getattr(req, "timeout_s", None),
        "max_steps": getattr(req, "max_steps", None),
        "headful": getattr(req, "headful", None) or None,  # False -> None
    }
    return {k: v for k, v in candidates.items() if v is not None}


def _extract_b_c(
    html: str, base_url: str | None
) -> tuple[dict[str, Any] | None, list[Any] | None, dict[str, Any] | None]:
    """Run Pattern B + Pattern C on ``html``.

    Returns ``(product, json_ld, microdata_price)`` — any field can be ``None``
    when the corresponding signal is absent on the page.
    """
    from scrapper_tool.patterns.b import extract_product_offer  # noqa: PLC0415
    from scrapper_tool.patterns.c import extract_microdata_price  # noqa: PLC0415

    product_obj = extract_product_offer(html, base_url=base_url)
    product = product_obj.model_dump(mode="json") if product_obj is not None else None

    json_ld: list[Any] | None = None
    try:
        import extruct  # noqa: PLC0415

        raw = extruct.extract(html, base_url=base_url, syntaxes=["json-ld"], uniform=True)
        json_ld = raw.get("json-ld") or None
    except Exception:
        json_ld = None

    microdata = extract_microdata_price(html)
    microdata_price = (
        {"price": str(microdata[0]), "currency": microdata[1]} if microdata is not None else None
    )

    return product, json_ld, microdata_price


async def _do_fetch(req: Any) -> dict[str, Any]:
    """POST /fetch — runs the impersonation ladder + optional B/C extraction."""
    from scrapper_tool.ladder import request_with_ladder  # noqa: PLC0415

    response, profile = await request_with_ladder(
        req.method,
        req.url,
        timeout=req.timeout,
        proxy=req.proxy,
        extra_headers=req.headers,
    )
    content_type = str(response.headers.get("content-type", "") or "")
    text = response.text or ""

    json_data: Any = None
    if "application/json" in content_type.lower():
        try:
            json_data = response.json()
        except Exception:
            json_data = None

    product = json_ld = microdata_price = None
    if req.extract_structured and text:
        product, json_ld, microdata_price = _extract_b_c(text, str(response.url))

    return {
        "status_code": int(response.status_code),
        "url": str(response.url),
        "profile": profile,
        "content_type": content_type,
        "text": text,
        "json_data": json_data,
        "headers": {str(k): str(v) for k, v in dict(response.headers).items()},
        "product": product,
        "json_ld": json_ld,
        "microdata_price": microdata_price,
        "blocked": False,
    }


async def _do_extract(req: Any) -> dict[str, Any]:
    """POST /extract — Pattern E1."""
    try:
        from scrapper_tool.agent import AgentConfig, agent_extract  # noqa: PLC0415
    except ImportError as exc:
        raise ConfigurationError(_AGENT_NOT_INSTALLED) from exc

    cfg = AgentConfig.from_env().merged(**_build_overrides(req))
    result = await agent_extract(req.url, req.schema_json, instruction=req.instruction, config=cfg)
    return result.model_dump(mode="json")


async def _do_browse(req: Any) -> dict[str, Any]:
    """POST /browse — Pattern E2."""
    try:
        from scrapper_tool.agent import AgentConfig, agent_browse  # noqa: PLC0415
    except ImportError as exc:
        raise ConfigurationError(_AGENT_NOT_INSTALLED) from exc

    cfg = AgentConfig.from_env().merged(**_build_overrides(req))
    schema = req.schema_json if isinstance(req.schema_json, dict) else None
    result = await agent_browse(req.url, req.instruction, schema=schema, config=cfg)
    return result.model_dump(mode="json")


async def _do_scrape(req: Any) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    """POST /scrape — auto-escalating ladder A/B/C → E1 → E2.

    Decision logic:
    - mode="fetch": only run A/B/C, never escalate (raw fetch + structured extraction).
    - mode="extract" / "browse": forward straight to that pattern.
    - mode="auto" (default): try A/B/C first; escalate to E1 if blocked or schema not satisfied;
      escalate to E2 if E1 is blocked.
    """
    start = time.perf_counter()
    attempts: list[str] = []
    last_error: BaseException | None = None

    # ----- A/B/C -----
    if req.mode in ("auto", "fetch"):
        attempts.append("a_b_c")
        try:
            from scrapper_tool.ladder import request_with_ladder  # noqa: PLC0415

            response, profile = await request_with_ladder(
                "GET", req.url, timeout=req.timeout_s or 30.0
            )
            text = response.text or ""
            product, json_ld, microdata_price = _extract_b_c(text, str(response.url))

            # mode="fetch" → always success.
            # mode="auto" + no schema_json → success when Pattern B or C found data.
            # mode="auto" + schema_json → escalate to E1 (LLM applies the schema).
            success = req.mode == "fetch" or (
                req.schema_json is None and (product is not None or microdata_price is not None)
            )
            if success:
                _ = profile  # currently unused in response shape; kept for logging
                return {
                    "url": str(response.url),
                    "pattern_used": "a_b_c",
                    "pattern_attempts": attempts,
                    "product": product,
                    "data": None,
                    "raw_text": text,
                    "json_ld": json_ld,
                    "microdata_price": microdata_price,
                    "rendered_markdown": None,
                    "screenshots": None,
                    "tokens_used": 0,
                    "steps_used": 0,
                    "blocked": False,
                    "error": None,
                    "duration_s": time.perf_counter() - start,
                }
        except BlockedError as exc:
            last_error = exc
        except Exception as exc:
            last_error = exc

    if req.mode == "fetch":
        # mode="fetch" forces A/B/C only; if it failed, surface the error.
        if isinstance(last_error, BaseException):
            raise last_error
        raise ScrapingError("/scrape mode=fetch failed without an exception (unreachable)")

    # ----- E1 (Pattern E extract) -----
    if req.mode in ("auto", "extract"):
        attempts.append("e1")
        try:
            from scrapper_tool.agent import AgentConfig, agent_extract  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(_AGENT_NOT_INSTALLED) from exc

        cfg = AgentConfig.from_env().merged(**_build_overrides(req))
        schema = (
            req.schema_json
            if req.schema_json is not None
            else {
                "type": "object",
                "additionalProperties": True,
            }
        )
        try:
            result = await agent_extract(req.url, schema, instruction=req.instruction, config=cfg)
            if not result.blocked:
                return _scrape_response_from_agent(result, attempts, start, mode="e1")
            last_error = AgentBlockedError(result.error or "blocked")
        except AgentBlockedError as exc:
            last_error = exc

    if req.mode == "extract":
        if isinstance(last_error, BaseException):
            raise last_error
        raise ScrapingError("/scrape mode=extract failed without an exception (unreachable)")

    # ----- E2 (Pattern E browse) -----
    attempts.append("e2")
    try:
        from scrapper_tool.agent import AgentConfig, agent_browse  # noqa: PLC0415
    except ImportError as exc:
        raise ConfigurationError(_AGENT_NOT_INSTALLED) from exc

    instruction = req.instruction or (
        f"Extract structured data matching: {req.schema_json}"
        if req.schema_json is not None
        else "Extract the main content of this page"
    )
    cfg = AgentConfig.from_env().merged(**_build_overrides(req))
    schema = req.schema_json if isinstance(req.schema_json, dict) else None
    try:
        result = await agent_browse(req.url, instruction, schema=schema, config=cfg)
        return _scrape_response_from_agent(result, attempts, start, mode="e2")
    except AgentBlockedError as exc:
        msg = f"All patterns blocked: {', '.join(attempts)}. Last error: {exc}"
        raise AgentBlockedError(msg) from exc


def _scrape_response_from_agent(
    result: Any, attempts: list[str], start: float, *, mode: Literal["e1", "e2"]
) -> dict[str, Any]:
    """Convert an :class:`AgentResult` into the /scrape response shape."""
    import base64  # noqa: PLC0415

    screenshots: list[str] | None = None
    if result.screenshots:
        screenshots = [base64.b64encode(s).decode("ascii") for s in result.screenshots[:3]]
    return {
        "url": result.final_url,
        "pattern_used": mode,
        "pattern_attempts": attempts,
        "product": None,
        "data": result.data,
        "raw_text": None,
        "json_ld": None,
        "microdata_price": None,
        "rendered_markdown": result.rendered_markdown,
        "screenshots": screenshots,
        "tokens_used": result.tokens_used,
        "steps_used": result.steps_used,
        "blocked": result.blocked,
        "error": result.error,
        "duration_s": time.perf_counter() - start,
    }


# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------


async def _readiness_payload() -> dict[str, Any]:
    """Build the /ready response body."""
    checks: dict[str, Any] = {
        "agent_installed": _agent_available(),
        "hostile_installed": _hostile_available(),
        "browser": None,
        "browser_binary": None,
        "llm_backend": None,
        "llm_url": None,
        "llm_reachable": None,
        "llm_model": None,
        "llm_model_available": None,
    }
    if not checks["agent_installed"]:
        return {"status": "not_ready", "version": __version__, "checks": checks}

    try:
        from scrapper_tool.agent.types import AgentConfig  # noqa: PLC0415

        cfg = AgentConfig.from_env()
    except Exception as exc:
        checks["error"] = str(exc)
        return {"status": "degraded", "version": __version__, "checks": checks}

    checks["browser"] = cfg.browser
    checks["llm_backend"] = cfg.llm
    checks["llm_url"] = cfg.ollama_url
    checks["llm_model"] = cfg.model

    checks["browser_binary"] = _check_browser_module(cfg.browser)
    reachable, model_available = await _probe_llm(cfg)
    checks["llm_reachable"] = reachable
    checks["llm_model_available"] = model_available

    all_pass = (
        checks["agent_installed"]
        and checks["browser_binary"] == "ok"
        and checks["llm_reachable"] is True
        and checks["llm_model_available"] is True
    )
    return {
        "status": "ready" if all_pass else "degraded",
        "version": __version__,
        "checks": checks,
    }


def _check_browser_module(browser: str) -> str:  # noqa: PLR0911 — one return per backend
    """Best-effort: 'ok' / 'missing' / 'unknown' for the configured browser's Python module."""
    if browser == "patchright":
        try:
            import patchright  # noqa: F401, PLC0415

            return "ok"
        except ImportError:
            return "missing"
    if browser == "camoufox":
        try:
            import camoufox  # noqa: F401, PLC0415

            return "ok"
        except ImportError:
            return "missing"
    if browser == "scrapling":
        try:
            import scrapling  # noqa: F401, PLC0415

            return "ok"
        except ImportError:
            return "missing"
    return "unknown"


async def _probe_llm(cfg: Any) -> tuple[bool | None, bool | None]:  # noqa: PLR0911
    """Probe the configured LLM endpoint. Returns (reachable, model_available).

    Returns (None, None) for backends we can't probe (llama_cpp / vllm).
    """
    import httpx  # noqa: PLC0415

    if cfg.llm == "ollama":
        url = f"{cfg.ollama_url.rstrip('/')}/api/tags"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                if resp.status_code != _HTTP_OK:
                    return False, False
                data = resp.json()
                models = data.get("models") or []
                names = {m.get("name", "") for m in models}
                wanted = cfg.model
                wanted_base = wanted.split(":")[0]
                available = wanted in names or any(
                    n == wanted or n.split(":")[0] == wanted_base for n in names
                )
                return True, available
        except Exception:
            return False, False

    if cfg.llm == "openai_compat":
        url = f"{cfg.ollama_url.rstrip('/')}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                if resp.status_code != _HTTP_OK:
                    return True, False
                data = resp.json()
                models = data.get("data") or []
                names = {m.get("id", "") for m in models}
                available = cfg.model in names or any(cfg.model in n for n in names)
                return True, available
        except Exception:
            return False, False

    return None, None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scrapper-tool-serve",
        description="Start the scrapper-tool REST HTTP sidecar.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SCRAPPER_TOOL_HTTP_HOST", "0.0.0.0"),  # noqa: S104 — server bind
        help="Bind host. Default: 0.0.0.0 (env: SCRAPPER_TOOL_HTTP_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SCRAPPER_TOOL_HTTP_PORT", "5792")),
        help="Bind port. Default: 5792 (env: SCRAPPER_TOOL_HTTP_PORT)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("SCRAPPER_TOOL_HTTP_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error", "critical"],
        help="Uvicorn log level. Default: info",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Hot-reload on code change (development only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``scrapper-tool-serve`` console script."""
    _require_fastapi()
    args = _build_parser().parse_args(argv)

    api_key = os.environ.get("SCRAPPER_TOOL_HTTP_API_KEY") or None
    raw_origins = os.environ.get("SCRAPPER_TOOL_HTTP_CORS_ORIGINS", "*")
    cors_origins = [o.strip() for o in raw_origins.split(",") if o.strip()] or ["*"]
    serve_docs = os.environ.get("SCRAPPER_TOOL_HTTP_DOCS", "1") not in {"0", "false", "False"}

    app = _build_app(api_key=api_key, cors_origins=cors_origins, serve_docs=serve_docs)

    import uvicorn  # noqa: PLC0415

    _logger.info(
        "http_server.starting",
        host=args.host,
        port=args.port,
        auth="enabled" if api_key else "disabled",
        cors_origins=cors_origins,
        serve_docs=serve_docs,
    )

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
    )
    return 0


__all__ = ["_build_app", "main"]

"""Pattern REST HTTP sidecar — FastAPI server for scrapper-tool.

Exposes the full A-E capability stack over plain JSON/HTTP so any service
can call scrapper-tool without an MCP library or Python SDK. Designed to
run as a Docker sidecar on port 5792 alongside the consumer container.

Endpoints
---------
GET  /health       Liveness probe (always 200)
GET  /ready        Readiness — probes Ollama, model availability, browser binary
GET  /version      Version + capabilities (which extras are installed)
POST /scrape       **Primary endpoint** — auto-escalating ladder A/B/C → D → E1 → E2
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
import shutil
import tempfile
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

from scrapper_tool import __version__, _extras  # noqa: E402
from scrapper_tool._logging import get_logger  # noqa: E402

# CookieIn is a runtime import, not a TYPE_CHECKING one: it appears in a
# Pydantic field annotation, and pydantic resolves those against module globals
# when it builds the model. Deferring it would make ScrapeRequest fail to build.
from scrapper_tool.cookies import CookieIn, cookies_for_url  # noqa: E402
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
from scrapper_tool.ladder import IMPERSONATE_LADDER  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path


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
    mode: Literal["auto", "fetch", "extract", "browse", "hostile"] = Field(
        "auto",
        description=(
            "auto: full ladder (A/B/C → D → E1 → E2). "
            "fetch/extract/browse: force a specific pattern (mode=fetch never invokes "
            "Pattern D — the cheap-path contract is preserved). "
            "hostile (NEW v1.2.0): invoke Pattern D directly, skipping the A/B/C ladder. "
            "On D failure, falls through to E1/E2 unless hostile_fallback=false. "
            "Use for vendors recon-classified as hostile (Cloudflare Turnstile, "
            "Akamai EVA, DataDome) where A/B/C is known to fail."
        ),
    )
    cookies: list[CookieIn] | None = Field(
        None,
        description=(
            "Caller-supplied cookies, threaded to every tier that can carry them. "
            "Export them with `scrapper-tool cookies export --domain <host>`. "
            "Values are write-only: they are never echoed back in a response and "
            "never appear in logs. Sending these to an unauthenticated sidecar is "
            "refused with 403 — see SCRAPPER_TOOL_HTTP_API_KEY."
        ),
    )
    browser: str | None = Field(None, description="Override SCRAPPER_TOOL_AGENT_BROWSER")
    model: str | None = Field(None, description="Override SCRAPPER_TOOL_AGENT_MODEL")
    timeout_s: float | None = Field(None, description="Override AgentConfig.timeout_s")
    max_steps: int | None = Field(None, description="Override AgentConfig.max_steps (E2 only)")
    headful: bool = Field(False, description="Run browser headful (debugging)")
    force_llm_extract: bool = Field(
        False,
        description=(
            "Pre-v1.1.2 behaviour: with ``mode=auto`` and ``schema_json`` set, "
            "always escalate to E1 even when A/B/C returned a readable page. "
            "From v1.1.2 the default is to accept A/B/C as success when the "
            "page returned 200 + any structured signal, letting the caller "
            "post-process from the raw fetch instead of paying for an LLM "
            "call. Set ``force_llm_extract=true`` to opt back in to the old "
            "always-escalate behaviour."
        ),
    )
    interactive: bool = Field(
        False,
        description=(
            "Whether this target needs a multi-step agent (NEW v1.6.0). E2 "
            "(browser-use) is the most expensive tier by a wide margin and only "
            "earns its cost on genuinely interactive flows — login, pagination, "
            "dynamic forms. With interactive=false (default) a blocked E1 stops "
            "and returns the blocked result rather than auto-escalating into an "
            "agent loop that will hit the same wall, slower. Set true when the "
            "page really does require interaction."
        ),
    )
    hostile_fallback: bool = Field(
        True,
        description=(
            "When mode=hostile and Pattern D fails (extra missing, fetch failed, "
            "or classifier rejected D's output), control whether the cascade falls "
            "through to E1/E2 (default true) or surfaces the failure immediately "
            "(false). Set false on adapters that have already paid the cost of "
            "recon and want to fail fast rather than silently pay for an LLM call."
        ),
    )
    pattern_d_network_idle: bool = Field(
        False,
        description=(
            "When True, Pattern D's Scrapling fetcher waits for the page's "
            "network to settle before returning HTML. Set this for SPA-rendered "
            "hostile vendors (Tasca, RevolutionParts dealers with CF, etc.) "
            "where the static HTML lacks structured signals because results "
            "lazy-load via client-side JS after CF clearance. Adds ~5-15s of "
            "fetch latency. Default False keeps cold-call latency low for the "
            "common Cloudflare-but-not-SPA case (Amayama, Subaru-JP)."
        ),
    )
    solve_cloudflare: bool | Literal["auto"] = Field(
        "auto",
        description=(
            "Pattern D's Cloudflare-solve behavior (NEW v1.4.0). "
            "``auto`` (default): probe first without the solver; redo with "
            "solver only when a CF challenge body is detected. Saves ~10s "
            "on vendors that don't gate behind CF. "
            "``True``: always run the solver on the first fetch (pre-1.4.0 "
            "behavior; explicit when caller knows the vendor always serves "
            "a CF challenge). "
            "``False``: never run the solver; D fetches once unprotected."
        ),
    )
    persist_browser_profile_dir: str | None = Field(
        None,
        description=(
            "Optional absolute path to a persistent browser profile dir (NEW v1.3.0). "
            "When set, Pattern D and any E1/E2 escalation step share this dir, "
            "so Cloudflare clearance cookies (cf_clearance) persist across requests. "
            "Caller is responsible for: (1) per-vendor isolation (use a different "
            "dir per host so clearance state doesn't leak between targets), "
            "(2) periodic cleanup to avoid Cloudflare's stale-profile detection "
            "(~30 min TTL is a safe default — rotate via cron or the consumer's "
            "own scheduler). When unset (default), the cascade creates an "
            "ephemeral per-request dir, shares it across D + E1 + E2, and "
            "deletes it on every exit path. Use this field only when you want "
            "cross-request reuse — the per-cascade default already eliminates "
            "the redundant CF challenges that caused E-tier escalations to "
            "fresh-fight already-bypassed sites."
        ),
    )


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


class MapRequest(BaseModel):
    """Body of POST /map — URL discovery."""

    url: str = Field(..., description="Seed URL; its site is what gets mapped")
    max_urls: int = Field(
        200,
        ge=1,
        le=10_000,
        description=(
            "Cap on returned URLs. Truncation is reported via `truncated` and "
            "`dropped_by_limit` rather than silently applied."
        ),
    )
    same_domain: bool = Field(
        True,
        description=(
            "Restrict to the seed's host and its subdomains. False follows links "
            "anywhere, which on a page with outbound links means the whole web."
        ),
    )
    include_sitemap: bool = Field(
        True, description="Read sitemaps declared in robots.txt (and /sitemap.xml)"
    )
    fetch_seed: bool = Field(
        True,
        description=(
            "Fetch the seed page through the impersonation ladder to extract its "
            "links. False makes this sitemap-only — no page request at all."
        ),
    )
    timeout_s: float | None = Field(None, description="Per-request timeout for the seed fetch")


class CrawlRequest(BaseModel):
    """Body of POST /crawl — recursive traversal running the cascade per page."""

    model_config = ConfigDict(protected_namespaces=())

    url: str = Field(..., description="Seed URL")
    schema_json: dict[str, Any] | list[Any] | str | None = Field(  # type: ignore[assignment]
        None, description="Passed to each page's cascade run, exactly as /scrape takes it"
    )
    depth: int = Field(2, ge=0, le=10, description="Link-following depth; 0 visits only the seed")
    max_pages: int = Field(
        50, ge=1, le=1_000, description="Hard cap on pages visited. Reported when hit."
    )
    concurrency: int = Field(4, ge=1, le=32, description="Pages fetched in parallel")
    same_domain: bool = Field(True, description="Stay on the seed's host and its subdomains")
    respect_robots: bool = Field(
        True,
        description=(
            "Honour robots.txt, including Crawl-delay. Only set False for sites "
            "you own or are authorised to crawl — a crawler visits pages nobody "
            "asked for, which is exactly what robots.txt governs."
        ),
    )
    interactive: bool = Field(
        False, description="Allow per-page escalation to E2 (see /scrape's interactive)"
    )
    include_html: bool = Field(
        False,
        description=(
            "Include each page's raw HTML in the response. Off by default because "
            "a 50-page crawl of rendered pages is tens of megabytes of JSON."
        ),
    )
    timeout_s: float | None = Field(None, description="Per-page timeout")


_logger = get_logger(__name__)

_HTTP_OK = 200
_HTTP_2XX_FLOOR = 200
_HTTP_3XX_FLOOR = 300


def _is_http_ok(status_code: int) -> bool:
    """True when ``status_code`` is in the 2xx success range."""
    return _HTTP_2XX_FLOOR <= status_code < _HTTP_3XX_FLOOR


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
    """Return True if the ``[llm-agent]`` extra is installed.

    Thin delegator to :func:`scrapper_tool._extras.agent_available`. A wrapper
    rather than a bare alias, deliberately: callers below resolve these names
    through this module's namespace, so a test that monkeypatches one here
    still steers every internal use of it.
    """
    return _extras.agent_available()


def _playwright_browsers_root() -> Path:
    """Return ``$PLAYWRIGHT_BROWSERS_PATH`` (or its default)."""
    return _extras.playwright_browsers_root()


def _browser_binary_present(browser: str) -> bool:
    """Probe the on-disk binary for the configured agent browser.

    Resolves the browsers root through :func:`_playwright_browsers_root` above
    rather than letting ``_extras`` look it up itself, so tests that
    monkeypatch *that* name keep pointing this probe at ``tmp_path``.
    """
    return _extras.browser_binary_present(browser, root=_playwright_browsers_root())


def _obscura_endpoint_reachable(timeout_s: float = 0.5) -> bool:
    """Best-effort TCP reachability probe for the Obscura CDP endpoint."""
    return _extras.obscura_endpoint_reachable(timeout_s)


def _agent_runnable(browser: str) -> bool:
    """True when both the Python extra AND the binary are present.

    Composed from this module's own wrappers rather than delegating to
    :func:`_extras.agent_runnable`, so patching either half changes the
    composed answer.
    """
    return _agent_available() and _browser_binary_present(browser)


def _hostile_available() -> bool:
    """Return True if the ``[hostile]`` extra (Scrapling) is installed."""
    return _extras.hostile_available()


def _user_data_dir_supported() -> bool:
    """Probe whether installed Crawl4AI / browser-use accept ``user_data_dir``."""
    return _extras.user_data_dir_supported()


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
            "over plain JSON/HTTP. The /scrape endpoint runs the full A/B/C → D → E1 → E2 "
            "auto-escalation ladder server-side so callers don't need per-pattern logic. "
            "Pattern D (Scrapling) is invoked when the [hostile] extra is installed; "
            "skipped otherwise (cascade falls through to E1)."
        ),
        docs_url="/docs" if serve_docs else None,
        redoc_url="/redoc" if serve_docs else None,
    )

    # Wildcard origins and credentialed CORS must not be combined.
    #
    # The CORS spec forbids `Access-Control-Allow-Origin: *` together with
    # `Access-Control-Allow-Credentials: true`, and the assumption used to be
    # that this pairing was merely misconfigured-but-inert because browsers
    # reject it. Checked against the pinned Starlette (1.3.1) it is not inert:
    # with allow_origins=["*"] and allow_credentials=True, Starlette *reflects*
    # the request's Origin header verbatim and still sends
    # allow-credentials: true. A page on any origin can then make credentialed
    # cross-origin requests to this sidecar and read the responses — which for
    # a sidecar that holds API keys and session cookies is a real exfiltration
    # path, not a lint finding.
    #
    # So when origins are wildcarded we drop credentials rather than keep an
    # exploitable pairing. Nothing legitimate is lost: credentialed CORS
    # requires enumerating origins under the spec anyway, so a deployment that
    # genuinely needs it must list them, and one that doesn't is unaffected.
    wildcard_origins = "*" in (cors_origins or [])
    if wildcard_origins:
        _logger.warning(
            "http.cors.credentials_disabled",
            reason="allow_origins=* cannot be combined with credentialed CORS",
            remedy="set SCRAPPER_TOOL_HTTP_CORS_ORIGINS to an explicit origin list",
        )
        if api_key is None:
            _logger.warning(
                "http.cors.open_and_unauthenticated",
                reason="wildcard CORS with no API key configured",
                remedy="set SCRAPPER_TOOL_HTTP_API_KEY",
            )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=not wildcard_origins,
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

    async def _check_cookie_auth(req: ScrapeRequest) -> None:
        """Refuse caller-supplied cookies on an unauthenticated sidecar.

        ``_check_api_key`` is a no-op when ``SCRAPPER_TOOL_HTTP_API_KEY`` is
        unset, which is the default — so ``/scrape`` is open out of the box.
        That is defensible for anonymous scraping and indefensible the moment a
        request body carries a live session cookie: anyone who can reach the
        port could replay someone's session through this host's egress IP.

        Breaking "works out of the box" is the right call here, but only for
        requests that actually carry cookies; everything else is unaffected.
        ``SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES=1`` is the localhost-dev
        escape hatch.
        """
        if not req.cookies:
            return
        if api_key is not None:
            return
        if os.environ.get("SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Refusing cookies on an unauthenticated sidecar. Set "
                "SCRAPPER_TOOL_HTTP_API_KEY and send X-API-Key, or set "
                "SCRAPPER_TOOL_HTTP_ALLOW_UNAUTH_COOKIES=1 for localhost development."
            ),
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

    @app.get(
        "/metrics",
        operation_id="metrics",
        tags=["operational"],
        summary="Prometheus exposition (v1.4.0+)",
        description=(
            "Standard Prometheus text-format exposition. Counters: "
            "``scrapper_pattern_used_total``, "
            "``scrapper_responses_structured_total``, "
            "``scrapper_responses_unstructured_total``, "
            "``scrapper_user_data_dir_reused_total``, "
            "``scrapper_challenge_detected_total`` (by vendor), "
            "``scrapper_recipe_events_total`` (hit/learned/drift), "
            "``scrapper_policy_skips_total`` (by start tier). "
            "Histograms: ``scrapper_pattern_duration_seconds`` (per step+outcome), "
            "``scrapper_cascade_steps`` (per request). "
            "Returns 503 when ``prometheus-client`` isn't installed (older "
            "[http] extras)."
        ),
    )
    async def metrics():  # type: ignore[no-untyped-def]
        # Return-type annotation intentionally omitted: ``Response`` is
        # locally imported inside _build_app, and ``from __future__ import
        # annotations`` would force FastAPI/Pydantic to resolve the
        # forward-ref against module scope and fail.
        cache = _get_prometheus_registry()
        if cache is None:
            return Response(
                content="prometheus-client not installed in this build\n",
                status_code=503,
                media_type="text/plain; charset=utf-8",
            )
        from prometheus_client import (  # noqa: PLC0415
            CONTENT_TYPE_LATEST,
            generate_latest,
        )

        registry, _ = cache
        return Response(
            content=generate_latest(registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.post(
        "/fetch",
        operation_id="fetch",
        tags=["scraping"],
        summary="Pattern A/B/C — TLS-impersonation ladder fetch",
        description=(
            "Walks the impersonation ladder ("
            + " / ".join(IMPERSONATE_LADDER)
            + ") until a profile returns non-403/503. With extract_structured=true (default), "
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
            "Runs Pattern A/B/C → D → E1 → E2 in sequence and returns the first one that "
            "succeeds. Use for 95% of scraping tasks; the response includes pattern_used so "
            "callers can see which pattern produced the data. Pattern D (Scrapling) is invoked "
            "between A/B/C and E1 when the [hostile] extra is installed; when it isn't, the "
            "cascade falls through to E1 and the response carries hostile_skipped=true."
        ),
    )
    async def scrape(
        req: ScrapeRequest,
        _: None = Depends(_check_api_key),
        __: None = Depends(_check_cookie_auth),
    ) -> dict[str, Any]:
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

    @app.post(
        "/map",
        operation_id="map",
        tags=["scraping"],
        summary="Discover URLs on a site",
        description=(
            "Combines sitemap discovery (via robots.txt Sitemap: directives, "
            "falling back to /sitemap.xml) with links extracted from the seed "
            "page fetched through the impersonation ladder. Cheap — no browser, "
            "no LLM. Reports truncation explicitly rather than silently capping."
        ),
    )
    async def map_endpoint(req: MapRequest, _: None = Depends(_check_api_key)) -> dict[str, Any]:
        return await _do_map(req)

    @app.post(
        "/crawl",
        operation_id="crawl",
        tags=["scraping"],
        summary="Crawl a site, running the full cascade per page",
        description=(
            "Breadth-first traversal from a seed URL. Each page runs the same "
            "auto cascade as /scrape, so every page benefits from recipe replay, "
            "the render tier, and proxy rotation. robots.txt is honoured by "
            "default (including Crawl-delay). Bounded by depth, max_pages, and "
            "concurrency; the response reports what the bounds left unvisited."
        ),
    )
    async def crawl_endpoint(
        req: CrawlRequest, _: None = Depends(_check_api_key)
    ) -> dict[str, Any]:
        return await _do_crawl(req)

    return app


# ---------------------------------------------------------------------------
# Endpoint implementations (free functions so they can be unit-tested directly)
# ---------------------------------------------------------------------------


def _build_overrides(req: Any) -> dict[str, Any]:
    """Build :class:`AgentConfig` override kwargs from a request body.

    Filters out ``None`` and the default ``headful=False`` so callers don't
    accidentally override env-set defaults.

    The ``user_data_dir`` override is read from a transient attribute that
    ``_do_scrape`` stashes on the request after resolving (ephemeral temp
    dir vs caller-provided ``persist_browser_profile_dir``). When not set
    by the cascade orchestrator, this is None and the agent layer uses
    whatever the env var ``SCRAPPER_TOOL_AGENT_USER_DATA_DIR`` configured.
    """
    candidates = {
        "browser": getattr(req, "browser", None),
        "model": getattr(req, "model", None),
        "timeout_s": getattr(req, "timeout_s", None),
        "max_steps": getattr(req, "max_steps", None),
        "headful": getattr(req, "headful", None) or None,  # False -> None
        # v1.3.0: read the cascade-resolved profile dir off the request.
        # _do_scrape sets this in __dict__ after deciding ephemeral vs
        # persistent. None when no cascade ran (mode=extract / mode=browse
        # direct) — agent layer falls back to env var or its own default.
        "user_data_dir": req.__dict__.get("_resolved_profile_dir"),
        # The request-scoped cookie jar, so E1/E2 can carry the caller's
        # session. Travels the same route as user_data_dir above rather than
        # as a separate kwarg: agent_extract / agent_browse funnel every
        # per-call knob through AgentConfig.merged(), and a second channel
        # would be one more thing to keep in sync.
        "cookies": _request_cookies(req),
    }
    return {k: v for k, v in candidates.items() if v is not None}


def _extract_b_c(
    html: str, base_url: str | None
) -> tuple[dict[str, Any] | None, list[Any] | None, dict[str, Any] | None]:
    """Run Pattern B + Pattern C on ``html`` (legacy 3-tuple shape).

    Returns ``(product, json_ld, microdata_price)`` — any field can be
    ``None`` when the corresponding signal is absent on the page.

    v1.4.0+: this is now a thin wrapper over the extractor registry
    (``scrapper_tool._extractors``). The pipeline runs ``json_ld_product``
    + ``microdata_price`` + ``open_graph`` and unpacks back to the legacy
    tuple shape so existing call-sites and tests don't break.
    """
    from scrapper_tool._extractors import get as get_extractor  # noqa: PLC0415

    json_ld_result = get_extractor("json_ld_product").extract(html, base_url=base_url)
    microdata_result = get_extractor("microdata_price").extract(html, base_url=base_url)

    product: dict[str, Any] | None = None
    json_ld: list[Any] | None = None
    if json_ld_result.has_signal and isinstance(json_ld_result.data, dict):
        product = json_ld_result.data.get("product")
        json_ld = json_ld_result.data.get("json_ld")

    microdata_price: dict[str, Any] | None = None
    if microdata_result.has_signal and isinstance(microdata_result.data, dict):
        microdata_price = microdata_result.data

    return product, json_ld, microdata_price


# v1.4.0 — Pattern D's auto-CF detection. When the first fetch returns a
# CF challenge body, redo with solve_cloudflare=True. Saves ~10s on
# vendors that don't gate behind CF.
_CF_CHALLENGE_STATUS_CODES: frozenset[int] = frozenset({403, 503})
_CF_CHALLENGE_BODY_MAX_BYTES = 50_000  # CF challenge pages are tiny
_CF_BODY_SCAN_BYTES = 8_192
_SPA_SHELL_MAX_BYTES = 30_000

_CF_CHALLENGE_SIGNATURES: tuple[str, ...] = (
    "<title>Just a moment...",
    "<title>Attention Required! | Cloudflare",
    "challenges.cloudflare.com/turnstile",
    'cf-mitigated"',
    "cf-chl-bypass",
)


def _is_cf_challenge_body(html: str, status_code: int) -> bool:
    """True when the response looks like a Cloudflare challenge page.

    v1.6.0: delegates to the shared detector (``scrapper_tool._challenge``) so MCP
    and the render tier use the same heuristics. Deliberately still the
    Cloudflare-only variant — this decides whether Pattern D retries with
    Scrapling's CF-specific ``solve_cloudflare``, so broadening it would make
    Scrapling attempt a CF solve against non-CF vendors.
    """
    from scrapper_tool._challenge import is_cf_challenge_body  # noqa: PLC0415

    return is_cf_challenge_body(html, status_code)


# v1.4.0 — auto-SPA detection. After D + extractors return zero signal,
# detect SPA-shell patterns and trigger a network_idle retry.
_SPA_SHELL_SIGNATURES: tuple[str, ...] = (
    'id="root"',
    'id="app"',
    'id="__next"',
    "data-reactroot",
    "ng-version=",
    "window.__nuxt__",
    "window.__initial_state__",
)


def _looks_like_spa_shell(html: str) -> bool:
    """True when the response looks like an unhydrated SPA shell.

    Heuristic: small HTML (<30KB) with one of the canonical SPA root
    markers. Not perfect — the network_idle retry will still find no
    signal if the page is genuinely empty, but auto-SPA's cost is low
    (just one extra Scrapling fetch) so the FP rate is acceptable.

    v1.6.0: delegates to the shared detector. NB this size-capped check misses
    *large* unhydrated pages (a 419 KB shell full of ``{displayTitle}``
    placeholders sails past it) — ``_challenge.looks_unhydrated`` covers that case.
    """
    from scrapper_tool._challenge import looks_like_spa_shell  # noqa: PLC0415

    return looks_like_spa_shell(html)


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


def _get_prometheus_registry() -> Any:
    """Lazy-build the Prometheus registry + collectors (v1.4.0).

    Returns ``None`` when ``prometheus-client`` isn't importable so the
    sidecar still runs against pre-v1.4.0 ``[http]`` installs (no
    forced upgrade). When importable, returns a tuple of
    ``(registry, counters_dict)`` cached on the function as
    ``_get_prometheus_registry._cache``.
    """
    cache: tuple[Any, dict[str, Any]] | None = getattr(_get_prometheus_registry, "_cache", None)
    if cache is not None:
        return cache
    try:
        from prometheus_client import (  # noqa: PLC0415
            CollectorRegistry,
            Counter,
            Histogram,
        )
    except ImportError:
        _get_prometheus_registry._cache = None  # type: ignore[attr-defined]
        return None

    registry = CollectorRegistry()
    metrics: dict[str, Any] = {
        "pattern_used": Counter(
            "scrapper_pattern_used_total",
            "Number of /scrape calls grouped by winning pattern.",
            labelnames=["pattern"],
            registry=registry,
        ),
        "responses_structured": Counter(
            "scrapper_responses_structured_total",
            "Number of /scrape calls where the sidecar's classifier emitted is_structured=true.",
            labelnames=["pattern"],
            registry=registry,
        ),
        "responses_unstructured": Counter(
            "scrapper_responses_unstructured_total",
            "Number of /scrape calls where the sidecar emitted is_structured=false.",
            labelnames=["pattern"],
            registry=registry,
        ),
        "pattern_duration_seconds": Histogram(
            "scrapper_pattern_duration_seconds",
            "Duration of an individual cascade step in seconds.",
            labelnames=["step", "outcome"],
            buckets=(0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0),
            registry=registry,
        ),
        "cascade_steps_total": Histogram(
            "scrapper_cascade_steps",
            "Number of cascade steps walked per /scrape call.",
            buckets=(1, 2, 3, 4, 5),
            registry=registry,
        ),
        "user_data_dir_reused": Counter(
            "scrapper_user_data_dir_reused_total",
            "Number of /scrape calls that reused a caller-provided persistent profile dir.",
            registry=registry,
        ),
        # v1.6.0 F3 — the cascade-intelligence tiers, so their payoff is visible
        # on a dashboard rather than inferred from latency.
        "challenge_detected": Counter(
            "scrapper_challenge_detected_total",
            "Number of /scrape calls where a bot-vendor interstitial was detected, by vendor.",
            labelnames=["vendor"],
            registry=registry,
        ),
        "recipe_events": Counter(
            "scrapper_recipe_events_total",
            "Recipe cache outcomes: hit (replay served), learned, drift (evicted).",
            labelnames=["event"],
            registry=registry,
        ),
        "policy_skips": Counter(
            "scrapper_policy_skips_total",
            "Number of /scrape calls where per-domain memory skipped cheaper tiers, "
            "by the tier it started at.",
            labelnames=["start_tier"],
            registry=registry,
        ),
    }
    cache = (registry, metrics)
    _get_prometheus_registry._cache = cache  # type: ignore[attr-defined]
    return cache


def _observe_cascade(payload: dict[str, Any] | None, *, exception: bool = False) -> None:
    """Update Prometheus counters / histograms after a cascade run.

    Called from ``_do_scrape``'s try/finally. Safe to call when
    ``prometheus-client`` isn't installed (no-op).
    """
    cache = _get_prometheus_registry()
    if cache is None:
        return
    _, metrics = cache
    if exception or payload is None:
        # Cascade raised — count under a synthetic "exception" pattern
        # for ops dashboards.
        metrics["pattern_used"].labels(pattern="exception").inc()
        return

    pattern = payload.get("pattern_used") or "none"
    metrics["pattern_used"].labels(pattern=pattern).inc()
    if payload.get("is_structured"):
        metrics["responses_structured"].labels(pattern=pattern).inc()
    else:
        metrics["responses_unstructured"].labels(pattern=pattern).inc()

    vendor = payload.get("challenge_detected")
    if vendor:
        metrics["challenge_detected"].labels(vendor=str(vendor)).inc()
    if pattern == "replay":
        metrics["recipe_events"].labels(event="hit").inc()

    log: list[dict[str, Any]] = payload.get("escalation_log") or []
    for entry in log:
        step = entry.get("step", "unknown")
        outcome = entry.get("outcome", "unknown")
        duration = entry.get("duration_s") or 0.0
        metrics["pattern_duration_seconds"].labels(step=step, outcome=outcome).observe(
            float(duration)
        )
        # The escalation log is the single source of truth for the intelligence
        # tiers, so derive their counters from it rather than re-instrumenting
        # each call site.
        if step == "policy" and outcome == "skipped":
            metrics["policy_skips"].labels(start_tier=pattern).inc()
        elif step == "recipe":
            # reason ∈ {learned, drift}; the "hit" event is counted above from
            # pattern_used == "replay" so it isn't double-counted here.
            metrics["recipe_events"].labels(event=entry.get("reason", "unknown")).inc()
    metrics["cascade_steps_total"].observe(len(log))


def _build_log_entry(
    step: str,
    *,
    outcome: str,
    reason: str,
    duration_s: float,
    detail: str | None = None,
) -> dict[str, Any]:
    """v1.4.0 — one ``escalation_log`` row.

    ``outcome`` ∈ ``{won, failed, rejected, skipped}``:

    * ``won`` — step produced the cascade's final response.
    * ``failed`` — step raised or the underlying anti-bot lost.
    * ``rejected`` — step succeeded transport-wise but the classifier
      didn't accept the result (e.g. Pattern D got HTML but no
      structured signal — the cascade escalates).
    * ``skipped`` — step couldn't run (extra missing, mode dispatch).

    ``reason`` ∈ ``{ok, blocked, no_signal, extra_missing, exception}``.
    Cheap enum so consumers (PartsPilot's per-vendor breaker accounting
    in particular) can decide whether to count this against a vendor's
    failure budget.
    """
    entry: dict[str, Any] = {
        "step": step,
        "outcome": outcome,
        "reason": reason,
        "duration_s": round(duration_s, 4),
    }
    if detail is not None:
        entry["detail"] = detail
    return entry


def _classify_extraction_success(
    req: Any,
    *,
    status_code: int,
    text: str,
    product: dict[str, Any] | None,
    microdata_price: dict[str, Any] | None,
    json_ld: list[Any] | None,
    css_data: list[Any] | dict[str, Any] | None = None,
) -> bool:
    """v1.1.2 success classifier — shared across A/B/C and Pattern D fetch steps.

    Both A/B/C (impersonation ladder) and Pattern D (Scrapling) produce
    the same shape — raw HTML + B/C structured extraction — so the
    accept-or-escalate decision is identical. force_llm_extract still
    forces escalation when the caller explicitly wants the LLM.

    v1.4.0+: ``css_data`` is the output of the CSS extractor when the
    caller's ``schema_json`` was CSS-shaped. A non-empty list (or
    non-empty dict) counts as a structured signal, regardless of
    whether B/C also matched.
    """
    # v1.5.0: logic moved verbatim to the shared classifier so REST and the
    # MCP auto_scrape cascade use one identical accept rule. REST behavior is
    # unchanged — this is a pure delegation.
    from scrapper_tool._classify import classify_extraction_success  # noqa: PLC0415

    return classify_extraction_success(
        mode=req.mode,
        schema_json=req.schema_json,
        force_llm_extract=getattr(req, "force_llm_extract", False),
        status_code=status_code,
        text=text,
        product=product,
        microdata_price=microdata_price,
        json_ld=json_ld,
        css_data=css_data,
    )


def _is_e_tier_structured(data: object | None, blocked: bool) -> bool:
    """Verdict for an E1/E2 result — True iff data is structured JSON, not LLM narration.

    Crawl4AI and browser-use return ``{"_raw": "<free-form text>"}`` when the
    LLM failed to emit valid JSON against the supplied schema. That's the
    "narration of failure" case — surface it as ``is_structured=False`` so
    downstream consumers don't treat it as a real payload.

    A/B/C and D never reach this helper — they return only when their
    classifier (``_classify_extraction_success``) already accepted the page,
    so their ``is_structured`` is always True.
    """
    if blocked or data is None:
        return False
    return not (isinstance(data, dict) and "_raw" in data)


async def _do_d_step(
    req: Any,
    attempts: list[str],
    start: float,
) -> tuple[dict[str, Any] | None, BaseException | None, bool]:
    """Pattern D (Scrapling) cascade step.

    Returns ``(response, error, hostile_skipped)``:

    * ``response`` — the /scrape response dict when D succeeded, else None.
    * ``error`` — the exception raised by D when it failed, else None.
    * ``hostile_skipped`` — True when the [hostile] extra is missing and
      D was skipped without any fetch attempt. The caller surfaces this
      to the consumer so they can choose to install the extra and avoid
      paying for an LLM call on hostile-but-Scrapling-readable vendors.

    Skips entirely (no append to ``attempts``) when [hostile] isn't
    installed — the cascade then falls through to E1.
    """
    log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])
    if not _hostile_available():
        _logger.info("scrape.d.skipped_no_extra", url=req.url)
        log.append(
            _build_log_entry(
                "d",
                outcome="skipped",
                reason="extra_missing",
                duration_s=0.0,
                detail="[hostile] extra not installed",
            )
        )
        return None, None, True

    attempts.append("d")
    d_start = time.perf_counter()
    try:
        html, status_code, final_url = await _d_fetch_with_smart_defaults(req)
    except Exception as exc:
        _logger.warning("scrape.d.failed", url=req.url, error=str(exc))
        log.append(
            _build_log_entry(
                "d",
                outcome="failed",
                reason="exception",
                duration_s=time.perf_counter() - d_start,
                detail=f"{type(exc).__name__}: {exc!s}",
            )
        )
        return None, exc, False

    # v1.4.0 — always stash D's HTML on the request so downstream
    # response payloads can surface it as ``intermediate_raw_text``.
    # Adapters that want to recover D's HTML after escalation read it
    # there, regardless of whether D's classifier accepted the page.
    req.__dict__["_d_intermediate_html"] = html

    # v1.4.0 — multi-extractor pipeline. Default order: json_ld_product,
    # microdata_price, open_graph. CSS schema added when the caller
    # supplied one (auto-detected via shape).
    css_data, product, json_ld, microdata_price = _run_d_extractors(req, html, final_url)

    success = _classify_extraction_success(
        req,
        status_code=status_code,
        text=html,
        product=product,
        microdata_price=microdata_price,
        json_ld=json_ld,
        css_data=css_data,
    )

    d_duration = time.perf_counter() - d_start
    if not success:
        _logger.info(
            "scrape.d.no_signal",
            url=req.url,
            status_code=status_code,
            has_product=product is not None,
            has_price=microdata_price is not None,
            has_css_data=bool(css_data),
        )
        log.append(
            _build_log_entry(
                "d",
                outcome="rejected",
                reason="no_signal",
                duration_s=d_duration,
                detail=f"status={status_code}; no LD+JSON / microdata / CSS rows",
            )
        )
        return None, None, False

    _logger.info("scrape.d.win", url=req.url, status_code=status_code)
    log.append(_build_log_entry("d", outcome="won", reason="ok", duration_s=d_duration))
    return (
        {
            "url": final_url,
            "pattern_used": "d",
            "pattern_attempts": attempts,
            "escalation_log": list(log),
            "product": product,
            "data": css_data,  # v1.4.0 — populated from CSS extractor when schema supplied
            "raw_text": html,
            "intermediate_raw_text": html,  # v1.4.0 — always D's HTML when D ran
            "json_ld": json_ld,
            "microdata_price": microdata_price,
            "rendered_markdown": None,
            "screenshots": None,
            "tokens_used": 0,
            "steps_used": 0,
            "blocked": False,
            "error": None,
            "hostile_skipped": False,
            "is_structured": True,
            "duration_s": time.perf_counter() - start,
        },
        None,
        False,
    )


async def _d_fetch_with_smart_defaults(req: Any) -> tuple[str, int, str]:
    """Fetch ``req.url`` via Pattern D with v1.4.0 smart defaults.

    Returns ``(html, status_code, final_url)``.

    Smart-default behaviors:

    1. **Auto-CF detection** — when ``solve_cloudflare`` is unspecified
       (or set to ``"auto"``), first fetch without the solver. If the
       response looks like a CF challenge body, redo with the solver.
       Saves ~10s on vendors that don't gate behind CF.
    2. **Auto-SPA detection** — when ``pattern_d_network_idle`` wasn't
       explicitly set AND the first response looks like an unhydrated
       SPA shell (small HTML with React/Vue/Angular root markers),
       retry once with ``network_idle=True``.

    Both behaviors can be disabled by explicitly setting the flags.
    """
    from scrapper_tool.patterns.d import CookieKwargUnsupported, hostile_client  # noqa: PLC0415

    timeout_s = req.timeout_s or 30.0
    network_idle = bool(getattr(req, "pattern_d_network_idle", False))
    effective_timeout = max(timeout_s, 30.0) if network_idle else timeout_s
    profile_dir = req.__dict__.get("_resolved_profile_dir")

    # Solve-CF resolution: explicit True/False wins. ``"auto"`` (default)
    # does two-pass detection — but only when the caller didn't already
    # tell us the target is hostile. ``mode="hostile"`` is the explicit
    # "skip the recon, solve CF straight away" signal; honoring auto in
    # that case wastes ~3s on the prelude probe and risks Scrapling's
    # internal retry loop running twice. So when mode=hostile, auto
    # resolves to True (preserves v1.3.0 cost story for hostile-pinned
    # adapters). When mode=auto, auto means probe-first (saves ~10s on
    # non-CF vendors).
    raw_solve = getattr(req, "solve_cloudflare", "auto")
    explicit_solve: bool | None
    if raw_solve in (True, False):
        explicit_solve = raw_solve
    elif getattr(req, "mode", "auto") == "hostile":
        explicit_solve = True
    else:
        explicit_solve = None  # "auto" + mode=auto -> probe-first detect

    base_kwargs: dict[str, Any] = {"network_idle": network_idle}
    if profile_dir:
        base_kwargs["user_data_dir"] = profile_dir

    # Narrow the jar to this URL once, rather than per pass — _fetch_once can run
    # up to three times (CF retry, network-idle retry) and the answer is the same
    # every time.
    d_cookies = _cookies_for_tier(req, "d")

    async def _fetch_once(*, solve: bool, ni: bool) -> tuple[str, int, str]:
        nonlocal d_cookies
        kw = dict(base_kwargs)
        kw["solve_cloudflare"] = solve
        kw["network_idle"] = ni
        try:
            async with hostile_client(timeout=effective_timeout, cookies=d_cookies) as fetcher:
                response = await fetcher.async_fetch(req.url, **kw)
        except CookieKwargUnsupported as exc:
            # This Scrapling build cannot carry a session. Report it and retry
            # once without cookies: a logged-out page is still worth more than a
            # failed tier, and cookies_skipped tells the caller why the result
            # looks anonymous. Clearing d_cookies stops later passes re-trying
            # a kwarg we now know is rejected.
            _logger.warning("scrape.d.cookies_unsupported", url=req.url, error=str(exc)[:120])
            _mark_cookies_skipped(req, "d", "scrapling_rejected_cookies_kwarg")
            d_cookies = None
            async with hostile_client(timeout=effective_timeout) as fetcher:
                response = await fetcher.async_fetch(req.url, **kw)
        else:
            if d_cookies:
                _mark_cookies_applied(req, "d")
        html = getattr(response, "html_content", None) or getattr(response, "body", None) or ""
        status = int(getattr(response, "status", 0) or getattr(response, "status_code", 0) or 0)
        url = str(getattr(response, "url", req.url) or req.url)
        return html, status, url

    # ----- Pass 1: try without the solver when "auto" -----
    first_solve = explicit_solve if explicit_solve is not None else False
    html, status_code, final_url = await _fetch_once(solve=first_solve, ni=network_idle)

    # ----- Pass 1b: auto-CF retry with the solver -----
    if explicit_solve is None and _is_cf_challenge_body(html, status_code):
        _logger.info("scrape.d.auto_cf_detected", url=req.url, status_code=status_code)
        html, status_code, final_url = await _fetch_once(solve=True, ni=network_idle)

    # ----- Pass 2: auto-SPA retry with network_idle -----
    # Only fires when network_idle wasn't explicit AND the first response
    # looks like an SPA shell.
    if not network_idle and _looks_like_spa_shell(html):
        _logger.info("scrape.d.auto_spa_detected", url=req.url, html_len=len(html))
        # Use the same solve decision the first pass made.
        final_solve = (
            explicit_solve
            if explicit_solve is not None
            else _is_cf_challenge_body(html, status_code)
        )
        html, status_code, final_url = await _fetch_once(solve=final_solve, ni=True)

    return html, status_code, final_url


async def _do_replay_step(
    req: Any,
    attempts: list[str],
    start: float,
) -> dict[str, Any] | None:
    """Tier 0 — replay this domain's learned recipe. No browser, no LLM.

    The cheapest possible outcome: a fetch plus a selectolax parse, reproducing
    what previously cost a browser launch or an LLM call. Returns None on any
    miss so the normal cascade runs — including on drift, where the stale recipe
    is evicted first so the cascade's re-derivation replaces it.
    """
    log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])
    r_start = time.perf_counter()
    try:
        from scrapper_tool.recipe.replay import try_replay  # noqa: PLC0415
        from scrapper_tool.recipe.store import cache_key, get_store  # noqa: PLC0415

        # Snapshot whether a recipe existed *before* the attempt, so a None
        # result after one did is distinguishable as drift (site changed) rather
        # than a plain cold miss — try_replay evicts on drift, so this is the
        # only moment the two can be told apart.
        had_recipe = get_store().get(cache_key(req.url, req.schema_json)) is not None
        outcome = await try_replay(
            req.url,
            fetch=_make_ladder_fetch(req),
            render=_make_render_fetch(req),
            schema_json=req.schema_json,
        )
    except Exception as exc:  # cache problems must never break a scrape
        _logger.warning("scrape.replay.failed", url=req.url, error=str(exc)[:160])
        return None
    if outcome is None:
        if had_recipe:
            # The recipe stopped matching and was evicted; the cascade re-derives.
            log.append(
                _build_log_entry("recipe", outcome="rejected", reason="drift", duration_s=0.0)
            )
        return None

    attempts.append("replay")
    rows: Any = outcome.rows if outcome.recipe.multi_row else outcome.rows[0]
    log.append(
        _build_log_entry(
            "replay",
            outcome="won",
            reason="ok",
            duration_s=time.perf_counter() - r_start,
            detail=f"recipe from {outcome.recipe.source_tier}; {len(outcome.rows)} row(s)",
        )
    )
    _logger.info("scrape.replay.win", url=req.url, rows=len(outcome.rows))
    return {
        "url": outcome.final_url,
        "pattern_used": "replay",
        "pattern_attempts": attempts,
        "escalation_log": list(log),
        "product": None,
        "data": rows,
        "raw_text": outcome.html,
        "intermediate_raw_text": None,
        "json_ld": None,
        "microdata_price": None,
        "rendered_markdown": None,
        "screenshots": None,
        "tokens_used": 0,
        "steps_used": 0,
        "blocked": False,
        "error": None,
        "hostile_skipped": False,
        "is_structured": True,
        "duration_s": time.perf_counter() - start,
    }


def _make_ladder_fetch(req: Any) -> Callable[[], Awaitable[tuple[str, int, str]]]:
    """A zero-arg HTTP fetch for replaying an ``a_b_c``-learned recipe."""

    async def fetch() -> tuple[str, int, str]:
        from scrapper_tool.ladder import request_with_ladder  # noqa: PLC0415

        # This is the replay tier's HTTP leg, and it is the one place replay can
        # carry a session: the recipe supplies selectors, the fetch is ours.
        replay_cookies = _cookies_for_tier(req, "replay")
        response, _profile = await request_with_ladder(
            "GET", req.url, timeout=req.timeout_s or 30.0, cookies=replay_cookies
        )
        if replay_cookies:
            _mark_cookies_applied(req, "replay")
        return response.text or "", response.status_code, str(response.url)

    return fetch


def _make_render_fetch(req: Any) -> Callable[[], Awaitable[tuple[str, int, str]]] | None:
    """A zero-arg render for replaying a browser-learned recipe, if possible."""
    if not _render_tier_enabled():
        return None

    async def render() -> tuple[str, int, str]:
        from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
        from scrapper_tool.agent.backends.browser import BrowserLaunchOptions  # noqa: PLC0415
        from scrapper_tool.patterns.render import render_html  # noqa: PLC0415

        cfg = AgentConfig.from_env().merged(**_build_overrides(req))
        replay_cookies = _cookies_for_tier(req, "replay")
        result = await render_html(
            req.url,
            browser=cfg.browser,
            timeout_s=cfg.timeout_s,
            options=BrowserLaunchOptions(
                headful=cfg.headful,
                proxy=cfg.proxy,
                user_data_dir=cfg.user_data_dir,
                headless_mode=cfg.camoufox_headless_mode,
                block_images=cfg.block_images,
                fingerprint_preset=cfg.fingerprint_preset,
                os=cfg.camoufox_os,
                locale=cfg.camoufox_locale,
            ),
            cdp_url=cfg.obscura_cdp_url,
            cookies=replay_cookies,
        )
        if replay_cookies:
            _mark_cookies_applied(req, "replay")
        return result.html, result.status, result.final_url

    return render


def _learn_recipe(req: Any, html: str, data: Any, *, source_tier: str) -> None:
    """Teach the cache from a tier that just succeeded. Never raises."""
    if not html or not data:
        return
    try:
        from scrapper_tool.recipe.replay import learn_from_success  # noqa: PLC0415

        recipe = learn_from_success(
            req.url,
            html,
            data,
            source_tier=source_tier,
            schema_json=req.schema_json,
            # If A/B/C already fetched a body, let the learner check whether the
            # selectors work against it too — a browser-learned recipe that also
            # matches raw HTML replays without a browser.
            cheap_html=req.__dict__.get("_a_b_c_html"),
        )
        if recipe is not None:
            # A "recipe" log row so _observe_cascade counts the learn without
            # this module reaching into the metrics registry.
            log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])
            log.append(_build_log_entry("recipe", outcome="won", reason="learned", duration_s=0.0))
    except Exception as exc:  # an optimisation for next time; never fail now
        _logger.debug("scrape.learn.failed", url=req.url, error=str(exc)[:160])


def _note_challenge(req: Any, log: list[dict[str, Any]], html: str, status_code: int) -> str | None:
    """Record which bot vendor walled us, if any. Returns the vendor name.

    Stashed on the request so the final payload can surface
    ``challenge_detected`` regardless of which tier ends up winning — knowing a
    page was walled is the single most useful thing for tuning a target, and it
    was previously invisible.
    """
    from scrapper_tool._challenge import is_interstitial  # noqa: PLC0415

    vendor = is_interstitial(html, status_code)
    if vendor is None:
        return None
    req.__dict__["_challenge_detected"] = vendor
    _logger.info("scrape.challenge_detected", url=req.url, vendor=vendor)
    log.append(
        _build_log_entry(
            "challenge",
            outcome="rejected",
            reason="blocked",
            duration_s=0.0,
            detail=f"{vendor} interstitial (status={status_code})",
        )
    )
    return vendor


def _should_skip_d_for_challenge(req: Any) -> bool:
    """Whether a detected challenge makes Pattern D a waste of ~30 s.

    Not a blanket skip. Scrapling's anti-bot weapon is ``solve_cloudflare``,
    which is Cloudflare-specific, so:

    * Cloudflare wall -> **run D**. That's precisely what it's for.
    * Any other vendor (Radware, DataDome, PerimeterX, Akamai, Kasada,
      Incapsula) -> **skip D** and go straight to the render tier. Scrapling has
      no solver for these, so D would burn a browser launch to re-fetch the same
      interstitial the ladder already got.
    * Nothing detected -> run D as before (unchanged behaviour).
    """
    vendor = req.__dict__.get("_challenge_detected")
    return bool(vendor) and vendor != "cloudflare"


def _tier_rank(tier: str) -> int:
    """Cost rank of a cascade tier (see recipe.policy.TIER_ORDER)."""
    from scrapper_tool.recipe.policy import tier_rank  # noqa: PLC0415

    return tier_rank(tier)


def _policy_start_rank(req: Any) -> int:
    """Lowest tier rank worth attempting for this URL, per the domain policy.

    0 (start from A/B/C) unless a *confident* policy has learned this domain
    needs a more expensive tier, in which case the cheaper ones are skipped.
    Stashes the chosen rank on the request so the deterministic-tier step can
    read it without a second lookup, and logs the skip for observability.
    """
    try:
        from scrapper_tool.recipe.policy import (  # noqa: PLC0415
            domain_policy_enabled,
            get_policy_store,
        )

        if not domain_policy_enabled():
            return 0
        policy = get_policy_store().get(req.url)
    except Exception as exc:  # a policy problem must never break a scrape
        _logger.debug("scrape.policy.lookup_failed", url=req.url, error=str(exc)[:120])
        return 0
    if policy is None:
        return 0
    rank = policy.start_tier_rank()
    req.__dict__["_policy_start_rank"] = rank
    if rank > 0:
        log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])
        log.append(
            _build_log_entry(
                "policy",
                outcome="skipped",
                reason="ok",
                duration_s=0.0,
                detail=(
                    f"domain learned to start at {policy.best_tier} "
                    f"({policy.observations} obs); skipping cheaper tiers"
                ),
            )
        )
        _logger.info(
            "scrape.policy.skip",
            url=req.url,
            best_tier=policy.best_tier,
            observations=policy.observations,
        )
    return rank


def _record_policy(payload: dict[str, Any] | None, req: Any) -> None:
    """After a scrape, remember which tier reached content on this domain.

    Best-effort: any failure is swallowed, because learning is an optimisation
    for next time and must never affect the result the caller already has.
    """
    if payload is None:
        return
    tier = payload.get("pattern_used")
    if not isinstance(tier, str) or payload.get("blocked"):
        return
    try:
        from scrapper_tool.recipe.policy import (  # noqa: PLC0415
            domain_policy_enabled,
            get_policy_store,
        )

        if not domain_policy_enabled():
            return
        get_policy_store().record(
            req.url, tier, challenge_vendor=req.__dict__.get("_challenge_detected")
        )
    except Exception as exc:
        _logger.debug("scrape.policy.record_failed", url=req.url, error=str(exc)[:120])


# ---------------------------------------------------------------------------
# Caller-supplied cookies
# ---------------------------------------------------------------------------


def _request_cookies(req: Any) -> list[CookieIn] | None:
    """The request-scoped cookie jar, or None when the caller sent none.

    Stashed on ``req.__dict__`` following the convention already used for
    ``_resolved_profile_dir`` and ``_render_intermediate_html``: request-scoped
    state that several cascade steps need, held for exactly one request and
    never persisted. Returning None rather than an empty list lets call sites
    pass it straight through as an optional argument.
    """
    jar = req.__dict__.get("_cookie_jar")
    if jar is None:
        jar = list(getattr(req, "cookies", None) or [])
        req.__dict__["_cookie_jar"] = jar
    return jar or None


def _cookies_for_tier(req: Any, tier: str) -> list[CookieIn] | None:
    """The caller's cookies narrowed to ``req.url``, or None when none apply.

    Every tier that injects cookies goes through here rather than reaching for
    ``_request_cookies`` directly, so the domain/path/secure/expiry rules are
    applied in exactly one place. A jar that matches nothing for this URL is
    reported as skipped — "I sent cookies and nothing happened" should never be
    silent, and "none of them were scoped to this host" is the single most
    likely explanation.
    """
    jar = _request_cookies(req)
    if not jar:
        return None
    applicable = cookies_for_url(jar, req.url)
    if not applicable:
        _mark_cookies_skipped(req, tier, "no_cookie_matched_this_url")
        return None
    return applicable


def _agent_cfg_for(req: Any, tier: str) -> Any:
    """Build this request's :class:`AgentConfig` and record its cookie outcome.

    The two always belong together — the config is what carries the jar into the
    tier, so the moment it is built is the moment we can say whether the jar will
    actually be carried. Pairing them in one call also keeps the answer from
    drifting away from the config it describes.
    """
    from scrapper_tool.agent import AgentConfig  # noqa: PLC0415

    cfg = AgentConfig.from_env().merged(**_build_overrides(req))
    if tier == "e1":
        _record_e1_cookie_outcome(req, cfg)
    else:
        _record_e2_cookie_outcome(req, cfg)
    return cfg


def _record_e1_cookie_outcome(req: Any, cfg: Any) -> None:
    """Record whether E1 will actually carry the caller's cookies.

    The injection itself lives in ``agent.extract._browser_cfg_kwargs`` — the
    agent layer must not import this module — so applicability is decided here,
    against the same probe the injection uses. Crawl4AI launches its own browser
    and takes cookies on ``BrowserConfig``; a build whose ``BrowserConfig`` has
    no ``cookies`` parameter simply cannot carry one.
    """
    if not _cookies_for_tier(req, "e1"):
        return
    if not _extras.crawl4ai_accepts("cookies"):
        _mark_cookies_skipped(req, "e1", "crawl4ai_browserconfig_has_no_cookies_param")
        return
    if not getattr(cfg, "cookies", None):
        # _build_overrides drops falsy values, so an empty jar never reaches the
        # config. Nothing to carry, and nothing to claim.
        return
    _mark_cookies_applied(req, "e1")


def _record_e2_cookie_outcome(req: Any, cfg: Any) -> None:
    """Record whether E2 will actually carry the caller's cookies.

    E2 injects into the live context via CDP (``agent.browse._inject_cookies``),
    so the question is not "does browser-use accept a kwarg" but "is there a CDP
    endpoint at all". Camoufox is Firefox, Firefox dropped CDP, and
    ``agent_browse`` refuses to run without one — so on the default backend E2
    never starts, let alone carries a session.
    """
    if not _cookies_for_tier(req, "e2"):
        return
    if getattr(cfg, "browser", None) == "camoufox":
        _mark_cookies_skipped(req, "e2", "camoufox_exposes_no_cdp_endpoint")
        return
    if not getattr(cfg, "cookies", None):
        return
    _mark_cookies_applied(req, "e2")


def _mark_cookies_applied(req: Any, tier: str) -> None:
    """Record that ``tier`` actually carried the caller's cookies.

    Without this, "I passed cookies and still got the logged-out page" is an
    unfalsifiable complaint. Mirrors the ``hostile_skipped`` convention.
    """
    if not _request_cookies(req):
        return
    applied: list[str] = req.__dict__.setdefault("_cookies_applied", [])
    if tier not in applied:
        applied.append(tier)


def _mark_cookies_skipped(req: Any, tier: str, reason: str) -> None:
    """Record that ``tier`` ran but could NOT carry the caller's cookies."""
    if not _request_cookies(req):
        return
    skipped: list[dict[str, str]] = req.__dict__.setdefault("_cookies_skipped", [])
    if not any(entry["tier"] == tier for entry in skipped):
        skipped.append({"tier": tier, "reason": reason})


def _assert_cookies_safe_to_send(req: Any) -> None:
    """Refuse to route credentialed traffic through an untrusted proxy pool.

    Asserted at the entrypoint boundary rather than threaded as a kwarg: a
    defaulted kwarg that mypy won't enforce drifts, and the failure mode here is
    silent credential disclosure to whoever runs a free proxy. Raises
    ``ConfigurationError`` (503-class) before a byte leaves the process.
    """
    if not _request_cookies(req):
        return
    try:
        from scrapper_tool.proxy import ProxyPool  # noqa: PLC0415

        pool = ProxyPool.from_env()
    except Exception:  # pragma: no cover — a broken pool config fails elsewhere
        return
    if pool is not None:
        pool.assert_safe_for_credentials()


def _harvest_cookies(req: Any, harvested: Any, *, tier: str) -> None:
    """Fold cookies a tier *won* into the request-scoped jar.

    A `cf_clearance` bought with an expensive render was previously computed,
    returned on ``RenderResult.cookies``, and then dropped on the floor by both
    consumers — so every later tier re-fought the same wall.

    Scope is one request, deliberately. These are **not** written to the recipe
    store, for five independent reasons, any one of them sufficient: that store
    lives at a fixed world-readable temp path; it is keyed by *domain* while
    cookies are per-*identity*, so two callers scraping one domain as different
    users would silently share a session; its TTL is 14 days against a
    ~30-minute clearance; its contract is "every read failure is silent", which
    is right for a selector and wrong for a credential, where the worst case of
    a *successful* read is impersonating the wrong user; and the sanctioned
    mechanism for cross-request persistence already exists in
    ``persist_browser_profile_dir``.
    """
    if not harvested:
        return
    try:
        from scrapper_tool.cookies import from_playwright, merge  # noqa: PLC0415

        incoming = from_playwright([dict(entry) for entry in harvested])
    except Exception as exc:
        _logger.debug("scrape.cookies.harvest_failed", tier=tier, error=str(exc)[:120])
        return
    if not incoming:
        return

    existing = req.__dict__.get("_cookie_jar") or []
    req.__dict__["_cookie_jar"] = merge(existing, incoming)
    harvested_from: list[str] = req.__dict__.setdefault("_cookies_harvested_from", [])
    if tier not in harvested_from:
        harvested_from.append(tier)
    _logger.debug(
        "scrape.cookies.harvested",
        tier=tier,
        count=len(incoming),
    )


def _render_tier_enabled() -> bool:
    """Render tier is on by default; ``SCRAPPER_TOOL_RENDER_TIER=0`` disables it."""
    return _extras.render_tier_enabled()


async def _do_render_step(
    req: Any,
    attempts: list[str],
    start: float,
) -> tuple[dict[str, Any] | None, BaseException | None]:
    """Stealth-browser render tier — rendered HTML + deterministic extract, NO LLM.

    Sits between Pattern D and the LLM tiers. Measured value: on a target where
    the HTTP ladder got 403 on all four TLS profiles, a single Camoufox render
    returned 1.35 MB of real content; on another, rendering turned 4 extractable
    headlines into 212. Both without an LLM call, so this is strictly cheaper and
    more reliable than escalating to E1/E2.

    Returns ``(response, error)`` — response when the render produced an accepted
    signal, else None so the cascade falls through to the LLM tiers.
    """
    log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])
    if not _render_tier_enabled():
        return None, None

    # Resolve imports BEFORE claiming an attempt. Mirrors Pattern D's
    # `hostile_skipped` convention: a tier that could not even start must not
    # appear in `pattern_attempts`, or the log implies a render was tried.
    try:
        from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
        from scrapper_tool.agent.backends.browser import BrowserLaunchOptions  # noqa: PLC0415
        from scrapper_tool.patterns.render import render_html  # noqa: PLC0415
    except ImportError as exc:
        _logger.info("scrape.render.skipped_no_extra", url=req.url, error=str(exc)[:160])
        log.append(
            _build_log_entry(
                "render",
                outcome="skipped",
                reason="extra_missing",
                duration_s=0.0,
                detail="[llm-agent] extra not installed",
            )
        )
        return None, None

    attempts.append("render")
    r_start = time.perf_counter()
    try:
        # Same config path as the E tiers, so `_build_overrides` carries the
        # cascade-resolved profile dir (clearance cookies from earlier rungs)
        # and per-request browser/timeout overrides through unchanged.
        cfg = AgentConfig.from_env().merged(**_build_overrides(req))
        options = BrowserLaunchOptions(
            headful=cfg.headful,
            proxy=cfg.proxy,
            user_data_dir=cfg.user_data_dir,
            headless_mode=cfg.camoufox_headless_mode,
            block_images=cfg.block_images,
            fingerprint_preset=cfg.fingerprint_preset,
            os=cfg.camoufox_os,
            locale=cfg.camoufox_locale,
        )
        render_cookies = _cookies_for_tier(req, "render")
        result = await render_html(
            req.url,
            browser=cfg.browser,
            timeout_s=cfg.timeout_s,
            options=options,
            cdp_url=cfg.obscura_cdp_url,
            cookies=render_cookies,
        )
        if render_cookies:
            _mark_cookies_applied(req, "render")
    except Exception as exc:
        _logger.warning("scrape.render.failed", url=req.url, error=str(exc)[:200])
        log.append(
            _build_log_entry(
                "render",
                outcome="failed",
                reason="exception",
                duration_s=time.perf_counter() - r_start,
                detail=f"{type(exc).__name__}: {exc!s}"[:200],
            )
        )
        return None, exc

    # Harvest BEFORE the accept/reject branch below. A render can win a
    # cf_clearance and still produce no accepted signal — that is precisely the
    # case where E1 should inherit the clearance rather than re-fight the wall
    # from scratch. Harvesting only on the success path would throw it away in
    # the one situation it is most valuable.
    _harvest_cookies(req, result.cookies, tier="render")

    html, status_code, final_url = result.html, result.status, result.final_url
    req.__dict__["_render_intermediate_html"] = html

    css_data, product, json_ld, microdata_price = _run_d_extractors(req, html, final_url)
    success = _classify_extraction_success(
        req,
        status_code=status_code,
        text=html,
        product=product,
        microdata_price=microdata_price,
        json_ld=json_ld,
        css_data=css_data,
    )

    duration = time.perf_counter() - r_start
    if not success:
        _logger.info("scrape.render.no_signal", url=req.url, status_code=status_code)
        log.append(
            _build_log_entry(
                "render",
                outcome="rejected",
                reason="no_signal",
                duration_s=duration,
                detail=f"status={status_code}; rendered {len(html)} bytes, no signal",
            )
        )
        return None, None

    _logger.info("scrape.render.win", url=req.url, status_code=status_code, bytes=len(html))
    log.append(_build_log_entry("render", outcome="won", reason="ok", duration_s=duration))
    # Learn from the expensive win so the next page on this domain replays free.
    _learn_recipe(req, html, css_data or product, source_tier="render")
    return (
        {
            "url": final_url,
            "pattern_used": "render",
            "pattern_attempts": attempts,
            "escalation_log": list(log),
            "product": product,
            "data": css_data,
            "raw_text": html,
            "intermediate_raw_text": html,
            "json_ld": json_ld,
            "microdata_price": microdata_price,
            "rendered_markdown": None,
            "screenshots": None,
            "tokens_used": 0,  # the point of this tier: zero LLM
            "steps_used": 0,
            "blocked": False,
            "error": None,
            "hostile_skipped": False,
            "is_structured": True,
            "duration_s": time.perf_counter() - start,
        },
        None,
    )


def _run_d_extractors(
    req: Any, html: str, final_url: str
) -> tuple[
    list[Any] | dict[str, Any] | None,
    dict[str, Any] | None,
    list[Any] | None,
    dict[str, Any] | None,
]:
    """Run the v1.4.0 D-step extractor pipeline.

    Returns ``(css_data, product, json_ld, microdata_price)``:

    * ``css_data`` — output of the CSS extractor when the caller's
      ``schema_json`` is CSS-shaped; otherwise None.
    * ``product`` — auto-detected ProductOffer from JSON-LD.
    * ``json_ld`` — raw json-ld blocks list.
    * ``microdata_price`` — ``{"price": ..., "currency": ...}``.

    Order of extractors:

    1. ``css`` (only when ``schema_json`` is CSS-shaped) — populates
       ``css_data``. When CSS yields rows, this wins; the legacy B/C
       extractors still run for observability but their output goes
       into ``product``/``json_ld``/``microdata_price`` rather than
       being the canonical signal.
    2. ``json_ld_product``
    3. ``microdata_price``

    Open Graph isn't run by default in the legacy D path (kept available
    for the cascade DSL). Adding it here unconditionally would shift the
    "what's a structured signal" definition for back-compat callers, so
    we keep it opt-in via the cascade DSL.
    """
    from scrapper_tool._extractors import get as get_extractor  # noqa: PLC0415
    from scrapper_tool._extractors.css import looks_like_css_schema  # noqa: PLC0415

    schema = getattr(req, "schema_json", None)

    css_data: list[Any] | dict[str, Any] | None = None
    if looks_like_css_schema(schema):
        css_result = get_extractor("css").extract(
            html, base_url=final_url, options={"schema": schema}
        )
        if css_result.has_signal:
            css_data = css_result.data

    json_ld_result = get_extractor("json_ld_product").extract(html, base_url=final_url)
    microdata_result = get_extractor("microdata_price").extract(html, base_url=final_url)

    product: dict[str, Any] | None = None
    json_ld: list[Any] | None = None
    if json_ld_result.has_signal and isinstance(json_ld_result.data, dict):
        product = json_ld_result.data.get("product")
        json_ld = json_ld_result.data.get("json_ld")

    microdata_price: dict[str, Any] | None = None
    if microdata_result.has_signal and isinstance(microdata_result.data, dict):
        microdata_price = microdata_result.data

    return css_data, product, json_ld, microdata_price


async def _do_scrape_e_tier(  # noqa: PLR0915 — linear cascade; splitting hides the order
    req: Any,
    attempts: list[str],
    start: float,
    hostile_skipped: bool,
    last_error: BaseException | None,
) -> dict[str, Any]:
    """E1 → E2 escalation. Shared between mode=auto (after A/B/C+D fall through),
    mode=hostile (after D fails with hostile_fallback=True), and mode=extract.

    Honors mode="extract" — when set, raises rather than continuing to E2.

    v1.6.0: also honors ``interactive``. E2 is the most expensive tier by a wide
    margin, and auto-escalating into it on *any* blocked E1 spends an agent loop
    to hit the same wall more slowly. Unless the caller says the target needs
    interaction, the cascade now stops at E1 and returns the blocked result.
    An explicit ``mode="browse"`` is a direct request for E2 and is never gated.
    """
    log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])
    blocked_e1: Any = None

    # ----- E1 (Pattern E extract) -----
    if req.mode in ("auto", "extract", "hostile"):
        attempts.append("e1")
        e1_start = time.perf_counter()
        try:
            from scrapper_tool.agent import agent_extract  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(_AGENT_NOT_INSTALLED) from exc

        cfg = _agent_cfg_for(req, "e1")
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
            e1_duration = time.perf_counter() - e1_start
            # Before the accept/reject branch, for the same reason the render
            # tier harvests early: E1 can win a captcha clearance and still
            # extract nothing, and that is exactly when E2 most wants it.
            _harvest_cookies(req, result.cookies, tier="e1")
            if not result.blocked:
                log.append(
                    _build_log_entry("e1", outcome="won", reason="ok", duration_s=e1_duration)
                )
                # The highest-value thing to learn from: this is the tier that
                # costs real money per page, and a recipe replaces it entirely.
                # Learn against the DOM an earlier tier captured — E1 returns
                # markdown, and selectors have to be derived from the real HTML.
                _learn_recipe(
                    req,
                    req.__dict__.get("_render_intermediate_html")
                    or req.__dict__.get("_d_intermediate_html")
                    or "",
                    result.data,
                    source_tier="render" if req.__dict__.get("_render_intermediate_html") else "d",
                )
                return _scrape_response_from_agent(
                    result, attempts, start, mode="e1", hostile_skipped=hostile_skipped, req=req
                )
            log.append(
                _build_log_entry(
                    "e1",
                    outcome="failed",
                    reason="blocked",
                    duration_s=e1_duration,
                    detail=result.error or "agent reported blocked",
                )
            )
            last_error = AgentBlockedError(result.error or "blocked")
            blocked_e1 = result
        except AgentBlockedError as exc:
            log.append(
                _build_log_entry(
                    "e1",
                    outcome="failed",
                    reason="blocked",
                    duration_s=time.perf_counter() - e1_start,
                    detail=str(exc),
                )
            )
            last_error = exc

    if req.mode == "extract":
        if isinstance(last_error, BaseException):
            raise last_error
        raise ScrapingError("/scrape mode=extract failed without an exception (unreachable)")

    # ----- E2 gate: interactive tasks only -----
    # mode="browse" is a direct request for E2, so it bypasses the gate.
    if req.mode != "browse" and not getattr(req, "interactive", False):
        log.append(
            _build_log_entry(
                "e2",
                outcome="skipped",
                reason="no_signal",
                duration_s=0.0,
                detail="interactive=false; E2 reserved for login/pagination/form flows",
            )
        )
        if blocked_e1 is not None:
            # Hand back E1's blocked result — the caller gets the escalation log
            # and whatever partial content E1 did see, which is strictly more
            # useful than a bare error.
            return _scrape_response_from_agent(
                blocked_e1, attempts, start, mode="e1", hostile_skipped=hostile_skipped, req=req
            )
        if isinstance(last_error, BaseException):
            raise last_error
        raise ScrapingError("/scrape exhausted the cascade without an exception (unreachable)")

    # ----- E2 (Pattern E browse) -----
    attempts.append("e2")
    e2_start = time.perf_counter()
    try:
        from scrapper_tool.agent import agent_browse  # noqa: PLC0415
    except ImportError as exc:
        raise ConfigurationError(_AGENT_NOT_INSTALLED) from exc

    instruction = req.instruction or (
        f"Extract structured data matching: {req.schema_json}"
        if req.schema_json is not None
        else "Extract the main content of this page"
    )
    cfg = _agent_cfg_for(req, "e2")
    schema = req.schema_json if isinstance(req.schema_json, dict) else None
    try:
        result = await agent_browse(req.url, instruction, schema=schema, config=cfg)
        _harvest_cookies(req, result.cookies, tier="e2")
        log.append(
            _build_log_entry(
                "e2",
                outcome="won",
                reason="ok",
                duration_s=time.perf_counter() - e2_start,
            )
        )
        return _scrape_response_from_agent(
            result, attempts, start, mode="e2", hostile_skipped=hostile_skipped, req=req
        )
    except AgentBlockedError as exc:
        log.append(
            _build_log_entry(
                "e2",
                outcome="failed",
                reason="blocked",
                duration_s=time.perf_counter() - e2_start,
                detail=str(exc),
            )
        )
        msg = f"All patterns blocked: {', '.join(attempts)}. Last error: {exc}"
        raise AgentBlockedError(msg) from exc


async def _do_hostile_only(req: Any, attempts: list[str], start: float) -> dict[str, Any]:
    """mode='hostile' — invoke Pattern D directly; honor hostile_fallback on failure.

    Reuses _do_d_step. On success returns its payload. On failure either falls
    through to the standard E1 → E2 escalation (when hostile_fallback=True,
    default) or raises a clean error (when False).
    """
    d_response, d_error, hostile_skipped = await _do_d_step(req, attempts, start)
    if d_response is not None:
        return d_response

    if not getattr(req, "hostile_fallback", True):
        if hostile_skipped:
            raise ConfigurationError(
                "mode=hostile requires the [hostile] extra. "
                "Install with: pip install scrapper-tool[hostile]"
            )
        raise AgentBlockedError(
            f"mode=hostile and Pattern D failed for {req.url}: "
            f"{d_error or 'classifier rejected D output'}"
        )

    # Fall through to E1 → E2 with hostile_skipped flag preserved.
    return await _do_scrape_e_tier(req, attempts, start, hostile_skipped, last_error=d_error)


def _resolve_profile_dir(req: Any) -> tuple[str | None, str | None]:
    """Resolve the cascade's browser profile dir.

    Returns ``(profile_dir, cleanup_dir)``:

    * ``profile_dir`` — the dir to pass to Scrapling/Crawl4AI/browser-use,
      or None when the cascade doesn't need one (mode=fetch, or mode=auto
      without [hostile] installed and no caller-provided dir).
    * ``cleanup_dir`` — the dir the cascade owns and must rmtree on exit,
      or None when the caller owns the lifecycle (provided their own dir
      via ``persist_browser_profile_dir``).

    Decision matrix:

    +-----------------------+------------+--------------------------------+
    | req.mode              | extra      | result                         |
    +-----------------------+------------+--------------------------------+
    | fetch                 | -          | (None, None) — no browser      |
    | extract / browse      | -          | honor caller dir if any        |
    | auto / hostile        | hostile-no | honor caller dir if any        |
    | auto / hostile        | hostile-ok | ephemeral if no caller dir     |
    +-----------------------+------------+--------------------------------+
    """
    explicit = getattr(req, "persist_browser_profile_dir", None)
    if explicit:
        # Caller owns the dir; we never clean it up.
        return explicit, None

    # mode=fetch never spins up a browser → no point allocating a dir.
    if req.mode == "fetch":
        return None, None

    # Only allocate ephemeral dirs when D might run AND can actually run.
    # mode=auto with [hostile] missing falls straight through to E1; the
    # E-tier still benefits from a shared dir (E2 inherits E1 cookies),
    # but the marginal value is small enough that we keep allocations
    # tied to D's availability for now. Direct E-tier modes (extract /
    # browse) honor only the caller-provided dir.
    if req.mode in ("auto", "hostile") and _hostile_available():
        ephemeral = tempfile.mkdtemp(prefix="scrapper-cascade-")
        return ephemeral, ephemeral

    return None, None


async def _do_scrape(req: Any) -> dict[str, Any]:
    """POST /scrape — auto-escalating ladder A/B/C → D → E1 → E2.

    Decision logic:
    - mode="fetch": only run A/B/C, never escalate.
    - mode="extract" / "browse": forward straight to that pattern.
    - mode="hostile" (NEW v1.2.0): invoke Pattern D directly, skipping A/B/C.
      Falls through to E1/E2 unless hostile_fallback=false.
    - mode="auto" (default): try A/B/C first; if blocked or schema not satisfied,
      try Pattern D when [hostile] is installed; if D is skipped or fails,
      escalate to E1; if E1 is blocked, escalate to E2.

    Every response carries ``hostile_skipped: bool`` (D was skipped because the
    extra is missing) and ``is_structured: bool`` (the sidecar's verdict on
    whether the page yielded a real payload).

    v1.3.0: when D might run, the cascade allocates a per-request
    ``user_data_dir`` and threads it to D + E1 + E2 so Cloudflare clearance
    cookies persist across cascade steps. The dir is ephemeral by default
    (cleaned up in ``finally``); callers wanting cross-request persistence
    set ``persist_browser_profile_dir``.
    """
    start = time.perf_counter()
    attempts: list[str] = []

    # v1.3.0: resolve and stash the profile dir BEFORE any cascade step
    # so _do_d_step + _build_overrides can read it via req.__dict__.
    profile_dir, cleanup_dir = _resolve_profile_dir(req)
    req.__dict__["_resolved_profile_dir"] = profile_dir

    # Before any tier runs, and before a byte leaves the process.
    _assert_cookies_safe_to_send(req)

    payload: dict[str, Any] | None = None
    raised = False
    try:
        payload = await _do_scrape_inner(req, attempts, start)
        # Surfaced here rather than in each tier's return: the vendor that
        # walled us is worth reporting no matter which tier eventually won.
        payload["challenge_detected"] = req.__dict__.get("_challenge_detected")
        # Which tiers actually carried the caller's cookies, and which ran
        # without them. Both keys are always present when cookies were supplied
        # so a caller can tell "not applied" from "field absent".
        if _request_cookies(req):
            payload["cookies_applied"] = req.__dict__.get("_cookies_applied", [])
            payload["cookies_skipped"] = req.__dict__.get("_cookies_skipped", [])
        harvested_from = req.__dict__.get("_cookies_harvested_from")
        if harvested_from:
            # Metadata only — never the cookies themselves. The response body is
            # what every reverse proxy in the path writes to its access log.
            payload["cookies_harvested_from"] = harvested_from
        # F2: remember which tier reached content so the next request for this
        # domain can start there. One place, so every winning tier is recorded.
        _record_policy(payload, req)
        return payload
    except BaseException:
        raised = True
        raise
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        # v1.4.0 — Prometheus observation. Safe no-op when prometheus-client
        # isn't installed (e.g. older [http] installs).
        _observe_cascade(payload, exception=raised)


async def _do_scrape_inner(req: Any, attempts: list[str], start: float) -> dict[str, Any]:
    """The actual cascade body — extracted so _do_scrape's try/finally
    can wrap it cleanly without indenting the whole thing one level."""
    last_error: BaseException | None = None
    hostile_skipped = False

    # ----- mode=hostile fast-path (no A/B/C ladder) -----
    if req.mode == "hostile":
        return await _do_hostile_only(req, attempts, start)

    # v1.4.0 — escalation_log accumulator. Stashed on req.__dict__ so
    # _do_d_step / _do_scrape_e_tier can append from inside their stack
    # without us threading it explicitly.
    log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])

    # ----- Replay (tier 0 — cached recipe, no browser, no LLM) -----------
    if req.mode == "auto":
        replayed = await _do_replay_step(req, attempts, start)
        if replayed is not None:
            return replayed

    # v1.6.0 F2 — per-domain tier memory. If this domain has repeatedly needed a
    # more expensive tier (the ladder always 403s, only render gets through),
    # skip the tiers we've learned are doomed. Purely a starting hint: the
    # cascade still falls through from wherever it starts, and replay above ran
    # regardless. mode="fetch" is exempt — it's an explicit "A/B/C only" request.
    start_rank = _policy_start_rank(req) if req.mode == "auto" else 0

    # ----- A/B/C -----
    if req.mode in ("auto", "fetch") and start_rank <= _tier_rank("a_b_c"):
        attempts.append("a_b_c")
        a_b_c_start = time.perf_counter()
        try:
            from scrapper_tool.ladder import request_with_ladder  # noqa: PLC0415

            a_b_c_cookies = _cookies_for_tier(req, "a_b_c")
            response, profile = await request_with_ladder(
                "GET", req.url, timeout=req.timeout_s or 30.0, cookies=a_b_c_cookies
            )
            if a_b_c_cookies:
                _mark_cookies_applied(req, "a_b_c")
            text = response.text or ""
            # Kept for the recipe learner: if a later tier's selectors also
            # match this raw body, its recipe can replay without a browser.
            req.__dict__["_a_b_c_html"] = text
            # Same extractor pipeline as D and render, which crucially includes
            # the CSS extractor when the caller supplied a CSS-shaped schema.
            # Running only B/C here meant every CSS-schema request escalated past
            # the cheapest tier — paying for Scrapling's browser at minimum —
            # even when the raw HTML already had everything the selectors need.
            css_data, product, json_ld, microdata_price = _run_d_extractors(
                req, text, str(response.url)
            )

            success = _classify_extraction_success(
                req,
                status_code=response.status_code,
                text=text,
                product=product,
                microdata_price=microdata_price,
                json_ld=json_ld,
                css_data=css_data,
            )

            a_b_c_duration = time.perf_counter() - a_b_c_start
            if success:
                _ = profile  # currently unused in response shape; kept for logging
                log.append(
                    _build_log_entry("a_b_c", outcome="won", reason="ok", duration_s=a_b_c_duration)
                )
                return {
                    "url": str(response.url),
                    "pattern_used": "a_b_c",
                    "pattern_attempts": attempts,
                    "escalation_log": list(log),
                    "product": product,
                    "data": css_data,
                    "raw_text": text,
                    "intermediate_raw_text": None,
                    "json_ld": json_ld,
                    "microdata_price": microdata_price,
                    "rendered_markdown": None,
                    "screenshots": None,
                    "tokens_used": 0,
                    "steps_used": 0,
                    "blocked": False,
                    "error": None,
                    "hostile_skipped": False,
                    "is_structured": True,
                    "duration_s": time.perf_counter() - start,
                }
            vendor = _note_challenge(req, log, text, response.status_code)
            log.append(
                _build_log_entry(
                    "a_b_c",
                    outcome="rejected",
                    reason="no_signal",
                    duration_s=a_b_c_duration,
                    detail=(
                        f"status={response.status_code}; "
                        + (f"{vendor} challenge page" if vendor else "classifier rejected")
                    ),
                )
            )
        except BlockedError as exc:
            last_error = exc
            log.append(
                _build_log_entry(
                    "a_b_c",
                    outcome="failed",
                    reason="blocked",
                    duration_s=time.perf_counter() - a_b_c_start,
                    detail=str(exc),
                )
            )
        except Exception as exc:
            last_error = exc
            log.append(
                _build_log_entry(
                    "a_b_c",
                    outcome="failed",
                    reason="exception",
                    duration_s=time.perf_counter() - a_b_c_start,
                    detail=f"{type(exc).__name__}: {exc!s}",
                )
            )

    if req.mode == "fetch":
        # mode="fetch" forces A/B/C only; if it failed, surface the error.
        if isinstance(last_error, BaseException):
            raise last_error
        raise ScrapingError("/scrape mode=fetch failed without an exception (unreachable)")

    # ----- Deterministic tiers: Pattern D, then stealth render (no LLM) -----
    if req.mode == "auto":
        det_response, last_error, hostile_skipped = await _do_deterministic_tiers(
            req, attempts, start, last_error
        )
        if det_response is not None:
            return det_response

    # ----- E1 → E2 escalation -----
    return await _do_scrape_e_tier(req, attempts, start, hostile_skipped, last_error)


async def _do_deterministic_tiers(
    req: Any,
    attempts: list[str],
    start: float,
    last_error: BaseException | None,
) -> tuple[dict[str, Any] | None, BaseException | None, bool]:
    """Run the no-LLM tiers between the HTTP ladder and the agent tiers.

    Pattern D (Scrapling) then the stealth render, in cost order. Grouped
    because they answer the same question — "can we get this
    deterministically?" — and because either failing must still let the next one
    try. Returns ``(response, last_error, hostile_skipped)``; a None response
    means keep escalating to E1.
    """
    log: list[dict[str, Any]] = req.__dict__.setdefault("_escalation_log", [])
    hostile_skipped = False
    # F2: the per-domain policy may have learned this domain needs render (or an
    # LLM tier), in which case D is doomed and worth skipping.
    start_rank = int(req.__dict__.get("_policy_start_rank", 0))

    if start_rank > _tier_rank("d"):
        pass  # policy says skip D — see _policy_start_rank
    elif _should_skip_d_for_challenge(req):
        # A wall Scrapling can't solve — don't spend ~30 s proving it.
        log.append(
            _build_log_entry(
                "d",
                outcome="skipped",
                reason="blocked",
                duration_s=0.0,
                detail=(
                    f"{req.__dict__['_challenge_detected']} challenge; "
                    "Scrapling only solves Cloudflare"
                ),
            )
        )
    else:
        d_response, d_error, hostile_skipped = await _do_d_step(req, attempts, start)
        if d_response is not None:
            return d_response, last_error, hostile_skipped
        if d_error is not None:
            last_error = d_error

    if start_rank <= _tier_rank("render"):
        r_response, r_error = await _do_render_step(req, attempts, start)
        if r_response is not None:
            return r_response, last_error, hostile_skipped
        if r_error is not None:
            last_error = r_error
    return None, last_error, hostile_skipped


async def _do_map(req: MapRequest) -> dict[str, Any]:
    """POST /map — discover URLs on the seed's site."""
    from scrapper_tool.crawl.map import make_ladder_fetch, map_site  # noqa: PLC0415

    start = time.perf_counter()
    result = await map_site(
        req.url,
        max_urls=req.max_urls,
        same_domain=req.same_domain,
        include_sitemap=req.include_sitemap,
        fetch=make_ladder_fetch(req.timeout_s or 30.0) if req.fetch_seed else None,
    )
    return {
        "seed": result.seed,
        "urls": result.urls,
        "count": len(result.urls),
        "from_sitemap": result.from_sitemap,
        "from_links": result.from_links,
        "truncated": result.truncated,
        "dropped_by_limit": result.dropped_by_limit,
        "sitemaps_read": list(result.sitemaps_read),
        "duration_s": round(time.perf_counter() - start, 3),
    }


async def _do_crawl(req: CrawlRequest) -> dict[str, Any]:
    """POST /crawl — traverse the site, running the full cascade per page.

    Each page goes through ``_do_scrape``, so a crawl inherits recipe replay,
    the render tier, challenge detection, and proxy rotation for free — and the
    recipe learned on page one makes the rest of the crawl cheap.
    """
    from scrapper_tool.crawl.crawl import crawl_to_list  # noqa: PLC0415

    start = time.perf_counter()

    async def scrape_one(url: str) -> dict[str, Any]:
        page_req = ScrapeRequest(
            url=url,
            schema_json=req.schema_json,
            interactive=req.interactive,
            timeout_s=req.timeout_s,
        )
        return await _do_scrape(page_req)

    pages, stats = await crawl_to_list(
        req.url,
        scrape=scrape_one,
        depth=req.depth,
        max_pages=req.max_pages,
        concurrency=req.concurrency,
        same_domain=req.same_domain,
        respect_robots=req.respect_robots,
    )

    results: list[dict[str, Any]] = []
    for page in pages:
        payload = dict(page.payload or {})
        if not req.include_html:
            # A 50-page crawl of rendered pages is tens of MB of HTML; the
            # extracted data is what a crawl is for.
            payload.pop("raw_text", None)
            payload.pop("intermediate_raw_text", None)
        results.append(
            {
                "url": page.url,
                "depth": page.depth,
                "ok": page.ok,
                "error": page.error,
                "skipped_reason": page.skipped_reason,
                "result": payload or None,
            }
        )

    return {
        "seed": req.url,
        "pages": results,
        "stats": stats.as_dict(),
        "duration_s": round(time.perf_counter() - start, 3),
    }


def _scrape_response_from_agent(
    result: Any,
    attempts: list[str],
    start: float,
    *,
    mode: Literal["e1", "e2"],
    hostile_skipped: bool = False,
    req: Any = None,
) -> dict[str, Any]:
    """Convert an :class:`AgentResult` into the /scrape response shape.

    v1.4.0: ``req`` (optional, default None for back-compat) is read for
    ``intermediate_raw_text`` (the best pre-LLM HTML captured by an earlier
    tier) and the ``escalation_log`` accumulator. Both default to None / empty
    when no req is threaded.
    """
    import base64  # noqa: PLC0415

    screenshots: list[str] | None = None
    if result.screenshots:
        screenshots = [base64.b64encode(s).decode("ascii") for s in result.screenshots[:3]]

    # Prefer the render tier's HTML when it ran — it's strictly richer than D's
    # (rendered DOM vs a possibly-unhydrated fetch), and it's the debugging
    # artefact you want when the LLM tier is being asked why it was needed.
    intermediate = None
    if req is not None:
        intermediate = req.__dict__.get("_render_intermediate_html") or req.__dict__.get(
            "_d_intermediate_html"
        )
    log: list[dict[str, Any]] = (
        list(req.__dict__.get("_escalation_log", [])) if req is not None else []
    )

    return {
        "url": result.final_url,
        "pattern_used": mode,
        "pattern_attempts": attempts,
        "escalation_log": log,
        "product": None,
        "data": result.data,
        "raw_text": None,
        "intermediate_raw_text": intermediate,
        "json_ld": None,
        "microdata_price": None,
        "rendered_markdown": result.rendered_markdown,
        "screenshots": screenshots,
        "tokens_used": result.tokens_used,
        "steps_used": result.steps_used,
        "blocked": result.blocked,
        "error": result.error,
        "hostile_skipped": hostile_skipped,
        "is_structured": _is_e_tier_structured(result.data, result.blocked),
        "duration_s": time.perf_counter() - start,
    }


# ---------------------------------------------------------------------------
# Readiness checks
# ---------------------------------------------------------------------------


async def _readiness_payload() -> dict[str, Any]:
    """Build the /ready response body.

    ``checks`` keys (v1.1.2):

    * ``agent_installed`` — ``[llm-agent]`` Python extra importable.
    * ``agent_runnable`` (NEW) — ``agent_installed`` AND the on-disk
      binary for the configured browser is present. **This is the
      field operators should gate Pattern E on.** ``agent_installed``
      true + ``agent_runnable`` false = lib is there but Firefox /
      Camoufox / Chromium isn't downloaded; cheap A/B/C still works,
      E1/E2 will fail at runtime.
    * ``browser`` — configured ``SCRAPPER_TOOL_AGENT_BROWSER``.
    * ``browser_binary`` — Python module probe for the configured
      backend (kept for backward compat; ``agent_runnable`` is the
      authoritative readiness signal now).
    * ``hostile_installed`` — ``[hostile]`` extra (Scrapling).
    * ``llm_*`` — LM Studio / Ollama / vLLM probe.
    * ``warnings`` (NEW v1.1.3) — non-fatal advisories. Currently emits
      ``hostile_not_installed`` when ``[hostile]`` is absent so operators
      can see at a glance that ``/scrape mode=auto`` will skip Pattern D
      and pay LLM costs on hostile vendors. Does NOT change ``status``.

    ``status`` resolution:

    * ``ready``    — ``agent_runnable`` ∧ LLM reachable + model loaded.
    * ``degraded`` — sidecar can serve A/B/C (``/fetch`` and
      ``/scrape mode=fetch``) but Pattern E or LLM is not available.
    * ``not_ready`` — ``[llm-agent]`` extra not even installed.
    """
    agent_installed = _agent_available()
    hostile_installed = _hostile_available()
    user_data_dir_ok = _user_data_dir_supported()
    warnings_list: list[str] = []
    if not hostile_installed:
        warnings_list.append(
            "hostile_not_installed: cascade will skip Pattern D and pay LLM costs "
            "on hostile vendors. Install with: pip install scrapper-tool[hostile]"
        )
    if agent_installed and not user_data_dir_ok:
        warnings_list.append(
            "user_data_dir_unsupported: Pattern D's CF clearance will NOT carry "
            "forward to E1/E2 (your installed Crawl4AI / browser-use versions "
            "appear to ignore user_data_dir). Upgrade Crawl4AI >= 0.6 + "
            "browser-use >= 0.5 to get the v1.3.0 shared-clearance benefit."
        )
    checks: dict[str, Any] = {
        "agent_installed": agent_installed,
        "agent_runnable": False,  # filled in below once we know the browser
        "hostile_installed": hostile_installed,
        "user_data_dir_supported": user_data_dir_ok,
        "browser": None,
        "browser_binary": None,
        "llm_backend": None,
        "llm_url": None,
        "llm_reachable": None,
        "llm_model": None,
        "llm_model_available": None,
        "warnings": warnings_list,
    }
    if not agent_installed:
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
    checks["agent_runnable"] = _agent_runnable(cfg.browser)
    reachable, model_available = await _probe_llm(cfg)
    checks["llm_reachable"] = reachable
    checks["llm_model_available"] = model_available

    all_pass = (
        checks["agent_runnable"] is True
        and checks["llm_reachable"] is True
        and checks["llm_model_available"] is True
    )
    return {
        "status": "ready" if all_pass else "degraded",
        "version": __version__,
        "checks": checks,
    }


def _check_browser_module(browser: str) -> str:
    """Best-effort: 'ok' / 'missing' / 'unknown' for the configured browser's Python module."""
    return _extras.check_browser_module(browser)


async def _probe_llm(cfg: Any) -> tuple[bool | None, bool | None]:
    """Probe the configured LLM endpoint. Returns (reachable, model_available)."""
    return await _extras.probe_llm(cfg)


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

    # Inventory of which SCRAPPER_TOOL_* env vars are actually present in the
    # environment. Names only — never values — so this is safe even for the
    # secret-bearing vars (API keys, proxy creds). This is the unambiguous
    # answer to "which env variables did the container start with", since the
    # resolved-config log below cannot distinguish an env-provided value from
    # a built-in default.
    _present_env = sorted(k for k in os.environ if k.startswith("SCRAPPER_TOOL_"))
    _logger.info(
        "http_server.env_vars_present",
        names=_present_env,
        count=len(_present_env),
    )

    # Log which agent env vars are loaded so operators can confirm config
    # without secrets appearing in logs. Secret fields show "set"/"not set".
    try:
        from scrapper_tool.agent.types import AgentConfig  # noqa: PLC0415

        _cfg = AgentConfig.from_env()
        _logger.info(
            "http_server.agent_config",
            browser=_cfg.browser,
            fingerprint=_cfg.fingerprint,
            behavior=_cfg.behavior,
            headful=_cfg.headful,
            llm=_cfg.llm,
            model=_cfg.model,
            ollama_url=_cfg.ollama_url,
            llm_api_key="set" if _cfg.llm_api_key else "not set",
            max_steps=_cfg.max_steps,
            timeout_s=_cfg.timeout_s,
            proxy="set" if _cfg.proxy else "not set",
            respect_robots=_cfg.respect_robots,
            captcha_solver=_cfg.captcha_solver,
            captcha_api_key="set" if _cfg.captcha_api_key else "not set",
            captcha_paid_fallback=_cfg.captcha_paid_fallback,
        )
    except Exception as _exc:
        _logger.warning("http_server.agent_config_unavailable", error=str(_exc))

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
    )
    return 0


__all__ = ["_build_app", "main"]

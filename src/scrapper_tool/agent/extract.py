"""E1 — extraction-after-render via Crawl4AI + local LLM.

The fast path. One LLM call per page: a stealth browser renders the
page (via the configured backend), Crawl4AI converts the rendered DOM
to clean markdown, and the LLM converts markdown to a structured
result against the supplied schema.

This is the *default* mode for "scrape any website" because:

- 1 LLM call vs 5-20 for an agent loop.
- No click hallucinations — the LLM only sees rendered text.
- Works perfectly on listing pages, articles, JSON-LD pages, product
  pages, search results.

Use :func:`scrapper_tool.agent.browse` instead when the page requires
interaction (login, multi-step nav, dynamic forms).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from scrapper_tool._logging import get_logger
from scrapper_tool.agent.backends import (
    get_behavior_policy,
    get_browser_backend,
    get_captcha_solver,
    get_fingerprint_generator,
    get_llm_backend,
)
from scrapper_tool.agent.types import ActionTrace, AgentConfig, AgentResult
from scrapper_tool.errors import (
    AgentBlockedError,
    AgentError,
    AgentTimeoutError,
)

_logger = get_logger(__name__)


_CRAWL4AI_NOT_INSTALLED = (
    "Crawl4AI extraction requires the [llm-agent] extra.\n"
    "Install with: pip install scrapper-tool[llm-agent]"
)


_DEFAULT_EXTRACT_INSTRUCTION = (
    "Extract the requested fields from the page content. Return ONLY valid JSON "
    "matching the provided schema. If a field is not present, set it to null. "
    "Do NOT invent data. Do NOT include surrounding prose."
)


async def run_extract(
    url: str,
    schema: type[BaseModel] | dict[str, object] | str,
    *,
    config: AgentConfig,
    instruction: str | None = None,
) -> AgentResult:
    """Render ``url``, run a single LLM extraction call, return result.

    Public wrapper is :func:`scrapper_tool.agent.agent_extract`. This
    function is the entry point used by the runner / sessions.
    """
    started = time.perf_counter()

    # Probe LLM up front — fail fast if Ollama is down.
    llm = get_llm_backend(config)
    await llm.probe()

    # Captcha solver and behavior policy aren't directly used by Crawl4AI
    # in this implementation (Crawl4AI doesn't expose mid-render captcha
    # hooks consistently across versions). They're constructed here to
    # validate config — captcha handling for E1 happens via Camoufox's
    # silent auto-pass when the browser backend is Camoufox, and via the
    # browse-mode path otherwise.
    _ = get_captcha_solver(config)
    _ = get_behavior_policy(config.behavior)
    _ = get_fingerprint_generator(config.fingerprint)
    _ = get_browser_backend(config.browser)  # validate name

    try:
        from crawl4ai import (  # noqa: PLC0415
            AsyncWebCrawler,
            BrowserConfig,
            CacheMode,
            CrawlerRunConfig,
            LLMConfig,
        )
        from crawl4ai.extraction_strategy import (  # noqa: PLC0415
            JsonCssExtractionStrategy,
            LLMExtractionStrategy,
        )
    except ImportError as exc:  # pragma: no cover — covered by unit mock
        raise ImportError(_CRAWL4AI_NOT_INSTALLED) from exc

    provider, api_base, api_token = llm.to_crawl4ai_provider()
    schema_payload = _normalize_schema(schema)

    extraction_strategy: Any
    if isinstance(schema, dict) and _looks_like_css_schema(schema):
        # Pure CSS schema (Crawl4AI's no-LLM mode). Faster + free.
        extraction_strategy = JsonCssExtractionStrategy(schema=cast("dict[str, Any]", schema))
    else:
        # Crawl4AI 0.6+ deprecated direct ``provider=`` / ``api_base=`` /
        # ``api_token=`` kwargs in favor of an ``llm_config=LLMConfig(...)``
        # wrapper. Pass an empty-string token if the backend doesn't expose
        # one so litellm doesn't reject the call.
        llm_config = LLMConfig(
            provider=provider,
            api_token=api_token or "no-key-needed",
            base_url=api_base,
        )
        extraction_strategy = LLMExtractionStrategy(
            llm_config=llm_config,
            schema=schema_payload,
            instruction=instruction or _DEFAULT_EXTRACT_INSTRUCTION,
            extraction_type="schema" if schema_payload else "block",
            chunk_token_threshold=4000,
            apply_chunking=True,
        )

    browser_cfg = BrowserConfig(
        headless=not config.headful,
        browser_type=_crawl4ai_browser_type(config.browser),
        proxy=config.proxy,
        verbose=False,
    )
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=extraction_strategy,
        wait_until="domcontentloaded",
        page_timeout=int(config.timeout_s * 1000),
    )

    try:

        async def _run() -> Any:
            async with AsyncWebCrawler(config=browser_cfg) as crawler:
                return await crawler.arun(url=url, config=run_cfg)

        result = await asyncio.wait_for(_run(), timeout=config.timeout_s)
    except TimeoutError as exc:
        msg = f"agent_extract timed out after {config.timeout_s}s for {url}"
        raise AgentTimeoutError(msg) from exc
    except AgentError:
        raise
    except Exception as exc:
        if _looks_like_block(exc):
            raise AgentBlockedError(f"agent_extract blocked at {url}: {exc}") from exc
        raise AgentError(f"agent_extract failed at {url}: {exc}") from exc

    duration = time.perf_counter() - started
    return _crawl4ai_result_to_agent(
        result,
        url=url,
        duration_s=duration,
        fallback_schema=schema_payload is not None,
        schema_payload=schema_payload,
    )


# --- Helpers --------------------------------------------------------------


def _normalize_schema(schema: object) -> dict[str, object] | None:
    """Convert any schema input into a JSON-Schema-shaped dict for Crawl4AI.

    - Pydantic model → `.model_json_schema()`
    - dict → returned as-is (caller's responsibility to be valid)
    - str → None (Crawl4AI's "block" extraction mode without a schema).
    """
    if isinstance(schema, str):
        return None
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return cast("dict[str, object]", schema.model_json_schema())
    if isinstance(schema, dict):
        return cast("dict[str, object]", schema)
    msg = f"Unsupported schema type: {type(schema).__name__}"
    raise TypeError(msg)


def _looks_like_css_schema(schema: dict[str, object]) -> bool:
    """Crawl4AI's CSS schema has top-level ``baseSelector`` + ``fields``."""
    return "baseSelector" in schema and "fields" in schema


def _crawl4ai_browser_type(name: str) -> str:
    """Map our backend name to Crawl4AI's ``browser_type`` argument.

    Crawl4AI ships with chromium / firefox / webkit. Camoufox-as-Firefox
    isn't first-class — when Camoufox is selected, Crawl4AI launches its
    own Firefox; the user gets *some* of Camoufox's stealth value via
    behavioral randomization in Crawl4AI itself. For full Camoufox
    semantics, route through agent_browse instead.
    """
    return {
        "camoufox": "firefox",
        "patchright": "chromium",
        "scrapling": "chromium",
        "zendriver": "chromium",
        "botasaurus": "chromium",
    }.get(name, "chromium")


def _looks_like_block(exc: Exception) -> bool:
    """Heuristic — does the exception look like an anti-bot block?"""
    text = str(exc).lower()
    return any(
        needle in text
        for needle in (
            "challenge",
            "cloudflare",
            "captcha",
            "blocked",
            "403",
            "access denied",
            "datadome",
        )
    )


def _crawl4ai_result_to_agent(
    result: Any,
    *,
    url: str,
    duration_s: float,
    fallback_schema: bool,
    schema_payload: dict[str, object] | None = None,
) -> AgentResult:
    """Convert Crawl4AI's CrawlResult → AgentResult."""
    success = bool(getattr(result, "success", True))
    final_url = str(getattr(result, "url", url) or url)
    markdown = _stringify_markdown(getattr(result, "markdown", None))
    extracted = getattr(result, "extracted_content", None)

    data: dict[str, object] | list[object] | None
    error: str | None = None
    raw_text: str | None = None

    if isinstance(extracted, (dict, list)):
        data = cast("dict[str, object] | list[object]", extracted)
    elif isinstance(extracted, str):
        # Crawl4AI sometimes returns the LLM's raw JSON string.
        try:
            import json  # noqa: PLC0415

            parsed = json.loads(extracted)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            data = cast("dict[str, object] | list[object]", parsed)
        else:
            data = {"_raw": extracted} if fallback_schema else None
            raw_text = extracted
            error = "schema-validation-failed" if fallback_schema else None
    else:
        data = None

    # Crawl4AI's LLMExtractionStrategy wraps every extraction in a list
    # (one element per chunk). When the caller's schema is an object and
    # we ended up with a single-element list of a matching dict, unwrap
    # so callers don't have to know about the wrapper. Smaller LLMs also
    # sometimes flatten the schema's outer object into a bare list, so
    # we re-wrap when we can prove the schema is an object with exactly
    # one array property.
    data = _unwrap_crawl4ai_singleton(
        data, fallback_schema=fallback_schema, schema_payload=schema_payload
    )

    blocked = False
    if not success:
        msg = (getattr(result, "error_message", "") or "").lower()
        blocked = any(needle in msg for needle in ("block", "cloudflare", "challenge", "captcha"))
        error = error or msg or "crawl4ai-failure"

    trace = ActionTrace(
        step=1,
        action="extract",
        target=url,
        screenshot_idx=None,
        dom_snippet=(raw_text or markdown or "")[:1024] or None,
        latency_ms=int(duration_s * 1000),
    )

    return AgentResult(
        mode="extract",
        data=data,
        final_url=final_url,
        rendered_markdown=markdown,
        screenshots=None,
        actions=[trace],
        tokens_used=0,
        blocked=blocked,
        error=error,
        duration_s=duration_s,
        steps_used=1,
    )


def _unwrap_crawl4ai_singleton(
    data: dict[str, object] | list[object] | None,
    *,
    fallback_schema: bool,
    schema_payload: dict[str, object] | None = None,
) -> dict[str, object] | list[object] | None:
    """Normalize Crawl4AI's chunked output to match the caller's schema shape.

    Crawl4AI's ``LLMExtractionStrategy`` returns a list with one item
    per chunk. We handle two common shapes:

    1. ``[{...schema-shaped object...}]`` — single chunk, dict inside.
       Unwrap to the inner dict.
    2. ``[item1, item2, ...]`` (multi-element list) when the caller's
       schema is ``{type: object, properties: {<single-array-prop>: ...}}``.
       Smaller LLMs (qwen3-vl-8b at 4B-class quant, e.g.) sometimes flatten
       the outer object away and return the array's items directly. Detect
       this and re-wrap into ``{<single-array-prop>: [...]}``.

    Pure list schemas (caller asked for an array as the top-level type)
    and ambiguous multi-chunk results are left as-is.
    """
    if not fallback_schema:
        return data
    if not isinstance(data, list):
        return data

    # Case 1: singleton list of a dict — unwrap the chunk wrapper.
    if len(data) == 1 and isinstance(data[0], dict):
        return cast("dict[str, object]", data[0])

    # Case 2: schema asks for an object with one array property; LLM
    # returned a flat list of items. Re-wrap.
    if (
        isinstance(schema_payload, dict)
        and schema_payload.get("type") == "object"
        and isinstance(schema_payload.get("properties"), dict)
    ):
        props = cast("dict[str, object]", schema_payload["properties"])
        array_props = [
            k for k, v in props.items() if isinstance(v, dict) and v.get("type") == "array"
        ]
        if len(array_props) == 1:
            return {array_props[0]: data}

    return data


def _stringify_markdown(md: object) -> str | None:
    if md is None:
        return None
    if isinstance(md, str):
        return md
    # Crawl4AI's markdown may be a complex object with .raw_markdown / .fit_markdown.
    for attr in ("fit_markdown", "raw_markdown", "markdown"):
        val = getattr(md, attr, None)
        if isinstance(val, str):
            return val
    return str(md) if md else None


def validate_against_pydantic(
    data: object,
    schema: type[BaseModel],
) -> tuple[BaseModel | None, str | None]:
    """Optionally re-validate Crawl4AI output against a pydantic model.

    Returns ``(model, None)`` on success or ``(None, error_message)`` on
    validation failure. Used by :func:`scrapper_tool.agent.runner` when
    the caller passes a pydantic class as the schema.
    """
    try:
        return schema.model_validate(data), None
    except ValidationError as exc:
        return None, str(exc)


__all__ = [
    "run_extract",
    "validate_against_pydantic",
]

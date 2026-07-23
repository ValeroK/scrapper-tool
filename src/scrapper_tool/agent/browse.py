"""E2 — interactive multi-step agent via browser-use + local LLM.

Use only for tasks that require interaction: login, multi-step
navigation, "click load more" pagination, dynamic forms, conditional UI.
For "just give me the data from this page", use
:func:`scrapper_tool.agent.extract.run_extract` instead — it's faster
and far more reliable.

The agent loop is owned by ``browser-use`` (Apache-2.0, ~91k★, native
Ollama support). We feed it the configured stealth browser (Camoufox by
default), the configured local LLM (Qwen3-VL-8B by default), and the
caller's natural-language ``instruction``. browser-use returns an
``AgentHistoryList`` which we convert to a uniform :class:`AgentResult`.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from typing import Any, cast

from pydantic import BaseModel, ValidationError

from scrapper_tool._challenge import looks_like_block_message
from scrapper_tool._logging import get_logger
from scrapper_tool.agent.backends import (
    BrowserHandle,
    BrowserLaunchOptions,
    get_behavior_policy,
    get_browser_backend,
    get_captcha_solver,
    get_fingerprint_generator,
    get_llm_backend,
    is_vision_model,
    make_on_step_end,
)
from scrapper_tool.agent.backends.behavior import make_behavior_consumer
from scrapper_tool.agent.backends.captcha_dom import make_captcha_consumer
from scrapper_tool.agent.types import ActionTrace, AgentConfig, AgentResult
from scrapper_tool.errors import (
    AgentBlockedError,
    AgentError,
    AgentTimeoutError,
)

_logger = get_logger(__name__)


_BROWSER_USE_NOT_INSTALLED = (
    "browser-use is required for agent_browse. Install the [llm-agent] extra:\n"
    "    pip install scrapper-tool[llm-agent]"
)

_MAX_SCREENSHOTS = 3
_TARGET_SCREENSHOT_WIDTH = 1024


async def run_browse(
    url: str,
    instruction: str,
    *,
    config: AgentConfig,
    schema: type[BaseModel] | dict[str, object] | None = None,
) -> AgentResult:
    """Run a multi-step browser-use agent loop, return :class:`AgentResult`.

    Public wrapper is :func:`scrapper_tool.agent.agent_browse`.
    """
    started = time.perf_counter()

    # Probe LLM up front.
    llm = get_llm_backend(config)
    await llm.probe()

    backend = get_browser_backend(config.browser, cdp_url=config.obscura_cdp_url)
    fingerprint = get_fingerprint_generator(config.fingerprint)
    behavior = get_behavior_policy(config.behavior)
    solver = get_captcha_solver(config)

    # v1.6.0: hand the backend the full launch-options object. This is what
    # finally threads ``user_data_dir`` (cf_clearance carry-forward) and the
    # render/stealth knobs (virtual display, image blocking) into Camoufox —
    # previously they never reached it.
    launch_options = BrowserLaunchOptions(
        headful=config.headful,
        proxy=config.proxy,
        user_data_dir=config.user_data_dir,
        headless_mode=config.camoufox_headless_mode,
        block_images=config.block_images,
        fingerprint_preset=config.fingerprint_preset,
        os=config.camoufox_os,
        locale=config.camoufox_locale,
    )
    handle = await backend.launch(
        options=launch_options,
        fingerprint=fingerprint,
        behavior=behavior,
    )
    try:
        result = await _run_with_handle(
            handle,
            url=url,
            instruction=instruction,
            schema=schema,
            config=config,
            llm_chat=llm.to_browser_use_llm(),
            solver=solver,
            behavior=behavior,
            started=started,
        )
    finally:
        await handle.close()
    return result


async def _run_with_handle(
    handle: BrowserHandle,
    *,
    url: str,
    instruction: str,
    schema: type[BaseModel] | dict[str, object] | None,
    config: AgentConfig,
    llm_chat: Any,
    solver: Any,
    behavior: Any,
    started: float,
) -> AgentResult:
    if handle.playwright_browser is None:
        msg = (
            f"browser backend {handle.name!r} does not expose a Playwright Browser; "
            "agent_browse requires a Playwright-drivable backend (camoufox / patchright / obscura)."
        )
        raise AgentError(msg)

    try:
        from browser_use import Agent  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — covered by unit mock
        raise ImportError(_BROWSER_USE_NOT_INSTALLED) from exc

    # Compose the navigation instruction so the agent always starts from `url`.
    full_task = f"Navigate to {url}. {instruction}"
    if schema is not None:
        full_task += (
            "\n\nWhen finished, return ONLY a JSON object matching this schema "
            "(no surrounding prose):\n" + _schema_for_prompt(schema)
        )

    use_vision = is_vision_model(config.model)

    # Hand browser-use the LIVE browser from our stealth backend so it drives
    # THAT browser instead of launching its own default Chromium. browser-use
    # 0.5.x ignores the old ``playwright_browser`` attribute injection (it builds
    # a BrowserSession from an existing context/page), so we pass the running
    # context + page directly. This is what makes the stealth backend
    # (Camoufox / Patchright / Obscura) actually get used in E2.
    bu_browser = handle.playwright_browser
    context = bu_browser.contexts[0] if bu_browser.contexts else await bu_browser.new_context()
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        agent: Any = Agent(
            task=full_task,
            llm=llm_chat,
            browser_context=context,
            page=page,
            use_vision=use_vision,
            max_actions_per_step=4,
        )
    except TypeError:  # pragma: no cover — defensive against API drift
        # Older browser-use signatures may take fewer kwargs.
        agent = Agent(task=full_task, llm=llm_chat, browser_context=context, page=page)

    # Captcha + behavior ride an on_step_end hook: after every agent step
    # the live page is checked for a challenge (mechanism-aware solve) and
    # behavior shaping is applied. Consumer errors are swallowed inside the
    # hook so they never abort the loop.
    on_step_end = make_on_step_end(
        make_captcha_consumer(solver),
        make_behavior_consumer(behavior, full=True),
    )

    try:
        try:
            run_coro = agent.run(max_steps=config.max_steps, on_step_end=on_step_end)
        except TypeError:  # pragma: no cover — older browser-use lacks on_step_end
            run_coro = agent.run(max_steps=config.max_steps)
        history = await asyncio.wait_for(run_coro, timeout=config.timeout_s)
    except TimeoutError as exc:
        msg = f"agent_browse timed out after {config.timeout_s}s for {url}"
        raise AgentTimeoutError(msg) from exc
    except AgentError:
        raise
    except Exception as exc:
        if _looks_like_block(exc):
            raise AgentBlockedError(f"agent_browse blocked at {url}: {exc}") from exc
        raise AgentError(f"agent_browse failed at {url}: {exc}") from exc
    # NB: the browser is the backend's own (passed to browser-use as an existing
    # context), so its lifecycle is owned by ``handle.close()`` in run_browse —
    # we must NOT close it here or we'd double-close.

    duration = time.perf_counter() - started
    return _history_to_agent_result(
        history,
        url=url,
        duration_s=duration,
        schema=schema,
    )


def _schema_for_prompt(schema: type[BaseModel] | dict[str, object]) -> str:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return json.dumps(schema.model_json_schema(), indent=2)
    return json.dumps(schema, indent=2)


def _looks_like_block(exc: Exception) -> bool:
    """Does this exception look like an anti-bot block rather than a bug?

    Shared with E1 via :func:`scrapper_tool._challenge.looks_like_block_message`
    — the two tiers had byte-identical copies of this list, which is exactly the
    kind of duplication that drifts the moment one of them learns a new vendor.
    """
    return looks_like_block_message(str(exc))


# --- History → AgentResult conversion ------------------------------------


def _history_to_agent_result(
    history: Any,
    *,
    url: str,
    duration_s: float,
    schema: type[BaseModel] | dict[str, object] | None,
) -> AgentResult:
    """Convert browser-use's AgentHistoryList → AgentResult.

    Defensive against API drift: pulls fields via ``getattr`` with sane
    fallbacks so a minor browser-use bump doesn't silently regress.
    """
    history_items = list(getattr(history, "history", history) or [])
    actions: list[ActionTrace] = []
    raw_screenshots: list[bytes] = []

    for idx, item in enumerate(history_items, start=1):
        action = _action_label(item)
        target = _action_target(item)
        snippet = _action_snippet(item)
        screenshot_b = _extract_screenshot_bytes(item)
        screenshot_idx: int | None = None
        if screenshot_b is not None and len(raw_screenshots) < _MAX_SCREENSHOTS:
            raw_screenshots.append(screenshot_b)
            screenshot_idx = len(raw_screenshots) - 1
        actions.append(
            ActionTrace(
                step=idx,
                action=action,
                target=target,
                screenshot_idx=screenshot_idx,
                dom_snippet=snippet,
                latency_ms=0,
            )
        )

    final_result = _final_result(history)
    final_url = _final_url(history) or url
    blocked = bool(getattr(history, "blocked", False)) or _detect_block(history_items)

    data, error = _coerce_final(final_result, schema=schema)
    if not data and not error:
        error = "no-match"

    screenshots = _downsample_screenshots(raw_screenshots) or None

    return AgentResult(
        mode="browse",
        data=data,
        final_url=final_url,
        rendered_markdown=None,
        screenshots=screenshots,
        actions=actions,
        tokens_used=int(getattr(history, "total_input_tokens", 0) or 0),
        blocked=blocked,
        error=error,
        duration_s=duration_s,
        steps_used=len(actions),
    )


def _action_label(item: Any) -> str:
    for attr in ("model_action", "action", "type"):
        v = getattr(item, attr, None)
        if v:
            return str(v)
    return "step"


def _action_target(item: Any) -> str | None:
    for attr in ("selector", "target", "url"):
        v = getattr(item, attr, None)
        if v:
            return str(v)
    return None


def _action_snippet(item: Any) -> str | None:
    for attr in ("dom", "extracted_content", "result_text"):
        v = getattr(item, attr, None)
        if isinstance(v, str) and v:
            return v[:1024]
    return None


def _extract_screenshot_bytes(item: Any) -> bytes | None:
    raw = getattr(item, "screenshot", None) or getattr(item, "screenshot_bytes", None)
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str) and raw:
        # base64 PNG → bytes
        import base64  # noqa: PLC0415

        try:
            return base64.b64decode(raw, validate=False)
        except (ValueError, TypeError):
            return None
    return None


def _final_result(history: Any) -> object:
    fn = getattr(history, "final_result", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            return None
    return getattr(history, "result", None)


def _final_url(history: Any) -> str | None:
    for attr in ("url", "final_url"):
        v = getattr(history, attr, None)
        if isinstance(v, str):
            return v
    items = list(getattr(history, "history", []) or [])
    if items:
        v = getattr(items[-1], "url", None)
        if isinstance(v, str):
            return v
    return None


def _detect_block(items: list[Any]) -> bool:
    for item in items:
        for attr in ("error", "result_text", "extracted_content"):
            v = getattr(item, attr, None)
            if isinstance(v, str) and any(
                needle in v.lower()
                for needle in ("blocked", "captcha", "cloudflare", "access denied")
            ):
                return True
    return False


def _coerce_final(
    final: object,
    *,
    schema: type[BaseModel] | dict[str, object] | None,
) -> tuple[dict[str, object] | list[object] | None, str | None]:
    """Best-effort normalize browser-use's final result into JSON.

    Returns ``(data, error)``. Validation failures populate
    ``error="schema-validation-failed"`` and stash the raw under
    ``data["_raw"]``.
    """
    if final is None:
        return None, None

    parsed: object = final
    if isinstance(final, str):
        try:
            parsed = json.loads(final)
        except (json.JSONDecodeError, ValueError):
            parsed = final  # leave as-is

    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            model = schema.model_validate(parsed)
        except ValidationError as exc:
            return ({"_raw": str(parsed)}, f"schema-validation-failed: {exc}")
        return cast("dict[str, object]", model.model_dump(mode="json")), None

    if isinstance(parsed, dict):
        return cast("dict[str, object]", parsed), None
    if isinstance(parsed, list):
        return cast("list[object]", parsed), None
    return ({"_raw": str(parsed)}, None)


def _downsample_screenshots(raw: list[bytes]) -> list[bytes]:
    """Downscale PNGs to a target width to bound MCP / context payload size."""
    if not raw:
        return []
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:  # pragma: no cover — Pillow is in [llm-agent]
        return raw[:_MAX_SCREENSHOTS]

    out: list[bytes] = []
    for png in raw[:_MAX_SCREENSHOTS]:
        try:
            with Image.open(io.BytesIO(png)) as opened:
                target: Any = opened
                if opened.width > _TARGET_SCREENSHOT_WIDTH:
                    ratio = _TARGET_SCREENSHOT_WIDTH / opened.width
                    new_size = (_TARGET_SCREENSHOT_WIDTH, int(opened.height * ratio))
                    target = opened.resize(new_size)
                buf = io.BytesIO()
                target.save(buf, format="PNG", optimize=True)
                out.append(buf.getvalue())
        except Exception:
            out.append(png)
    return out


__all__ = [
    "run_browse",
]

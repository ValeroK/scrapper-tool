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
    get_vision_backend,
    make_on_step_end,
    supports_vision,
)
from scrapper_tool.agent.backends.behavior import make_behavior_consumer
from scrapper_tool.agent.backends.captcha_dom import make_captcha_consumer
from scrapper_tool.agent.types import ActionTrace, AgentConfig, AgentResult
from scrapper_tool.errors import (
    AgentBlockedError,
    AgentError,
    AgentTimeoutError,
    ConfigurationError,
)

_logger = get_logger(__name__)


_BROWSER_USE_NOT_INSTALLED = (
    "browser-use is required for agent_browse. Install the [llm-agent] extra:\n"
    "    pip install scrapper-tool[llm-agent]"
)

_NO_CDP_FOR_E2 = """E2 (agent_browse) cannot drive the {backend!r} backend.

browser-use 0.13 attaches to a browser over CDP only — it dropped the Playwright
context/page handoff earlier versions used. Camoufox is a Firefox fork, and
Firefox removed CDP in favour of WebDriver BiDi, so there is no endpoint for
browser-use to attach to.

This raises instead of falling back because browser-use SILENTLY ignores unknown
kwargs: proceeding would launch a plain unpatched Chromium and run the
interactive agent with no stealth at all, with nothing in the logs to say so.

For interactive flows (login / pagination / dynamic forms) pick a CDP-capable
backend:
    browser='patchright'   # Chromium with stealth patches, launched locally
    browser='obscura'      # Chromium over an external CDP server

Camoufox stays the default for the render tier and E1, where it has the highest
measured bypass rate and needs no CDP."""

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
            llm_backend=llm,
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
    llm_backend: Any,
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

    if not handle.cdp_url:
        # browser-use 0.13 attaches over CDP only, and it does so *silently*: the
        # old `browser_context=` / `page=` kwargs are now ignored rather than
        # rejected, so passing them builds a fresh unpatched Chromium and E2 runs
        # with no stealth at all. That is the precise bug the v1.6.0 A1 work
        # fixed, so failing here is deliberate — a silent stealth downgrade is
        # worse than an error the operator can act on.
        raise ConfigurationError(_NO_CDP_FOR_E2.format(backend=handle.name))

    try:
        from browser_use import Agent  # noqa: PLC0415
        from browser_use.browser.session import BrowserSession  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover — covered by unit mock
        raise ImportError(_BROWSER_USE_NOT_INSTALLED) from exc

    # Compose the navigation instruction so the agent always starts from `url`.
    full_task = f"Navigate to {url}. {instruction}"
    if schema is not None:
        full_task += (
            "\n\nWhen finished, return ONLY a JSON object matching this schema "
            "(no surrounding prose):\n" + _schema_for_prompt(schema)
        )

    # Ask the server what the model is rather than pattern-matching its name.
    # The name heuristic reported False for every locally installed VLM, so vision
    # was silently disabled on models that could see and E2 ran blind. Falls back
    # to the heuristic when the server has no such endpoint.
    use_vision = await supports_vision(config.model, config.ollama_url)

    # Inject the caller's session into the LIVE context, before browser-use
    # attaches and before the agent navigates.
    #
    # Not `storage_state`: that is a *launch* argument, and by this point the
    # browser and its context already exist — our stealth backend launched them
    # and browser-use is about to attach over CDP to that same instance. A
    # storage_state passed to BrowserSession would at best seed a fresh context
    # that nothing then drives. Setting cookies on the resolved context is the
    # same mechanism the render tier uses, and it lands on the context the agent
    # actually gets.
    await _inject_cookies(handle.playwright_browser, config, url)

    # Attach browser-use to the LIVE browser our stealth backend launched, so it
    # drives THAT one instead of starting its own default Chromium.
    #
    # The mechanism changed in browser-use 0.13: it no longer accepts a
    # Playwright context/page and talks CDP directly. Passing `cdp_url` is what
    # makes it treat the browser as external (it derives `is_local=False` from
    # that), and `keep_alive=True` stops it tearing the browser down on finish —
    # the lifecycle belongs to `handle.close()`, and letting both close it
    # double-closes.
    session = BrowserSession(cdp_url=handle.cdp_url, keep_alive=True)

    agent: Any = Agent(
        task=full_task,
        llm=llm_chat,
        browser_session=session,
        use_vision=use_vision,
        max_actions_per_step=4,
    )

    # Captcha + behavior ride an on_step_end hook: after every agent step
    # the live page is checked for a challenge (mechanism-aware solve) and
    # behavior shaping is applied. Consumer errors are swallowed inside the
    # hook so they never abort the loop.
    # The same `use_vision` verdict gates the grid solver: handing it a text-only
    # model would burn a round on a reply that cannot be about the image.
    # A solve is the most expensive thing this tier does; collect the clearance
    # it wins while the browser is still open. `handle.close()` in run_browse
    # tears the context down, taking the credential with it.
    won_cookies: list[dict[str, Any]] = []
    # The captcha tier may want a DIFFERENT model from the one driving the
    # agent: grids need spatial vision, the agent loop needs instruction
    # following, and the best model for one is measurably bad at the other.
    vision_backend = await get_vision_backend(config)
    on_step_end = make_on_step_end(
        make_captcha_consumer(
            solver,
            vision=vision_backend,
            on_solved=won_cookies.extend,
        ),
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
        cookies=won_cookies,
    )


async def _inject_cookies(pw_browser: Any, config: AgentConfig, url: str) -> None:
    """Set the caller's cookies on the live context E2 is about to drive.

    Mirrors the render tier (``patterns.render.render_html``) exactly, and for
    the same reasons: resolve the context through
    :func:`~scrapper_tool.agent.backends.browser.resolve_context` because
    Camoufox in persistent mode hands back a ``BrowserContext`` rather than a
    ``Browser``, and narrow the jar with ``cookies_for_url`` so nothing is sent
    to a host it isn't scoped to.

    Exceptions propagate. ``run_browse`` already classifies and reports tier
    failures, and a silently missing session would surface as the agent
    cheerfully reporting it could not find the logged-in page.
    """
    if not config.cookies:
        return

    from scrapper_tool.agent.backends.browser import resolve_context  # noqa: PLC0415
    from scrapper_tool.cookies import cookies_for_url, redact, to_playwright  # noqa: PLC0415

    applicable = cookies_for_url(config.cookies, url)
    if not applicable:
        return

    context = await resolve_context(pw_browser)
    await context.add_cookies(to_playwright(applicable))
    _logger.debug("agent.browse.cookies_applied", url=url, cookies=redact(applicable))


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
    cookies: list[dict[str, Any]] | None = None,
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
    blocked = _detect_block(history)

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
        tokens_used=_tokens_used(history),
        blocked=blocked,
        error=error,
        duration_s=duration_s,
        steps_used=len(actions),
        cookies=list(cookies or []),
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


def _step_errors(history: Any) -> list[str]:
    """Every step error browser-use recorded, as strings.

    ``AgentHistoryList.errors()`` is the authoritative source and returns one
    entry per step (``None`` where the step was fine). The manual walk below is
    the fallback for history shapes that predate it, and reads ``.error`` only —
    never a result's content.
    """
    fn = getattr(history, "errors", None)
    if callable(fn):
        try:
            return [str(e) for e in fn() if e]
        except Exception as exc:
            _logger.debug("agent.browse.errors_unreadable", error=str(exc)[:160])
    out: list[str] = []
    for item in list(getattr(history, "history", []) or []):
        for result in list(getattr(item, "result", []) or []):
            err = getattr(result, "error", None)
            if err:
                out.append(str(err))
        err = getattr(item, "error", None)
        if err:
            out.append(str(err))
    return out


def _detect_block(history: Any) -> bool:
    """Whether E2 was walled — judged on step ERRORS, never on page content.

    This used to substring-scan ``extracted_content`` and ``result_text`` for
    "blocked" / "captcha" / "cloudflare" / "access denied" — that is, the text the
    agent had just *successfully extracted*. Any page carrying a standard
    reCAPTCHA footer notice ("This site is protected by reCAPTCHA...") matched on
    ``captcha``, and any Cloudflare-fronted site with a "Performance & security
    by Cloudflare" footer matched on ``cloudflare``. Both are ordinary furniture
    on perfectly good pages.

    The consequence was silent data corruption in the worst direction: a
    successful extraction carrying correct data was returned with
    ``blocked=True``. A consumer that honours the flag threw good data away, and
    one that ignores it would parse a real challenge page. It cost a downstream
    integration a two-day investigation into an anti-bot wall that did not exist.

    Compounding it, the other half of the old expression —
    ``getattr(history, "blocked", False)`` — read an attribute browser-use has
    never had, so it was permanently ``False`` and the content scan was the
    *only* signal. ``errors()`` was there the whole time and was never consulted.

    A navigation error is the right place to look precisely because it frequently
    carries the wall's own hostname (``validate.perfdrive.com``,
    ``geo.captcha-delivery.com``), which is what
    :func:`~scrapper_tool._challenge.looks_like_block_message` matches on — the
    same detector E1 already uses.
    """
    return any(looks_like_block_message(msg) for msg in _step_errors(history))


def _tokens_used(history: Any) -> int:
    """Total tokens the agent spent, or 0 when the run genuinely reported none.

    Read from ``history.usage.total_tokens``. The previous
    ``getattr(history, "total_input_tokens", 0)`` named an attribute that does
    not exist on browser-use 0.13's ``AgentHistoryList``, so the default fired
    every time and **every E2 run reported 0 tokens regardless of model**. That
    was read downstream as evidence that local inference is unmetered, when the
    field was simply never populated — which matters the moment E2 is
    cost-budgeted.
    """
    usage = getattr(history, "usage", None)
    total = getattr(usage, "total_tokens", None)
    if isinstance(total, int):
        return total
    for attr in ("total_tokens", "total_input_tokens"):
        value = getattr(history, attr, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                value = None
        if isinstance(value, int):
            return value
    return 0


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

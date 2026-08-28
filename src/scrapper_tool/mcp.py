"""MCP server — exposes scrapper-tool helpers as LLM-agent tools.

Available via ``pip install scrapper-tool[agent]`` and the
``scrapper-tool-mcp`` console script. Compatible with any
Model-Context-Protocol consumer:

- **Claude Desktop / Claude Code** — wire via ``.mcp.json``::

    {
      "mcpServers": {
        "scrapper-tool": {
          "command": "scrapper-tool-mcp",
          "args": [],
          "env": {}
        }
      }
    }

- **Anthropic Python SDK + ``mcp-use``** — register the stdio server
  as a toolset and pass to ``client.messages.create(..., tools=...)``.
- **OpenClaw / Hermes Agent / AutoGen / LangChain** — see
  ``docs/agent-integration.md`` for per-framework wiring.

Tools exposed
-------------

- ``auto_scrape(url, schema_json, *, instruction, model, browser, timeout_s)`` —
  PRIMARY tool (NEW v1.1.0+; cascade fixed v1.1.3 to actually invoke
  Pattern D). Auto-escalates Pattern A/B/C → D → E1 → E2 in a single
  call and returns ``pattern_used`` so the agent can see what worked.
  Pattern D (Scrapling) is invoked when the ``[hostile]`` extra is
  installed; skipped otherwise (cascade falls through to E1, response
  carries ``hostile_skipped=true``). Use this instead of
  fetch_with_ladder + agent_extract when you just want data and don't
  care which pattern produced it.
- ``fetch_with_ladder(url, *, method, use_curl_cffi, extract_structured)`` —
  Issue an HTTP request through the impersonation ladder; returns
  status, body truncated to 64 KB, and the winning profile name. With
  ``extract_structured=True`` (NEW v1.1.0+) also runs Pattern B + C and
  includes ``product`` and ``microdata_price`` fields — eliminates the
  common two-tool pattern (fetch then extract_product).
- ``extract_product(html, *, base_url)`` — parse a schema.org
  Product+Offer block from HTML (Pattern B); returns a normalised
  ``ProductOffer`` dict or ``null``.
- ``extract_microdata_price(html)`` — parse ``<meta itemprop="price">``
  + ``priceCurrency`` schema.org microdata (Pattern C); returns
  ``{price, currency}`` or ``null``.
- ``canary(url, *, profiles)`` — walk the impersonation ladder and
  report which profile won; returns the same JSON shape as the CLI's
  ``--json`` mode.
- ``agent_extract(url, schema_json, *, instruction, model, timeout_s, headful)`` —
  Pattern E1 (v1.0.0+): render with a stealth browser (Camoufox by
  default) and run a single local-LLM call to extract structured JSON.
  Requires the ``[llm-agent]`` extra and a running local LLM (Ollama).
- ``agent_browse(url, instruction, *, model, max_steps, timeout_s, headful)`` —
  Pattern E2 (v1.0.0+): multi-step LLM-driven browser-use agent for
  interactive tasks (login, multi-step nav, dynamic forms). Same extras
  required as ``agent_extract``.

Security
--------

The MCP server runs in the agent's trust boundary. The
``fetch_with_ladder`` tool can fetch arbitrary URLs — the consuming
agent (Claude, OpenClaw, etc.) is responsible for confirming with the
end user before fetching user-data-bearing URLs. This server does NOT
itself prompt for confirmation; it's the agent's permission model that
gates the call. See ``docs/agent-integration.md § Security``.

Body truncation: we cap response bodies returned to the agent at 64 KB
so a single fetch can't exhaust the agent's context window.
"""

from __future__ import annotations

import base64
import os
import sys
from typing import TYPE_CHECKING, Any

from scrapper_tool import __version__, _extras
from scrapper_tool._challenge import is_interstitial
from scrapper_tool._classify import classify_extraction_success
from scrapper_tool.canary import run_canary
from scrapper_tool.errors import (
    AgentBlockedError,
    AgentError,
    AgentLLMError,
    BlockedError,
    VendorHTTPError,
)
from scrapper_tool.http import request_with_retry, vendor_client
from scrapper_tool.ladder import IMPERSONATE_LADDER, request_with_ladder
from scrapper_tool.patterns.b import extract_product_offer
from scrapper_tool.patterns.c import extract_microdata_price as _extract_microdata_price

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_BODY_TRUNCATION_BYTES = 64 * 1024
_MAX_AGENT_SCREENSHOTS = 3
_MAX_DOM_SNIPPET_STEPS = 5
_AGENT_NOT_INSTALLED = (
    "scrapper-tool[llm-agent] extra not installed. "
    "Install with: pip install scrapper-tool[llm-agent]"
)


def _is_e_tier_structured(data: object | None, blocked: bool) -> bool:
    """Verdict for an E1/E2 result — True iff ``data`` is structured JSON.

    Mirrors :func:`scrapper_tool.http_server._is_e_tier_structured`. Reimplemented
    here because the http_server module pulls in FastAPI; the MCP module must
    stay importable without the ``[http]`` extra.
    """
    if blocked or data is None:
        return False
    return not (isinstance(data, dict) and "_raw" in data)


def _truncate(text: str, limit: int = _BODY_TRUNCATION_BYTES) -> tuple[str, bool]:
    """Cap text to ``limit`` bytes; report whether truncation occurred."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="replace"), True


def _structured_product(html: str, base_url: str | None) -> dict[str, Any] | None:
    """Pattern B helper — return ProductOffer dict or None."""
    product = extract_product_offer(html, base_url=base_url)
    return product.model_dump(mode="json") if product is not None else None


def _structured_price(html: str) -> dict[str, Any] | None:
    """Pattern C helper — return ``{price, currency}`` or None."""
    result = _extract_microdata_price(html)
    if result is None:
        return None
    price, currency = result
    return {"price": str(price), "currency": currency}


def _structured_json_ld(html: str, base_url: str | None) -> list[Any] | None:
    """Return raw JSON-LD blocks (for the shared classifier's signal check).

    Mirrors the ``json_ld`` component of the REST ``_extract_b_c`` pipeline so
    ``auto_scrape`` feeds the same signals to ``classify_extraction_success``.
    """
    from scrapper_tool._extractors import get as get_extractor  # noqa: PLC0415

    result = get_extractor("json_ld_product").extract(html, base_url=base_url)
    if result.has_signal and isinstance(result.data, dict):
        return result.data.get("json_ld")
    return None


def _agent_error_payload(
    message: str,
    *,
    blocked: bool = False,
    original: str | None = None,
) -> dict[str, Any]:
    """Uniform error envelope returned to MCP clients on agent failure."""
    payload: dict[str, Any] = {
        "blocked": blocked,
        "data": None,
        "error": message,
        "final_url": None,
        "screenshots": None,
        "actions": [],
        "rendered_markdown": None,
        "duration_s": 0.0,
        "steps_used": 0,
    }
    if original is not None:
        payload["error_detail"] = original
    return payload


async def _continue_to_e_tier(  # noqa: PLR0915 — linear cascade; splitting hides the order
    url: str,
    schema_json: dict[str, Any] | None,
    instruction: str | None,
    model: str | None,
    browser: str | None,
    timeout_s: float,
    attempts: list[str],
    last_error: str | None,
    hostile_skipped: bool,
    user_data_dir: str | None = None,
    interactive: bool = False,
) -> dict[str, Any]:
    """E1 → E2 escalation for the MCP ``auto_scrape`` tool.

    Shared between the normal cascade (after A/B/C+D fall through) and the
    hostile_only=True path (after D fails with hostile_fallback=True).

    ``user_data_dir`` (v1.3.0+): forwarded to ``AgentConfig.merged`` so
    Crawl4AI / browser-use launch against the cascade-shared profile and
    inherit D's CF clearance.

    ``interactive`` (v1.6.0+): REST parity. E2 is the priciest tier by a wide
    margin and only earns its cost on login / pagination / dynamic-form flows;
    on anything else a blocked E1 stops here rather than spending an agent loop
    to hit the same wall more slowly.
    """
    try:
        from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
        from scrapper_tool.agent import agent_browse as _agent_browse  # noqa: PLC0415
        from scrapper_tool.agent import agent_extract as _agent_extract  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        return {
            "pattern_used": None,
            "pattern_attempts": attempts,
            "url": url,
            "blocked": True,
            "error": _AGENT_NOT_INSTALLED,
            "error_detail": str(exc),
            "data": None,
            "product": None,
            "rendered_markdown": None,
            "hostile_skipped": hostile_skipped,
            "is_structured": False,
        }

    cfg = AgentConfig.from_env()
    overrides: dict[str, Any] = {"timeout_s": timeout_s}
    if model:
        overrides["model"] = model
    if browser:
        overrides["browser"] = browser
    if user_data_dir:
        overrides["user_data_dir"] = user_data_dir

    # ----- Pattern E1 -----
    attempts.append("e1")
    schema_for_e1 = schema_json or {"type": "object", "additionalProperties": True}
    blocked_e1: Any = None
    try:
        result = await _agent_extract(
            url, schema_for_e1, instruction=instruction, config=cfg, **overrides
        )
        if not result.blocked:
            payload = _agent_result_payload(result)
            payload["pattern_used"] = "e1"
            payload["pattern_attempts"] = attempts
            payload["product"] = None
            payload["hostile_skipped"] = hostile_skipped
            payload["is_structured"] = _is_e_tier_structured(result.data, result.blocked)
            return payload
        last_error = f"e1: {result.error or 'blocked'}"
        blocked_e1 = result
    except AgentBlockedError as exc:
        last_error = f"e1: {exc}"
    except AgentLLMError:
        # E2 drives the same LLM backend — escalating fails identically and
        # slower. A deployment fault, not a property of the target. See the
        # matching carve-out in http_server._do_scrape_e_tier.
        raise
    except AgentError as exc:
        # A tier that could not deliver hands off to the next rung. But E2 is
        # the only rung left, and the gate below closes it when the caller did
        # not ask for interaction — with nothing beneath us, surface the real
        # failure rather than dressing a dead host up as a block.
        if not interactive:
            raise
        last_error = f"e1: {exc}"

    # ----- E2 gate: interactive tasks only -----
    if not interactive:
        if blocked_e1 is not None:
            # E1's blocked result carries the partial content and error detail —
            # strictly more useful to hand back than a bare "blocked".
            payload = _agent_result_payload(blocked_e1)
            payload["pattern_used"] = "e1"
            payload["pattern_attempts"] = attempts
            payload["product"] = None
            payload["hostile_skipped"] = hostile_skipped
            payload["is_structured"] = False
            return payload
        return _agent_error_payload(
            f"Blocked at E1: {last_error}. Set interactive=true to escalate to "
            "the E2 agent (login / pagination / dynamic forms).",
            blocked=True,
        ) | {
            "pattern_used": None,
            "pattern_attempts": attempts,
            "product": None,
            "hostile_skipped": hostile_skipped,
            "is_structured": False,
        }

    # ----- Pattern E2 -----
    attempts.append("e2")
    e2_instruction = instruction or (
        f"Extract structured data matching: {schema_json}"
        if schema_json
        else "Extract the main content of this page"
    )
    try:
        result = await _agent_browse(
            url, e2_instruction, schema=schema_json, config=cfg, **overrides
        )
        payload = _agent_result_payload(result)
        payload["pattern_used"] = "e2"
        payload["pattern_attempts"] = attempts
        payload["product"] = None
        payload["hostile_skipped"] = hostile_skipped
        payload["is_structured"] = _is_e_tier_structured(result.data, result.blocked)
        return payload
    except AgentBlockedError as exc:
        return _agent_error_payload(
            f"All patterns blocked: {', '.join(attempts)}. Last: {last_error or exc}",
            blocked=True,
        ) | {
            "pattern_used": None,
            "pattern_attempts": attempts,
            "product": None,
            "hostile_skipped": hostile_skipped,
            "is_structured": False,
        }


def _hostile_available_for_mcp() -> bool:
    """Probe whether [hostile] (Scrapling) is installed.

    Delegates to :func:`scrapper_tool._extras.hostile_available`. This used to
    be a local reimplementation so MCP wouldn't pull in FastAPI just to check
    an extra; ``_extras`` is stdlib-only at import time, so the copy is gone
    and the name survives only as this module's monkeypatch seam.
    """
    return _extras.hostile_available()


async def _auto_scrape_inner(
    *,
    url: str,
    schema_json: dict[str, Any] | None,
    instruction: str | None,
    model: str | None,
    browser: str | None,
    timeout_s: float,
    hostile_only: bool,
    hostile_fallback: bool,
    pattern_d_network_idle: bool,
    user_data_dir: str | None,
    interactive: bool = False,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cascade body for the MCP auto_scrape tool.

    Extracted so auto_scrape's try/finally can wrap it cleanly without
    indenting the whole cascade one level. Owns the A/B/C → D → E1 → E2
    sequence; profile-dir lifecycle (mkdtemp + rmtree) lives in the
    enclosing auto_scrape.

    ``state`` collects cascade-scoped facts (currently the detected bot vendor)
    for the caller to fold into the payload — the MCP equivalent of what REST
    stashes on ``req.__dict__``, since there's no request object here.
    """
    cascade_state = state if state is not None else {}
    attempts: list[str] = []
    last_error: str | None = None
    hostile_skipped = False

    # ----- hostile_only fast-path: skip A/B/C, start at D -----
    if hostile_only:
        return await _hostile_only_cascade(
            url=url,
            schema_json=schema_json,
            instruction=instruction,
            model=model,
            browser=browser,
            timeout_s=timeout_s,
            hostile_fallback=hostile_fallback,
            pattern_d_network_idle=pattern_d_network_idle,
            user_data_dir=user_data_dir,
            interactive=interactive,
            attempts=attempts,
        )

    # ----- Replay (tier 0 — cached recipe, no browser, no LLM) -----
    replayed = await _try_replay_for_auto_scrape(
        url, schema_json, attempts, timeout_s, browser, user_data_dir
    )
    if replayed is not None:
        return replayed

    # F2 — per-domain tier memory. Skip tiers this domain has repeatedly proven
    # it doesn't need. Starting hint only; the cascade still falls through.
    start_rank = _policy_start_rank(url, cascade_state)

    # ----- Pattern A/B/C -----
    if start_rank <= _tier_rank_mcp("a_b_c"):
        attempts.append("a_b_c")
        resp, _profile, err = await _try_a_b_c(url, schema_json, attempts, cascade_state)
        if resp is not None:
            return resp
        last_error = err  # carries the error string when A/B/C failed/blocked

    # ----- Deterministic tiers: Pattern D, then stealth render (no LLM) -----
    payload, last_error, hostile_skipped = await _run_deterministic_tiers(
        url=url,
        schema_json=schema_json,
        attempts=attempts,
        timeout_s=timeout_s,
        pattern_d_network_idle=pattern_d_network_idle,
        browser=browser,
        user_data_dir=user_data_dir,
        last_error=last_error,
        challenge=cascade_state.get("challenge_detected"),
        start_rank=start_rank,
    )
    if payload is not None:
        return payload

    # ----- E1 → E2 escalation -----
    return await _continue_to_e_tier(
        url,
        schema_json,
        instruction,
        model,
        browser,
        timeout_s,
        attempts,
        last_error,
        hostile_skipped,
        user_data_dir,
        interactive,
    )


async def _try_a_b_c(
    url: str,
    schema_json: dict[str, Any] | None,
    attempts: list[str],
    cascade_state: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Run the A/B/C ladder for MCP auto_scrape.

    Returns ``(success_payload, profile, error_or_none)``. On success the payload
    is set; otherwise the third element is the error string (or None) for
    ``last_error``. Extracted so the cascade body stays under the branch limit
    now that the F2 gate wraps it.
    """
    last_error: str | None = None
    try:
        resp, profile = await request_with_ladder("GET", url)
        text = resp.text or ""
        product = _structured_product(text, str(resp.url))
        price = _structured_price(text)
        # v1.5.0: use the shared classifier so auto_scrape accepts A/B/C the
        # same way REST /scrape does (previously MCP always escalated to an
        # LLM call whenever a schema was supplied).
        json_ld = _structured_json_ld(text, str(resp.url))
        success = classify_extraction_success(
            mode="auto",
            schema_json=schema_json,
            force_llm_extract=False,
            status_code=resp.status_code,
            text=text,
            product=product,
            microdata_price=price,
            json_ld=json_ld,
        )
        if success:
            truncated_text, truncated = _truncate(text)
            return (
                {
                    "pattern_used": "a_b_c",
                    "pattern_attempts": attempts,
                    "url": str(resp.url),
                    "winning_profile": profile,
                    "product": product,
                    "microdata_price": price,
                    "data": None,
                    "rendered_markdown": None,
                    "body": truncated_text,
                    "truncated": truncated,
                    "blocked": False,
                    "error": None,
                    "hostile_skipped": False,
                    "is_structured": True,
                },
                profile,
                None,
            )
        # Not a signal, so we escalate either way — but knowing *which* vendor
        # walled us decides whether Pattern D is worth attempting at all.
        vendor = is_interstitial(text, resp.status_code)
        if vendor is not None:
            cascade_state["challenge_detected"] = vendor
    except BlockedError as exc:
        last_error = f"a_b_c: {exc}"
    return None, None, last_error


async def _hostile_only_cascade(
    *,
    url: str,
    schema_json: dict[str, Any] | None,
    instruction: str | None,
    model: str | None,
    browser: str | None,
    timeout_s: float,
    hostile_fallback: bool,
    pattern_d_network_idle: bool,
    user_data_dir: str | None,
    interactive: bool,
    attempts: list[str],
) -> dict[str, Any]:
    """``hostile_only=True`` — start at Pattern D, skipping the A/B/C ladder.

    For vendors recon has already classified as hostile, where the ladder is
    known to fail and its ~2-3 s is pure waste. With ``hostile_fallback=False``
    a D failure surfaces immediately instead of silently paying for an LLM.
    """
    d_payload, d_error, hostile_skipped = await _try_pattern_d_for_auto_scrape(
        url, schema_json, attempts, timeout_s, pattern_d_network_idle, user_data_dir
    )
    if d_payload is not None:
        return d_payload
    if not hostile_fallback:
        return _agent_error_payload(
            f"hostile_only=True and D failed: "
            f"{d_error or 'classifier rejected D output or [hostile] missing'}",
            blocked=True,
        ) | {
            "pattern_used": None,
            "pattern_attempts": attempts,
            "product": None,
            "hostile_skipped": hostile_skipped,
            "is_structured": False,
        }
    return await _continue_to_e_tier(
        url,
        schema_json,
        instruction,
        model,
        browser,
        timeout_s,
        attempts,
        d_error,
        hostile_skipped,
        user_data_dir,
        interactive,
    )


async def _run_deterministic_tiers(
    *,
    url: str,
    schema_json: dict[str, Any] | None,
    attempts: list[str],
    timeout_s: float,
    pattern_d_network_idle: bool,
    browser: str | None,
    user_data_dir: str | None,
    last_error: str | None,
    challenge: str | None = None,
    start_rank: int = 0,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Run the no-LLM tiers between the HTTP ladder and the agent tiers.

    Pattern D (Scrapling) then the stealth render, in cost order. Grouped behind
    one seam because they answer the same question — "can we get this
    deterministically?" — and because either failing must still let the next one
    try. Returns ``(payload, last_error, hostile_skipped)``; a None payload means
    keep escalating to E1.

    ``challenge`` is the bot vendor detected by the ladder, if any. Scrapling's
    weapon is ``solve_cloudflare``, so a non-Cloudflare wall means D would burn
    a browser launch re-fetching the same interstitial — skip straight to the
    render tier. A Cloudflare wall is exactly what D is for, so it still runs.

    ``start_rank`` (F2): a confident per-domain policy may have learned this
    domain needs render or an LLM tier, in which case D — and possibly render —
    are known-doomed and skipped.
    """
    hostile_skipped = False
    skip_d = (challenge is not None and challenge != "cloudflare") or start_rank > _tier_rank_mcp(
        "d"
    )
    if not skip_d:
        d_payload, d_error, hostile_skipped = await _try_pattern_d_for_auto_scrape(
            url, schema_json, attempts, timeout_s, pattern_d_network_idle, user_data_dir
        )
        if d_payload is not None:
            return d_payload, last_error, hostile_skipped
        if d_error is not None:
            last_error = d_error

    if start_rank <= _tier_rank_mcp("render"):
        r_payload, r_error = await _try_render_for_auto_scrape(
            url, schema_json, attempts, timeout_s, browser, user_data_dir
        )
        if r_payload is not None:
            return r_payload, last_error, hostile_skipped
        if r_error is not None:
            last_error = r_error
    return None, last_error, hostile_skipped


async def _try_pattern_d_for_auto_scrape(
    url: str,
    schema_json: dict[str, Any] | None,
    attempts: list[str],
    timeout_s: float,
    network_idle: bool = False,
    user_data_dir: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Pattern D step for the MCP ``auto_scrape`` tool.

    Returns ``(success_payload, last_error, hostile_skipped)``:

    * ``success_payload`` — the full auto_scrape response when D succeeded, else None.
    * ``last_error`` — formatted error string when D was attempted but failed, else None.
    * ``hostile_skipped`` — True when the [hostile] extra is missing and D was
      skipped without any fetch attempt (no append to ``attempts``).

    ``network_idle`` (v1.2.0+): when True, Scrapling waits for the page's
    network to settle before returning HTML. Set for SPA-rendered hostile
    vendors where results lazy-load via JS after CF clearance.

    ``user_data_dir`` (v1.3.0+): when set, Scrapling launches against this
    on-disk profile directory so cookies (cf_clearance) persist for E1/E2.
    """
    try:
        from scrapper_tool.patterns.d import hostile_client  # noqa: PLC0415
    except ImportError:
        return None, None, True

    attempts.append("d")
    # Bump fetcher timeout floor when network_idle is set (mirrors http_server).
    effective_timeout = max(timeout_s, 30.0) if network_idle else timeout_s
    fetch_kwargs: dict[str, Any] = {
        "solve_cloudflare": True,
        "network_idle": network_idle,
    }
    if user_data_dir:
        fetch_kwargs["user_data_dir"] = user_data_dir
    try:
        async with hostile_client(timeout=effective_timeout) as fetcher:
            d_resp = await fetcher.async_fetch(url, **fetch_kwargs)
    except Exception as exc:  # broad: any Scrapling failure falls through to E1
        return None, f"d: {exc}", False

    d_html = getattr(d_resp, "html_content", None) or getattr(d_resp, "body", None) or ""
    d_url = str(getattr(d_resp, "url", url) or url)
    d_product = _structured_product(d_html, d_url)
    d_price = _structured_price(d_html)
    d_json_ld = _structured_json_ld(d_html, d_url)
    # D returns rendered HTML on success — treat as a readable 200 page.
    d_accepted = classify_extraction_success(
        mode="auto",
        schema_json=schema_json,
        force_llm_extract=False,
        status_code=200,
        text=d_html,
        product=d_product,
        microdata_price=d_price,
        json_ld=d_json_ld,
    )
    if not d_accepted:
        return None, None, False

    truncated_text, truncated = _truncate(d_html)
    return (
        {
            "pattern_used": "d",
            "pattern_attempts": attempts,
            "url": d_url,
            "winning_profile": "scrapling",
            "product": d_product,
            "microdata_price": d_price,
            "data": None,
            "rendered_markdown": None,
            "body": truncated_text,
            "truncated": truncated,
            "blocked": False,
            "error": None,
            "hostile_skipped": False,
            "is_structured": True,
        },
        None,
        False,
    )


async def _try_replay_for_auto_scrape(
    url: str,
    schema_json: dict[str, Any] | None,
    attempts: list[str],
    timeout_s: float,
    browser: str | None = None,
    user_data_dir: str | None = None,
) -> dict[str, Any] | None:
    """Tier 0 for MCP — replay this domain's learned recipe. REST parity.

    Returns None on any miss (including drift, which evicts the stale recipe
    first) so the normal cascade runs.
    """
    try:
        from scrapper_tool.recipe.replay import try_replay  # noqa: PLC0415

        outcome = await try_replay(
            url,
            fetch=_make_ladder_fetch_mcp(url, timeout_s),
            render=_make_render_fetch_mcp(url, timeout_s, browser, user_data_dir),
            schema_json=schema_json,
        )
    except Exception:
        return None  # a cache problem must never break a scrape
    if outcome is None:
        return None

    attempts.append("replay")
    truncated_text, truncated = _truncate(outcome.html)
    return {
        "pattern_used": "replay",
        "pattern_attempts": attempts,
        "url": outcome.final_url,
        "winning_profile": f"recipe:{outcome.recipe.source_tier}",
        "product": None,
        "microdata_price": None,
        "data": outcome.rows if outcome.recipe.multi_row else outcome.rows[0],
        "rendered_markdown": None,
        "body": truncated_text,
        "truncated": truncated,
        "blocked": False,
        "error": None,
        "hostile_skipped": False,
        "is_structured": True,
    }


def _make_ladder_fetch_mcp(
    url: str, timeout_s: float
) -> Callable[[], Awaitable[tuple[str, int, str]]]:
    async def fetch() -> tuple[str, int, str]:
        response, _profile = await request_with_ladder("GET", url, timeout=timeout_s)
        return response.text or "", response.status_code, str(response.url)

    return fetch


def _make_render_fetch_mcp(
    url: str, timeout_s: float, browser: str | None, user_data_dir: str | None
) -> Callable[[], Awaitable[tuple[str, int, str]]] | None:
    if not _render_tier_enabled():
        return None

    async def render() -> tuple[str, int, str]:
        from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
        from scrapper_tool.agent.backends.browser import BrowserLaunchOptions  # noqa: PLC0415
        from scrapper_tool.patterns.render import render_html  # noqa: PLC0415

        overrides: dict[str, Any] = {"timeout_s": timeout_s}
        if browser:
            overrides["browser"] = browser
        if user_data_dir:
            overrides["user_data_dir"] = user_data_dir
        cfg = AgentConfig.from_env().merged(**overrides)
        result = await render_html(
            url,
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
        )
        return result.html, result.status, result.final_url

    return render


def _learn_recipe_mcp(
    url: str, html: str, data: Any, *, source_tier: str, schema_json: Any = None
) -> None:
    """Teach the cache from a successful tier. Never raises."""
    if not html or not data:
        return
    try:
        from scrapper_tool.recipe.replay import learn_from_success  # noqa: PLC0415

        learn_from_success(url, html, data, source_tier=source_tier, schema_json=schema_json)
    except Exception:
        return  # an optimisation for next time; never fail the current call


def _policy_start_rank(url: str, state: dict[str, Any]) -> int:
    """F2 — lowest tier rank worth attempting for ``url``. REST parity.

    Records the chosen rank in ``state`` and logs the skip. Any failure returns 0
    (run everything), because a policy problem must never break a scrape.
    """
    try:
        from scrapper_tool.recipe.policy import (  # noqa: PLC0415
            domain_policy_enabled,
            get_policy_store,
        )

        if not domain_policy_enabled():
            return 0
        policy = get_policy_store().get(url)
    except Exception:
        return 0
    if policy is None:
        return 0
    rank = policy.start_tier_rank()
    state["policy_start_rank"] = rank
    if rank > 0:
        from scrapper_tool._logging import get_logger  # noqa: PLC0415

        get_logger(__name__).info("auto_scrape.policy_skip", url=url, best_tier=policy.best_tier)
    return rank


def _record_policy(payload: dict[str, Any], url: str, state: dict[str, Any]) -> None:
    """Remember the winning tier for this domain. Best-effort, never raises."""
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
        get_policy_store().record(url, tier, challenge_vendor=state.get("challenge_detected"))
    except Exception:
        return


def _tier_rank_mcp(tier: str) -> int:
    from scrapper_tool.recipe.policy import tier_rank  # noqa: PLC0415

    return tier_rank(tier)


def _render_tier_enabled() -> bool:
    """Render tier is on by default; ``SCRAPPER_TOOL_RENDER_TIER=0`` disables it."""
    raw = os.environ.get("SCRAPPER_TOOL_RENDER_TIER")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def _try_render_for_auto_scrape(
    url: str,
    schema_json: dict[str, Any] | None,
    attempts: list[str],
    timeout_s: float,
    browser: str | None = None,
    user_data_dir: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Stealth-render step for the MCP ``auto_scrape`` tool — no LLM.

    REST parity for :func:`scrapper_tool.http_server._do_render_step`. Returns
    ``(success_payload, last_error)``; ``(None, None)`` means "no signal, keep
    escalating".
    """
    if not _render_tier_enabled():
        return None, None
    try:
        from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
        from scrapper_tool.agent.backends.browser import BrowserLaunchOptions  # noqa: PLC0415
        from scrapper_tool.patterns.render import render_html  # noqa: PLC0415
    except ImportError:
        # [llm-agent] extra absent — skip without touching `attempts`.
        return None, None

    attempts.append("render")
    overrides: dict[str, Any] = {"timeout_s": timeout_s}
    if browser:
        overrides["browser"] = browser
    if user_data_dir:
        overrides["user_data_dir"] = user_data_dir
    cfg = AgentConfig.from_env().merged(**overrides)
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
    try:
        result = await render_html(
            url,
            browser=cfg.browser,
            timeout_s=cfg.timeout_s,
            options=options,
            cdp_url=cfg.obscura_cdp_url,
        )
    except Exception as exc:  # broad: any browser failure falls through to E1
        return None, f"render: {exc}"

    html, status, final_url = result.html, result.status, result.final_url
    product = _structured_product(html, final_url)
    price = _structured_price(html)
    json_ld = _structured_json_ld(html, final_url)
    accepted = classify_extraction_success(
        mode="auto",
        schema_json=schema_json,
        force_llm_extract=False,
        status_code=status,
        text=html,
        product=product,
        microdata_price=price,
        json_ld=json_ld,
    )
    if not accepted:
        return None, None

    # Learn from the expensive win so the next page on this domain replays free.
    _learn_recipe_mcp(url, html, product, source_tier="render", schema_json=schema_json)

    truncated_text, truncated = _truncate(html)
    return (
        {
            "pattern_used": "render",
            "pattern_attempts": attempts,
            "url": final_url,
            "winning_profile": cfg.browser,
            "product": product,
            "microdata_price": price,
            "data": None,
            "rendered_markdown": None,
            "body": truncated_text,
            "truncated": truncated,
            "blocked": False,
            "error": None,
            "hostile_skipped": False,
            "is_structured": True,
        },
        None,
    )


def _agent_result_payload(result: Any) -> dict[str, Any]:
    """Serialize an :class:`AgentResult` for MCP transport.

    - Body / markdown truncated to 64 KB.
    - Screenshots base64-encoded, capped at :data:`_MAX_AGENT_SCREENSHOTS`.
    - DOM snippets dropped after :data:`_MAX_DOM_SNIPPET_STEPS` steps.
    """
    markdown_raw = result.rendered_markdown
    markdown, markdown_trunc = _truncate(markdown_raw) if markdown_raw else (None, False)

    actions: list[dict[str, Any]] = []
    for trace in result.actions or []:
        keep_dom = trace.step <= _MAX_DOM_SNIPPET_STEPS
        actions.append(
            {
                "step": trace.step,
                "action": trace.action,
                "target": trace.target,
                "screenshot_idx": trace.screenshot_idx,
                "dom_snippet": trace.dom_snippet if keep_dom else None,
                "latency_ms": trace.latency_ms,
            }
        )

    screenshots: list[str] | None = None
    if result.screenshots:
        screenshots = [
            base64.b64encode(s).decode("ascii") for s in result.screenshots[:_MAX_AGENT_SCREENSHOTS]
        ]

    return {
        "mode": result.mode,
        "data": result.data,
        "final_url": result.final_url,
        "rendered_markdown": markdown,
        "rendered_markdown_truncated": markdown_trunc,
        "screenshots": screenshots,
        "actions": actions,
        "tokens_used": result.tokens_used,
        "blocked": result.blocked,
        "error": result.error,
        "duration_s": result.duration_s,
        "steps_used": result.steps_used,
    }


def _build_server(  # noqa: PLR0915 — single-place tool registration
    *, host: str = "127.0.0.1", port: int = 8000
) -> Any:
    """Lazy-construct the FastMCP server.

    Lazy because the ``mcp`` SDK is an optional extra
    (``pip install scrapper-tool[agent]``); importing at module top
    would break ``import scrapper_tool.mcp`` for consumers without the
    extra. The unit tests mock this function to avoid a real SDK
    dependency in the default test profile.

    Parameters
    ----------
    host
        Network bind address used by the SSE / streamable-HTTP
        transports. Default ``127.0.0.1`` (localhost-only). Set to
        ``0.0.0.0`` to expose the server on a published Docker port or
        to a LAN.
    port
        TCP port for SSE / streamable-HTTP. Default 8000. Ignored for
        the stdio transport.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # noqa: PLC0415
    except ImportError as exc:
        msg = (
            "scrapper-tool MCP server requires the [agent] extra.\n"
            "Install with: pip install scrapper-tool[agent]"
        )
        raise ImportError(msg) from exc

    server = FastMCP(
        name="scrapper-tool",
        instructions=(
            "Reusable web-scraping toolkit. RECOMMENDED first tool: "
            "auto_scrape (auto-escalates A/B/C -> D -> E1 -> E2 in one call). "
            "Power tools: fetch_with_ladder for TLS-sensitive fetches "
            "(pass extract_structured=True to also parse JSON-LD), "
            "extract_product for schema.org Product+Offer parsing on "
            "raw HTML, extract_microdata_price for <meta itemprop='price'> "
            "anchors, agent_extract / agent_browse for Pattern E direct, "
            "canary for fingerprint-health probes. "
            "See https://github.com/ValeroK/scrapper-tool"
        ),
        host=host,
        port=port,
    )

    # ---- Tool: fetch_with_ladder ------------------------------------------

    @server.tool(
        name="fetch_with_ladder",
        description=(
            "Issue an HTTP request through the four-profile TLS-impersonation "
            "ladder (" + " -> ".join(IMPERSONATE_LADDER) + ") "
            "until a profile returns non-403/503. Returns status, body "
            "(truncated to 64 KB), and which profile won. Use for sites "
            "that fingerprint the default httpx stack."
        ),
    )
    async def fetch_with_ladder(
        url: str,
        method: str = "GET",
        use_curl_cffi: bool = True,
        extract_structured: bool = False,
    ) -> dict[str, Any]:
        """Fetch ``url`` through the ladder; return structured result.

        When ``use_curl_cffi=False`` this falls back to the plain httpx
        client (no ladder), useful for sites that don't fingerprint.

        When ``extract_structured=True`` (v1.1.0+), also runs Pattern B
        (extruct JSON-LD/microdata → ProductOffer) and Pattern C (CSS
        microdata price) on the response body and includes ``product``
        and ``microdata_price`` fields in the result. Eliminates the
        common two-tool pattern (fetch then extract_product).
        """
        if use_curl_cffi:
            try:
                resp, profile = await request_with_ladder(method, url)
            except BlockedError as exc:
                return {
                    "url": url,
                    "blocked": True,
                    "winning_profile": None,
                    "status": None,
                    "body": None,
                    "truncated": False,
                    "error": str(exc),
                }
            text, truncated = _truncate(resp.text)
            payload: dict[str, Any] = {
                "url": url,
                "blocked": False,
                "winning_profile": profile,
                "status": int(resp.status_code),
                "body": text,
                "truncated": truncated,
                "error": None,
            }
            if extract_structured and resp.text:
                payload["product"] = _structured_product(resp.text, str(resp.url))
                payload["microdata_price"] = _structured_price(resp.text)
            return payload

        # Plain httpx path.
        try:
            async with vendor_client() as client:
                resp = await request_with_retry(client, method, url)
        except VendorHTTPError as exc:
            return {
                "url": url,
                "blocked": False,
                "winning_profile": "httpx",
                "status": None,
                "body": None,
                "truncated": False,
                "error": str(exc),
            }
        text, truncated = _truncate(resp.text)
        payload = {
            "url": url,
            "blocked": False,
            "winning_profile": "httpx",
            "status": int(resp.status_code),
            "body": text,
            "truncated": truncated,
            "error": None,
        }
        if extract_structured and resp.text:
            payload["product"] = _structured_product(resp.text, str(resp.url))
            payload["microdata_price"] = _structured_price(resp.text)
        return payload

    # ---- Tool: extract_product --------------------------------------------

    @server.tool(
        name="extract_product",
        description=(
            "Parse a schema.org Product+Offer block from HTML. Handles "
            "JSON-LD, microdata, and RDFa via extruct. Returns a "
            "ProductOffer dict (name, sku, mpn, gtin, brand, "
            "description, image, price, currency, availability, url) "
            "or null if no Product block is present."
        ),
    )
    async def extract_product(
        html: str,
        base_url: str | None = None,
    ) -> dict[str, Any] | None:
        product = extract_product_offer(html, base_url=base_url)
        if product is None:
            return None
        return product.model_dump(mode="json")

    # ---- Tool: extract_microdata_price ------------------------------------

    @server.tool(
        name="extract_microdata_price",
        description=(
            "Parse <meta itemprop='price'> + <meta itemprop='priceCurrency'> "
            "schema.org microdata anchors from HTML (Pattern C). Returns "
            "{price, currency} or null if either anchor is absent."
        ),
    )
    async def extract_microdata_price(html: str) -> dict[str, Any] | None:
        result = _extract_microdata_price(html)
        if result is None:
            return None
        price, currency = result
        return {"price": str(price), "currency": currency}

    # ---- Tool: agent_extract (Pattern E1) ---------------------------------

    @server.tool(
        name="agent_extract",
        description=(
            "Pattern E1 — render a page with a stealth browser (Camoufox by "
            "default) and run a SINGLE local-LLM call to extract structured "
            "JSON matching the supplied schema. Fast path for protected "
            "sites — escalate here only when the TLS-impersonation ladder "
            "AND Pattern D have failed. Requires the [llm-agent] extra and a "
            "running local LLM server (Ollama by default; "
            "set SCRAPPER_TOOL_AGENT_LLM and SCRAPPER_TOOL_AGENT_MODEL to "
            "configure). Returns {data, blocked, error, final_url, "
            "rendered_markdown, actions, duration_s, steps_used}."
        ),
    )
    async def agent_extract(
        url: str,
        schema_json: dict[str, Any] | None = None,
        instruction: str | None = None,
        model: str | None = None,
        browser: str | None = None,
        headful: bool = False,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Run Pattern E1 (Crawl4AI extraction) and return a serializable dict."""
        try:
            from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
            from scrapper_tool.agent import agent_extract as _agent_extract  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover — covered by mock
            return _agent_error_payload(_AGENT_NOT_INSTALLED, original=str(exc))

        cfg = AgentConfig.from_env()
        overrides: dict[str, Any] = {
            "headful": headful,
            "timeout_s": timeout_s,
        }
        if model:
            overrides["model"] = model
        if browser:
            overrides["browser"] = browser

        schema = schema_json or "Extract the page's salient data into a JSON object."

        try:
            result = await _agent_extract(
                url,
                schema,
                instruction=instruction,
                config=cfg,
                **overrides,
            )
        except AgentBlockedError as exc:
            return _agent_error_payload(str(exc), blocked=True)
        except AgentError as exc:
            return _agent_error_payload(str(exc))

        return _agent_result_payload(result)

    # ---- Tool: agent_browse (Pattern E2) ----------------------------------

    @server.tool(
        name="agent_browse",
        description=(
            "Pattern E2 — multi-step LLM-driven agent loop for interactive "
            "tasks (login, multi-step navigation, dynamic forms, 'click "
            "load more' pagination). Higher latency than agent_extract — "
            "use only when the page requires interaction. Requires the "
            "[llm-agent] extra and a local LLM. Returns {data, blocked, "
            "error, final_url, screenshots (base64 PNG), actions, "
            "duration_s, steps_used}."
        ),
    )
    async def agent_browse(
        url: str,
        instruction: str,
        schema_json: dict[str, Any] | None = None,
        model: str | None = None,
        browser: str | None = None,
        max_steps: int = 50,
        headful: bool = False,
        timeout_s: float = 180.0,
    ) -> dict[str, Any]:
        """Run Pattern E2 (browser-use agent) and return a serializable dict."""
        try:
            from scrapper_tool.agent import AgentConfig  # noqa: PLC0415
            from scrapper_tool.agent import agent_browse as _agent_browse  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            return _agent_error_payload(_AGENT_NOT_INSTALLED, original=str(exc))

        cfg = AgentConfig.from_env()
        overrides: dict[str, Any] = {
            "headful": headful,
            "timeout_s": timeout_s,
            "max_steps": max_steps,
        }
        if model:
            overrides["model"] = model
        if browser:
            overrides["browser"] = browser

        try:
            result = await _agent_browse(
                url,
                instruction,
                schema=schema_json,
                config=cfg,
                **overrides,
            )
        except AgentBlockedError as exc:
            return _agent_error_payload(str(exc), blocked=True)
        except AgentError as exc:
            return _agent_error_payload(str(exc))

        return _agent_result_payload(result)

    # ---- Tool: auto_scrape (NEW v1.1.0) -----------------------------------

    @server.tool(
        name="auto_scrape",
        description=(
            "PRIMARY scraping tool (v1.1.0+; cascade fixed v1.1.3; v1.2.0 adds "
            "hostile_only + is_structured). Auto-escalating ladder: tries "
            "Pattern A/B/C (TLS impersonation + JSON-LD/microdata extraction) "
            "first; if blocked or schema not satisfied, tries Pattern D "
            "(Scrapling, when [hostile] extra installed) for hostile vendors; "
            "then a stealth-browser render with the same deterministic "
            "extractors and NO LLM (v1.6.0); if that still yields nothing, "
            "escalates to Pattern E1 (Crawl4AI + LLM). Pattern E2 (browser-use "
            "multi-step agent) runs only with interactive=True — it is the "
            "priciest tier and earns its cost on login / pagination / dynamic "
            "forms, not on a page that is simply walled. Set hostile_only=True "
            "to skip the A/B/C ladder for vendors recon-classified as hostile — "
            "saves ~2-3s per call. Returns pattern_used + pattern_attempts + "
            "challenge_detected (the bot vendor that walled us, or null) + "
            "is_structured (sidecar's success verdict) + hostile_skipped "
            "(true when [hostile] extra missing)."
        ),
    )
    async def auto_scrape(
        url: str,
        schema_json: dict[str, Any] | None = None,
        instruction: str | None = None,
        model: str | None = None,
        browser: str | None = None,
        timeout_s: float = 120.0,
        hostile_only: bool = False,
        hostile_fallback: bool = True,
        pattern_d_network_idle: bool = False,
        persist_browser_profile_dir: str | None = None,
        interactive: bool = False,
    ) -> dict[str, Any]:
        """Run the full A/B/C → D → render → E1 → E2 escalation ladder.

        Set interactive=True only when the target genuinely needs a multi-step
        agent (login, pagination, dynamic forms). E2 is by far the most
        expensive tier, so by default a blocked E1 stops and returns the blocked
        result instead of spending an agent loop to hit the same wall.

        Set hostile_only=True to skip A/B/C and start at Pattern D (mirrors
        ``mode='hostile'`` on the REST side). When D fails with
        hostile_fallback=False, returns a clean error rather than escalating
        to E1/E2.

        Set pattern_d_network_idle=True for SPA-rendered hostile vendors
        (Tasca, etc.) where results lazy-load via JS after CF clearance —
        adds ~5-15s of D fetch time but lets the page hydrate first.

        v1.3.0: when D might run, the cascade allocates a per-request
        ``user_data_dir`` and threads it to D + E1 + E2 so Cloudflare
        clearance cookies persist across cascade steps. Pass
        ``persist_browser_profile_dir`` to opt into cross-request reuse
        (caller owns lifecycle).
        """
        # v1.3.0: resolve and own the cascade's profile dir lifecycle.
        # Mirrors http_server._resolve_profile_dir.
        cleanup_dir: str | None = None
        if persist_browser_profile_dir:
            profile_dir: str | None = persist_browser_profile_dir
        elif _hostile_available_for_mcp():
            # auto_scrape always runs the cascade including D when the
            # extra is available (hostile_only or normal cascade); so we
            # always allocate when [hostile] is installed and no explicit
            # caller dir was provided.
            import tempfile  # noqa: PLC0415

            profile_dir = tempfile.mkdtemp(prefix="scrapper-cascade-mcp-")
            cleanup_dir = profile_dir
        else:
            profile_dir = None

        state: dict[str, Any] = {}
        try:
            payload = await _auto_scrape_inner(
                url=url,
                schema_json=schema_json,
                instruction=instruction,
                model=model,
                browser=browser,
                timeout_s=timeout_s,
                hostile_only=hostile_only,
                hostile_fallback=hostile_fallback,
                pattern_d_network_idle=pattern_d_network_idle,
                user_data_dir=profile_dir,
                interactive=interactive,
                state=state,
            )
            # Reported no matter which tier won: the vendor that walled us is
            # the single most useful fact for tuning a target.
            payload["challenge_detected"] = state.get("challenge_detected")
            # F2: remember which tier reached content so the next call for this
            # domain starts there.
            _record_policy(payload, url, state)
            return payload
        finally:
            if cleanup_dir is not None:
                import shutil  # noqa: PLC0415

                shutil.rmtree(cleanup_dir, ignore_errors=True)

    # ---- Tool: map_site (NEW v1.6.0) --------------------------------------

    @server.tool(
        name="map_site",
        description=(
            "Discover URLs on a site. Combines sitemaps (found via robots.txt "
            "Sitemap: directives, falling back to /sitemap.xml) with links from "
            "the seed page. Cheap — no browser, no LLM. Use before crawl_site to "
            "see how big a job it is; truncation is reported, never silent."
        ),
    )
    async def map_site_tool(
        url: str,
        max_urls: int = 200,
        same_domain: bool = True,
        include_sitemap: bool = True,
        fetch_seed: bool = True,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        """Discover URLs on the seed's site."""
        from scrapper_tool.crawl.map import make_ladder_fetch, map_site  # noqa: PLC0415

        result = await map_site(
            url,
            max_urls=max_urls,
            same_domain=same_domain,
            include_sitemap=include_sitemap,
            fetch=make_ladder_fetch(timeout_s) if fetch_seed else None,
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
        }

    # ---- Tool: crawl_site (NEW v1.6.0) -----------------------------------

    @server.tool(
        name="crawl_site",
        description=(
            "Crawl a site breadth-first, running the full auto_scrape cascade on "
            "each page — so every page benefits from recipe replay, the render "
            "tier, and proxy rotation, and the recipe learned on page one makes "
            "the rest cheap. robots.txt is honoured by default, including "
            "Crawl-delay. Bounded by depth / max_pages / concurrency, and the "
            "response reports what the bounds left unvisited. Page HTML is "
            "omitted by default: a 50-page crawl of rendered pages is tens of MB "
            "and would swamp the agent's context."
        ),
    )
    async def crawl_site_tool(
        url: str,
        schema_json: dict[str, Any] | None = None,
        depth: int = 2,
        max_pages: int = 25,
        concurrency: int = 4,
        same_domain: bool = True,
        respect_robots: bool = True,
        interactive: bool = False,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        """Crawl from ``url``, running the auto cascade per page."""
        from scrapper_tool.crawl.crawl import crawl_to_list  # noqa: PLC0415

        async def scrape_one(target: str) -> dict[str, Any]:
            return await _auto_scrape_inner(
                url=target,
                schema_json=schema_json,
                instruction=None,
                model=None,
                browser=None,
                timeout_s=timeout_s,
                hostile_only=False,
                hostile_fallback=True,
                pattern_d_network_idle=False,
                user_data_dir=None,
                interactive=interactive,
            )

        pages, stats = await crawl_to_list(
            url,
            scrape=scrape_one,
            depth=depth,
            max_pages=max_pages,
            concurrency=concurrency,
            same_domain=same_domain,
            respect_robots=respect_robots,
        )
        results: list[dict[str, Any]] = []
        for page in pages:
            payload = dict(page.payload or {})
            payload.pop("body", None)  # the bulky field; extracted data is the point
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
        return {"seed": url, "pages": results, "stats": stats.as_dict()}

    # ---- Tool: canary -----------------------------------------------------

    @server.tool(
        name="canary",
        description=(
            "Walk the impersonation ladder against url and report which "
            "profile won (or all-blocked). Same as the scrapper-tool "
            "canary CLI but accessible to LLM agents. Useful for "
            "diagnosing which TLS fingerprint a vendor is rejecting."
        ),
    )
    async def canary_tool(
        url: str,
        profiles: list[str] | None = None,
    ) -> dict[str, Any]:
        ladder: tuple[str, ...] = tuple(profiles) if profiles else IMPERSONATE_LADDER
        return await run_canary(url, ladder=ladder)

    return server


_VALID_TRANSPORTS = {"stdio", "sse", "streamable-http"}

_HELP_TEXT = """\
scrapper-tool-mcp {version}
MCP server exposing scrapper-tool helpers (fetch_with_ladder,
extract_product, extract_microdata_price, canary, agent_extract,
agent_browse) as tools any MCP-aware LLM agent can call.

USAGE:
  scrapper-tool-mcp [--transport stdio|sse|streamable-http]
                    [--host HOST] [--port PORT]

TRANSPORTS:
  stdio (default)    JSON-RPC over stdin/stdout. Used by clients that
                     spawn the server as a subprocess (Claude Desktop,
                     Claude Code's local MCP wiring).
  sse                Server-Sent Events over HTTP. Mount /sse on the
                     given host:port. Older but widely-supported.
  streamable-http    Streamable HTTP (the modern MCP transport). Mount
                     /mcp on the given host:port. Recommended for
                     Cursor, Claude Code remote, and most 2026 clients.

ENVIRONMENT (override flags):
  SCRAPPER_TOOL_MCP_TRANSPORT  Same as --transport.
  SCRAPPER_TOOL_MCP_HOST       Same as --host. Default 127.0.0.1.
                               Use 0.0.0.0 inside Docker.
  SCRAPPER_TOOL_MCP_PORT       Same as --port. Default 8000.

EXAMPLES:
  # Local stdio (Claude Desktop / Claude Code spawn pattern)
  scrapper-tool-mcp

  # HTTP service for Cursor / Claude Code remote / mcp-use:
  scrapper-tool-mcp --transport streamable-http --host 0.0.0.0 --port 8000

See docs/agent-integration.md for client wiring patterns.
"""


def _parse_args(argv: list[str]) -> tuple[str, str, int] | int:
    """Parse argv → (transport, host, port). Returns int exit code on --help.

    Pure parsing; mocked easily in tests.
    """
    import os  # noqa: PLC0415

    transport = os.environ.get("SCRAPPER_TOOL_MCP_TRANSPORT", "stdio")
    host = os.environ.get("SCRAPPER_TOOL_MCP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("SCRAPPER_TOOL_MCP_PORT", "8000"))
    except ValueError:
        sys.stderr.write("SCRAPPER_TOOL_MCP_PORT must be an integer\n")
        return 2

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {"-h", "--help"}:
            print(_HELP_TEXT.format(version=__version__))
            return 0
        if arg == "--transport" and i + 1 < len(argv):
            transport = argv[i + 1]
            i += 2
            continue
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
            continue
        if arg == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                sys.stderr.write(f"--port must be an integer, got {argv[i + 1]!r}\n")
                return 2
            i += 2
            continue
        sys.stderr.write(f"unknown argument: {arg!r}\n")
        sys.stderr.write("Run with --help for usage.\n")
        return 2

    if transport not in _VALID_TRANSPORTS:
        sys.stderr.write(
            f"invalid --transport {transport!r}. Choose from: {sorted(_VALID_TRANSPORTS)}\n"
        )
        return 2

    return transport, host, port


def main() -> int:
    """Entry point for the ``scrapper-tool-mcp`` console script.

    Supports three transports — stdio (default, used by Claude
    Desktop's spawn pattern) and the HTTP-based SSE / streamable-http
    transports (used when the server runs as a long-lived service in
    Docker and external clients connect via URL).

    Exits with code 0 on clean shutdown, 1 on the ``[agent]`` extra not
    installed, 2 on argv error.
    """
    parsed = _parse_args(sys.argv[1:])
    if isinstance(parsed, int):
        return parsed
    transport, host, port = parsed

    try:
        server = _build_server(host=host, port=port)
    except ImportError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1

    if transport != "stdio":
        sys.stderr.write(f"scrapper-tool-mcp listening on {transport} at {host}:{port}\n")
    server.run(transport=transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "main",
]

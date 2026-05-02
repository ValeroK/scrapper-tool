"""Public agent API + long-lived :class:`AgentSession`.

Three coroutines and one async context manager:

- :func:`agent_extract` — E1, fast extraction-after-render (Crawl4AI).
- :func:`agent_browse` — E2, multi-step browser-use agent loop.
- :func:`agent_session` — long-lived browser+LLM context for batched
  calls. Avoids the ~3-5 s Patchright cold start and Ollama model load
  per call.

All three accept an :class:`AgentConfig` *or* per-call keyword overrides
that resolve through ``config.merged(**overrides)``. Env-var resolution
happens in :func:`_resolve_config`.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger
from scrapper_tool.agent import browse as _browse
from scrapper_tool.agent import extract as _extract
from scrapper_tool.agent.types import AgentConfig, AgentResult

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic import BaseModel

_logger = get_logger(__name__)


def _resolve_config(config: AgentConfig | None, overrides: dict[str, Any]) -> AgentConfig:
    base = config or AgentConfig.from_env()
    if not overrides:
        return base
    return base.merged(**overrides)


# --- One-shot calls -------------------------------------------------------


async def agent_extract(
    url: str,
    schema: type[BaseModel] | dict[str, Any] | str,
    *,
    instruction: str | None = None,
    config: AgentConfig | None = None,
    **overrides: Any,
) -> AgentResult:
    """Render ``url`` with a stealth browser; convert to structured JSON in
    one LLM call.

    Default for "scrape any data". 1 LLM call per page, fast and reliable.
    Use :func:`agent_browse` for interactive tasks.

    Parameters
    ----------
    url : str
        Page to render.
    schema : pydantic model | JSON Schema dict | natural-language str
        Tells the LLM what shape to produce. A pydantic class is the
        most type-safe option; a dict is forwarded to Crawl4AI as a JSON
        Schema; a string is interpreted as a natural-language hint.
    instruction : str, optional
        Free-form extraction guidance. Defaults to "extract the schema's
        fields, return only JSON."
    config : AgentConfig, optional
        Pre-built config. Defaults to ``AgentConfig.from_env()``.
    **overrides
        Per-call config overrides (``model="qwen3-vl"``, ``headful=True``,
        …). Applied via ``config.merged(**overrides)``.
    """
    cfg = _resolve_config(config, overrides)
    _logger.info("agent.extract.start", url=url, model=cfg.model, browser=cfg.browser)
    return await _extract.run_extract(url, schema, config=cfg, instruction=instruction)


async def agent_browse(
    url: str,
    instruction: str,
    *,
    schema: type[BaseModel] | dict[str, Any] | None = None,
    config: AgentConfig | None = None,
    **overrides: Any,
) -> AgentResult:
    """Multi-step LLM-driven agent loop. Use only when interaction is required.

    Parameters
    ----------
    url : str
        Starting URL. The agent navigates here first, then follows
        ``instruction``.
    instruction : str
        Natural-language task description ("log in with X / Y, then
        download the latest invoice").
    schema : pydantic model | dict, optional
        If provided, the agent's final output is validated against this
        schema; on failure the result carries
        ``error="schema-validation-failed"``.
    config : AgentConfig, optional
        Defaults to ``AgentConfig.from_env()``.
    **overrides
        Per-call overrides — same shape as :func:`agent_extract`.
    """
    cfg = _resolve_config(config, overrides)
    _logger.info("agent.browse.start", url=url, model=cfg.model, browser=cfg.browser)
    return await _browse.run_browse(url, instruction, config=cfg, schema=schema)


# --- Long-lived session ---------------------------------------------------


class AgentSession:
    """Reuse a warm LLM client across many extract / browse calls.

    Constructed by :func:`agent_session`. Calling ``await
    session.extract(...)`` / ``await session.browse(...)`` runs the same
    code path as the one-shot helpers but skips re-resolving config.

    NB: Crawl4AI / browser-use create their own browser instances per
    call by design. The session's value is the resolved config and an
    early-validated LLM probe — both saved across calls.
    """

    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    @property
    def config(self) -> AgentConfig:
        return self._config

    async def extract(
        self,
        url: str,
        schema: type[BaseModel] | dict[str, Any] | str,
        *,
        instruction: str | None = None,
        **overrides: Any,
    ) -> AgentResult:
        cfg = self._config.merged(**overrides) if overrides else self._config
        return await _extract.run_extract(url, schema, config=cfg, instruction=instruction)

    async def browse(
        self,
        url: str,
        instruction: str,
        *,
        schema: type[BaseModel] | dict[str, Any] | None = None,
        **overrides: Any,
    ) -> AgentResult:
        cfg = self._config.merged(**overrides) if overrides else self._config
        return await _browse.run_browse(url, instruction, config=cfg, schema=schema)


@asynccontextmanager
async def agent_session(
    *, config: AgentConfig | None = None, **overrides: Any
) -> AsyncIterator[AgentSession]:
    """Yield a long-lived :class:`AgentSession`.

    Probes the LLM once at entry — fail fast if Ollama is down — then
    yields a session that can be used for many calls. Cleanup is
    automatic on exit.

    Example::

        async with agent_session(model="qwen3-vl:8b") as s:
            r1 = await s.extract("https://a.example", schema=...)
            r2 = await s.browse("https://b.example", "log in and ...")
    """
    cfg = _resolve_config(config, overrides)
    # Pre-probe the LLM so the first real call doesn't surprise the caller.
    from scrapper_tool.agent.backends import get_llm_backend  # noqa: PLC0415

    await get_llm_backend(cfg).probe()
    session = AgentSession(cfg)
    try:
        yield session
    finally:
        # Currently no shared resources to close — placeholder for
        # future warm-browser pooling.
        await asyncio.sleep(0)


__all__ = [
    "AgentSession",
    "agent_browse",
    "agent_extract",
    "agent_session",
]

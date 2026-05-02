"""Pattern E — LLM-driven scraping for any protected site.

Two modes:

- :func:`agent_extract` (E1): render with a stealth browser, run a
  single local-LLM call to convert the rendered page into structured
  JSON via a schema. Fast path. Default for "scrape any data".
- :func:`agent_browse` (E2): multi-step LLM-driven agent loop via
  ``browser-use`` for interactive tasks (login, multi-step nav, dynamic
  forms). Slower, opt-in.

Both modes share a pluggable backend stack:

- BrowserBackend: Camoufox (default), Patchright, Zendriver, Botasaurus,
  Scrapling.
- LLMBackend: Ollama (default), llama.cpp, vLLM, generic OpenAI-compat.
- CaptchaSolver: free OSS cascade (CamoufoxAuto → Theyka) → optional
  paid (CapSolver / NopeCHA / 2Captcha) when ``captcha_api_key`` is set.
- BehaviorPolicy: humanlike timing (default), fast, off.
- FingerprintGenerator: Browserforge (default for non-Camoufox), none.

Optional install (this whole package is gated)::

    pip install scrapper-tool[llm-agent]
    # then for the default Camoufox backend:
    camoufox fetch
    # and for a local LLM:
    ollama pull qwen3-vl:8b   # default for 16 GB VRAM (or qwen3-vl:4b on 8 GB)

Public surface (the only stable contract — submodules may move)::

    from scrapper_tool.agent import (
        agent_extract,
        agent_browse,
        agent_session,
        AgentConfig,
        AgentResult,
        ActionTrace,
    )
"""

from __future__ import annotations

from scrapper_tool.agent.runner import (
    AgentSession,
    agent_browse,
    agent_extract,
    agent_session,
)
from scrapper_tool.agent.types import (
    ActionTrace,
    AgentConfig,
    AgentResult,
)

__all__ = [
    "ActionTrace",
    "AgentConfig",
    "AgentResult",
    "AgentSession",
    "agent_browse",
    "agent_extract",
    "agent_session",
]

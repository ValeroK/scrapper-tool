"""Pydantic models + AgentConfig for Pattern E (LLM-agent layer).

These are the *only* types in :mod:`scrapper_tool.agent` that pure
consumers (no ``[llm-agent]`` extra installed) can import — everything
else lazy-imports heavy deps (Camoufox, browser-use, Crawl4AI, …).

``AgentConfig.from_env()`` resolves all knobs from
``SCRAPPER_TOOL_AGENT_*`` environment variables so the consumer can
deploy the agent layer purely through env config without touching code.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

# Runtime import, not TYPE_CHECKING: CookieIn appears in an AgentConfig field
# annotation and pydantic resolves those against module globals when it builds
# the model. `cookies` imports nothing but the stdlib and pydantic, so this
# stays inside the "importable without the [llm-agent] extra" contract above.
from scrapper_tool.cookies import CookieIn  # noqa: TC001

# --- Result types ---------------------------------------------------------


class ActionTrace(BaseModel):
    """One step of an E2 (browse) agent loop.

    E1 (extract) returns at most a single synthetic trace so callers can
    treat the two modes uniformly when logging/auditing.
    """

    model_config = ConfigDict(frozen=True)

    step: int
    action: str
    """Action verb — ``click`` / ``type`` / ``extract`` / ``scroll`` / ``goto``."""
    target: str | None = None
    """CSS selector or coordinate hint, when known."""
    screenshot_idx: int | None = None
    """Index into ``AgentResult.screenshots``, or ``None`` if no screenshot."""
    dom_snippet: str | None = None
    """Truncated DOM snippet (≤1 KB) for debugging. Dropped after step 5
    in MCP responses to bound context."""
    latency_ms: int = 0


class AgentResult(BaseModel):
    """Outcome of an :func:`agent_extract` or :func:`agent_browse` call.

    The same shape across both modes so callers can branch on
    ``mode`` only when they need to.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mode: Literal["extract", "browse"]
    data: dict[str, object] | list[object] | None = None
    final_url: str
    rendered_markdown: str | None = None
    """E1 only — Crawl4AI's clean markdown of the rendered page."""
    screenshots: list[bytes] | None = None
    """PNG bytes, capped at 3 frames; ``None`` if vision was disabled."""
    actions: list[ActionTrace] = Field(default_factory=list)
    tokens_used: int = 0
    blocked: bool = False
    error: str | None = None
    """Recoverable error category — ``schema-validation-failed`` /
    ``no-match`` / ``captcha-encountered`` (when no solver). Hard
    failures raise an exception instead."""
    duration_s: float = 0.0
    steps_used: int = 0


# --- Configuration --------------------------------------------------------


CaptchaSolverName = Literal[
    "auto", "camoufox-auto", "theyka", "capsolver", "nopecha", "twocaptcha", "none"
]
BrowserBackendName = Literal["camoufox", "patchright", "scrapling", "obscura"]
CamoufoxDisplayName = Literal["headless", "virtual"]
LLMBackendName = Literal["ollama", "llama_cpp", "vllm", "openai_compat"]
BehaviorName = Literal["humanlike", "fast", "off"]
FingerprintName = Literal["browserforge", "none"]
PaidFallbackName = Literal["capsolver", "nopecha", "twocaptcha", "none"]


class AgentConfig(BaseModel):
    """Knobs for the Pattern E agent.

    All defaults target the "ultimate scraper" goal: Camoufox stealth,
    humanlike behavior, free-OSS captcha cascade, local Ollama LLM. Every
    field is overridable via env var (``from_env``) or per-call
    (``agent_extract(..., **overrides)``).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # --- Browser backend
    browser: BrowserBackendName = "camoufox"
    fingerprint: FingerprintName = "browserforge"
    behavior: BehaviorName = "humanlike"
    headful: bool = False
    proxy: str | None = None
    # v1.3.0: when set, the browser launches against this on-disk profile
    # directory. Cookies (including Cloudflare's cf_clearance) persist
    # between launches against the same dir, so D's clearance carries
    # forward to E1/E2 within a /scrape cascade. Threaded through to
    # crawl4ai.BrowserConfig and browser_use.BrowserConfig at launch time.
    # Set per-call via the cascade orchestrator (_do_scrape) — direct
    # callers of agent_extract / agent_browse can also pass it via
    # SCRAPPER_TOOL_AGENT_USER_DATA_DIR for cross-call session sharing.
    user_data_dir: str | None = None
    # Caller-supplied cookies for this one request, threaded to whichever tier
    # runs. Set per-call by the cascade orchestrator (_build_overrides), exactly
    # like user_data_dir above.
    #
    # Deliberately absent from ``from_env``: a cookie is a per-request identity,
    # and an ambient env-configured session would silently apply the same login
    # to every caller of a shared sidecar. ``value`` is a ``SecretStr``, so this
    # field stays masked in ``repr`` and ``model_dump``.
    cookies: list[CookieIn] | None = None
    # v1.5.0: CDP WebSocket URL for the Obscura backend (experimental Rust
    # headless browser run as an external ``obscura serve`` sidecar). Only
    # consulted when ``browser="obscura"``. Defaults to the conventional
    # local endpoint when unset.
    obscura_cdp_url: str | None = None
    # v1.6.0 render/stealth knobs. Camoufox-native — ignored by the other
    # backends. ``virtual`` runs under an Xvfb virtual display, which is
    # stealthier than pure headless (the Docker image ships xvfb).
    camoufox_headless_mode: CamoufoxDisplayName = "headless"
    # Drop image requests — large speed/bandwidth win when only text/DOM matters.
    # WARNING: Camoufox reports this can cause detection issues on major WAFs.
    # Enable for unprotected / speed-critical scrapes, NOT for hard targets.
    block_images: bool = False
    # Use a real bundled fingerprint instead of a generated one (Camoufox 0.5+).
    fingerprint_preset: bool = False
    # Target-audience matching, e.g. os="windows", locale="he-IL".
    camoufox_os: str | None = None
    camoufox_locale: str | None = None

    # --- LLM backend
    llm: LLMBackendName = "ollama"
    model: str = "qwen3-vl:8b"
    ollama_url: str = "http://localhost:11434"
    llm_api_key: SecretStr | None = None

    # --- Run budget
    max_steps: int = 50
    timeout_s: float = 120.0

    # --- Captcha cascade — auto-engages free OSS tiers, paid only if key set
    captcha_solver: CaptchaSolverName = "auto"
    captcha_api_key: SecretStr | None = None
    captcha_paid_fallback: PaidFallbackName = "capsolver"
    captcha_timeout_s: float = 120.0

    # --- ToS / safety
    respect_robots: bool = True

    @classmethod
    def from_env(cls) -> AgentConfig:
        """Construct config from ``SCRAPPER_TOOL_AGENT_*`` env vars.

        Missing vars fall through to dataclass-style defaults. Any vars
        with invalid values raise :class:`pydantic.ValidationError` —
        intentional, so misconfiguration is loud at startup rather than
        silent at run-time.
        """
        env = os.environ
        captcha_key = env.get("SCRAPPER_TOOL_CAPTCHA_KEY")
        llm_api_key = env.get("SCRAPPER_TOOL_AGENT_LLM_API_KEY")
        # Pydantic accepts plain str values for Literal-typed fields and
        # validates them, so we pass them through model_validate which
        # surfaces any invalid value with a clean error.
        return cls.model_validate(
            {
                "browser": env.get("SCRAPPER_TOOL_AGENT_BROWSER", "camoufox"),
                "fingerprint": env.get("SCRAPPER_TOOL_AGENT_FINGERPRINT", "browserforge"),
                "behavior": env.get("SCRAPPER_TOOL_AGENT_BEHAVIOR", "humanlike"),
                "headful": _envbool(env.get("SCRAPPER_TOOL_AGENT_HEADFUL"), default=False),
                "proxy": env.get("SCRAPPER_TOOL_AGENT_PROXY") or None,
                "user_data_dir": env.get("SCRAPPER_TOOL_AGENT_USER_DATA_DIR") or None,
                "obscura_cdp_url": env.get("SCRAPPER_TOOL_AGENT_OBSCURA_CDP_URL") or None,
                "camoufox_headless_mode": env.get(
                    "SCRAPPER_TOOL_AGENT_CAMOUFOX_DISPLAY", "headless"
                ),
                "block_images": _envbool(
                    env.get("SCRAPPER_TOOL_AGENT_BLOCK_IMAGES"), default=False
                ),
                "fingerprint_preset": _envbool(
                    env.get("SCRAPPER_TOOL_AGENT_FINGERPRINT_PRESET"), default=False
                ),
                "camoufox_os": env.get("SCRAPPER_TOOL_AGENT_CAMOUFOX_OS") or None,
                "camoufox_locale": env.get("SCRAPPER_TOOL_AGENT_CAMOUFOX_LOCALE") or None,
                "llm": env.get("SCRAPPER_TOOL_AGENT_LLM", "ollama"),
                "model": env.get("SCRAPPER_TOOL_AGENT_MODEL", "qwen3-vl:8b"),
                "ollama_url": env.get("SCRAPPER_TOOL_AGENT_OLLAMA_URL", "http://localhost:11434"),
                "llm_api_key": SecretStr(llm_api_key) if llm_api_key else None,
                "max_steps": int(env.get("SCRAPPER_TOOL_AGENT_MAX_STEPS", "50")),
                "timeout_s": float(env.get("SCRAPPER_TOOL_AGENT_TIMEOUT_S", "120")),
                "captcha_solver": env.get("SCRAPPER_TOOL_CAPTCHA_SOLVER", "auto"),
                "captcha_api_key": SecretStr(captcha_key) if captcha_key else None,
                "captcha_paid_fallback": env.get(
                    "SCRAPPER_TOOL_CAPTCHA_PAID_FALLBACK", "capsolver"
                ),
                "captcha_timeout_s": float(env.get("SCRAPPER_TOOL_CAPTCHA_TIMEOUT_S", "120")),
                "respect_robots": _envbool(
                    env.get("SCRAPPER_TOOL_AGENT_RESPECT_ROBOTS"), default=True
                ),
            }
        )

    def merged(self, **overrides: object) -> AgentConfig:
        """Return a copy with the given keyword overrides applied.

        Useful for ``agent_extract(..., model="qwen3-vl:4b")`` to spawn a
        per-call config without mutating the shared one.
        """
        return self.model_copy(update={k: v for k, v in overrides.items() if v is not None})


def _envbool(raw: str | None, *, default: bool) -> bool:
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


__all__ = [
    "ActionTrace",
    "AgentConfig",
    "AgentResult",
    "BehaviorName",
    "BrowserBackendName",
    "CamoufoxDisplayName",
    "CaptchaSolverName",
    "FingerprintName",
    "LLMBackendName",
    "PaidFallbackName",
]

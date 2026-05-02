"""Pluggable backends for Pattern E.

Re-exports the resolver functions so callers can do::

    from scrapper_tool.agent.backends import (
        get_browser_backend,
        get_llm_backend,
        get_captcha_solver,
        get_fingerprint_generator,
        get_behavior_policy,
    )
"""

from __future__ import annotations

from scrapper_tool.agent.backends.behavior import (
    BehaviorPolicy,
    FastPolicy,
    HumanlikePolicy,
    OffPolicy,
    get_behavior_policy,
)
from scrapper_tool.agent.backends.browser import (
    BotasaurusBackend,
    BrowserBackend,
    BrowserHandle,
    CamoufoxBackend,
    PatchrightBackend,
    ScraplingBackend,
    ZendriverBackend,
    get_browser_backend,
    open_browser,
)
from scrapper_tool.agent.backends.captcha import (
    AutoCascadeSolver,
    CamoufoxAutoSolver,
    CapSolverSolver,
    CaptchaKind,
    CaptchaSolver,
    NopechaSolver,
    NoSolver,
    TheykaSolver,
    TwoCaptchaSolver,
    get_captcha_solver,
)
from scrapper_tool.agent.backends.fingerprint import (
    BrowserforgeGenerator,
    FingerprintGenerator,
    GeneratedFingerprint,
    NoOpGenerator,
    get_fingerprint_generator,
)
from scrapper_tool.agent.backends.llm import (
    LlamaCppBackend,
    LLMBackend,
    OllamaBackend,
    OpenAICompatBackend,
    VLLMBackend,
    get_llm_backend,
    is_vision_model,
)

__all__ = [
    "AutoCascadeSolver",
    "BehaviorPolicy",
    "BotasaurusBackend",
    "BrowserBackend",
    "BrowserHandle",
    "BrowserforgeGenerator",
    "CamoufoxAutoSolver",
    "CamoufoxBackend",
    "CapSolverSolver",
    "CaptchaKind",
    "CaptchaSolver",
    "FastPolicy",
    "FingerprintGenerator",
    "GeneratedFingerprint",
    "HumanlikePolicy",
    "LLMBackend",
    "LlamaCppBackend",
    "NoOpGenerator",
    "NoSolver",
    "NopechaSolver",
    "OffPolicy",
    "OllamaBackend",
    "OpenAICompatBackend",
    "PatchrightBackend",
    "ScraplingBackend",
    "TheykaSolver",
    "TwoCaptchaSolver",
    "VLLMBackend",
    "ZendriverBackend",
    "get_behavior_policy",
    "get_browser_backend",
    "get_captcha_solver",
    "get_fingerprint_generator",
    "get_llm_backend",
    "is_vision_model",
    "open_browser",
]

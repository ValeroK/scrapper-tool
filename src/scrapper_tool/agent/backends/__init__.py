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
    BrowserBackend,
    BrowserHandle,
    BrowserLaunchOptions,
    CamoufoxBackend,
    ObscuraBackend,
    PatchrightBackend,
    ScraplingBackend,
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
    get_vision_backend,
    is_vision_model,
    supports_vision,
)
from scrapper_tool.agent.backends.page_hooks import (
    get_e2_page,
    make_after_goto,
    make_on_step_end,
)

__all__ = [
    "AutoCascadeSolver",
    "BehaviorPolicy",
    "BrowserBackend",
    "BrowserHandle",
    "BrowserLaunchOptions",
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
    "ObscuraBackend",
    "OffPolicy",
    "OllamaBackend",
    "OpenAICompatBackend",
    "PatchrightBackend",
    "ScraplingBackend",
    "TheykaSolver",
    "TwoCaptchaSolver",
    "VLLMBackend",
    "get_behavior_policy",
    "get_browser_backend",
    "get_captcha_solver",
    "get_e2_page",
    "get_fingerprint_generator",
    "get_llm_backend",
    "get_vision_backend",
    "is_vision_model",
    "make_after_goto",
    "make_on_step_end",
    "open_browser",
    "supports_vision",
]

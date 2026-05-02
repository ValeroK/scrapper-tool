"""Exception hierarchy for scrapper-tool.

::

    ScrapingError                 (base — all lib-specific exceptions inherit)
    ├── VendorHTTPError           (transport-error or 5xx/429 retry-exhaustion)
    │   └── VendorUnavailable     (alias for breaker call-sites)
    ├── BlockedError              (403 / Cloudflare challenge / Akamai EVA / Distil — anti-bot)
    ├── ParseError                (extractor couldn't find expected fields in the response)
    └── AgentError                (Pattern E LLM-agent failures)
        ├── AgentTimeoutError     (asyncio.wait_for exceeded)
        ├── AgentBlockedError     (also subclasses BlockedError — caught by existing handlers)
        ├── AgentLLMError         (Ollama/llama_cpp unreachable / model unavailable)
        ├── AgentSchemaError      (LLM output failed pydantic schema validation)
        └── CaptchaSolveError     (captcha solver returned an error or all solvers failed)

Consumers wrapping the lib in their own circuit breaker typically
trigger on ``VendorHTTPError`` / ``VendorUnavailable`` / ``BlockedError``
but *not* on ``ParseError`` — the latter is "our bug" (parser drift),
not "vendor down". See ``scrapper_tool.adapter`` (M7) for the generic
Adapter Protocol that codifies this distinction.

``AgentBlockedError`` deliberately multi-inherits from
:class:`BlockedError` so existing ``except BlockedError`` handlers in
consumer code keep working when callers escalate to Pattern E.
"""

from __future__ import annotations


class ScrapingError(Exception):
    """Base for all scrapper-tool exceptions."""


class VendorHTTPError(ScrapingError):
    """Raised when all retry attempts exhaust on a retriable failure.

    Non-retriable HTTP 4xx responses (other than 429) are NOT wrapped —
    the caller sees a plain response object via the underlying client.
    The distinction matters for circuit breakers: 4xx is "our bug",
    5xx/429/transport is "their fault".
    """


class VendorUnavailable(VendorHTTPError):
    """Alias of :class:`VendorHTTPError` for circuit-breaker call-sites.

    Subclasses :class:`VendorHTTPError` so existing handlers keep
    working; the dedicated name reads better at the breaker boundary.
    """


class BlockedError(ScrapingError):
    """Raised when an anti-bot platform blocks the request.

    Distinct from :class:`VendorHTTPError` because the remediation
    differs: the breaker should NOT trip (the vendor is up, just
    fingerprinting us). Consumer should escalate to the next ladder
    profile or to Pattern D (Scrapling).
    """


class ParseError(ScrapingError):
    """Raised when the extractor cannot find expected fields.

    Indicates parser drift (vendor changed markup) or a fixture-vs-live
    mismatch. NOT a circuit-breaker signal — re-fetching won't help.
    """


class ConfigurationError(ScrapingError):
    """Raised when a required component is missing or misconfigured locally.

    Examples: browser binary not found (patchright/camoufox not installed),
    required extra not installed (``[llm-agent]``), Ollama model not pulled
    yet. Distinct from :class:`AgentLLMError` (which covers live connectivity
    failures) — this is a static environment / install issue that the
    operator can fix without restarting any external service.

    The HTTP sidecar maps this to ``503 Service Unavailable`` with
    ``{"error": "configuration_error", "detail": "..."}`` so callers
    distinguish "scrapper-tool is misconfigured here" from "the target
    site is down" (502) or "the LLM is down" (502 ``llm_unreachable``).
    """


# --- Pattern E (LLM-agent) errors -----------------------------------------


class AgentError(ScrapingError):
    """Base for Pattern E (LLM-agent) failures.

    Distinct from :class:`VendorHTTPError` so circuit breakers can
    route LLM/agent-stage failures separately from transport failures.
    """


class AgentTimeoutError(AgentError):
    """Raised when ``agent_extract`` / ``agent_browse`` exceeds ``timeout_s``."""


class AgentBlockedError(AgentError, BlockedError):
    """Raised when the agent stage detects an unrecoverable anti-bot block.

    Multi-inherits :class:`BlockedError` so existing ``except BlockedError``
    handlers absorb agent-stage blocks transparently.
    """


class AgentLLMError(AgentError):
    """Raised when the LLM backend is unreachable or model is unavailable.

    Examples: Ollama daemon down, model not pulled, OpenAI-compat server
    refused connection, llama.cpp segfault. Distinct from
    :class:`VendorHTTPError` so a breaker can trip on "LLM down" without
    declaring the scraping vendor unavailable.
    """


class AgentSchemaError(AgentError):
    """Raised when the LLM's output cannot be validated against the schema.

    Note: in normal flow, agent_extract/agent_browse RETURN an
    ``AgentResult`` with ``error="schema-validation-failed"`` rather than
    raising. This exception is reserved for cases where the caller
    explicitly opts into strict mode (``raise_on_schema_error=True``).
    """


class CaptchaSolveError(AgentError):
    """Raised when the captcha solver cascade fails to solve a challenge.

    Aggregates the underlying cause (network error, no-key-set, vendor
    rejection) in ``args[0]`` and the original exception (if any) in
    ``__cause__``.
    """


__all__ = [
    "AgentBlockedError",
    "AgentError",
    "AgentLLMError",
    "AgentSchemaError",
    "AgentTimeoutError",
    "BlockedError",
    "CaptchaSolveError",
    "ConfigurationError",
    "ParseError",
    "ScrapingError",
    "VendorHTTPError",
    "VendorUnavailable",
]

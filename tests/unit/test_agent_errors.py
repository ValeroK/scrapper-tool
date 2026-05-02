"""Unit tests for the Pattern E exception hierarchy.

The most important contract: ``AgentBlockedError`` must be caught by
existing ``except BlockedError`` handlers — that's why it multi-inherits
from :class:`scrapper_tool.errors.BlockedError`.
"""

from __future__ import annotations

import pytest

from scrapper_tool.errors import (
    AgentBlockedError,
    AgentError,
    AgentLLMError,
    AgentSchemaError,
    AgentTimeoutError,
    BlockedError,
    CaptchaSolveError,
    ScrapingError,
)


class TestExceptionMRO:
    def test_agent_error_subclasses_scraping_error(self) -> None:
        assert issubclass(AgentError, ScrapingError)

    def test_agent_blocked_error_caught_by_blocked_error(self) -> None:
        with pytest.raises(BlockedError):
            raise AgentBlockedError("fingerprint detected")

    def test_agent_blocked_error_also_caught_by_agent_error(self) -> None:
        with pytest.raises(AgentError):
            raise AgentBlockedError("camoufox didn't pass")

    def test_agent_timeout_error_subclasses_agent_error(self) -> None:
        with pytest.raises(AgentError):
            raise AgentTimeoutError("ran out of time")

    def test_agent_llm_error_distinct_from_blocked(self) -> None:
        # An LLM-down failure should NOT be caught by BlockedError —
        # circuit breakers care about the distinction.
        with pytest.raises(AgentLLMError):
            raise AgentLLMError("ollama unreachable")
        with pytest.raises(AgentError):
            raise AgentLLMError("ollama unreachable")
        # And it shouldn't be a BlockedError.
        try:
            raise AgentLLMError("ollama")
        except BlockedError:
            pytest.fail("AgentLLMError must not subclass BlockedError")
        except AgentLLMError:
            pass

    def test_agent_schema_error_subclasses_agent_error(self) -> None:
        with pytest.raises(AgentError):
            raise AgentSchemaError("schema mismatch")

    def test_captcha_solve_error_is_agent_error(self) -> None:
        with pytest.raises(AgentError):
            raise CaptchaSolveError("vendor down")

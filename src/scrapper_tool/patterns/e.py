"""Pattern E — LLM-driven scraping (delegate module).

Re-exports the public API from :mod:`scrapper_tool.agent` so the
pattern alphabet (B, C, D, E) reads symmetrically. Implementation
lives in :mod:`scrapper_tool.agent` because Pattern E is a stateful
package, not a single-file extractor.

When to escalate to Pattern E
-----------------------------

Reach for Pattern E only when the existing ladder fails:

1. Pattern A/B/C return :class:`scrapper_tool.errors.BlockedError`.
2. Pattern D (:func:`scrapper_tool.patterns.d.hostile_client`) ALSO
   returns :class:`BlockedError` or doesn't render the data correctly
   (JS-heavy SPA, lazy-loaded content the fetcher misses).

For most "scrape protected listing/product page" cases, :func:`agent_extract`
(one local-LLM call) is the right tool. For "log in / paginate / fill a
form / click through dynamic UI", reach for :func:`agent_browse`.

See :mod:`scrapper_tool.agent` for the full surface and
``docs/patterns/e-llm-agent.md`` for tradeoffs and hardware sizing.
"""

from __future__ import annotations

from scrapper_tool.agent import (
    ActionTrace,
    AgentConfig,
    AgentResult,
    AgentSession,
    agent_browse,
    agent_extract,
    agent_session,
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

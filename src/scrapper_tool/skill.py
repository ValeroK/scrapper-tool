"""Locate the bundled agent skill so every surface can serve it.

The skill is the tool's own operating manual — what the cascade is, which
entrypoint to call, which flags exist and when they earn their cost. It has
existed since v1.0 and, until now, only ever reached agents that had the
*repository* checked out.

That is the discoverability gap this closes. A heavy consumer reverse-engineered
the request flags from ``/openapi.json`` by hand and discovered the valid browser
backends only from the text of an error message, because nothing over the wire
said what the tool could do. Both the HTTP sidecar and the MCP server can now
hand an agent the manual directly.

Resolution order, first hit wins:

1. ``SCRAPPER_TOOL_SKILL_PATH`` — an explicit override, for deployments that
   vendor their own house rules on top of the shipped skill.
2. Package data inside the installed wheel.
3. Repository / image layout (``<root>/skills/scrapper-tool/SKILL.md``), which is
   what a source checkout and the Docker image both look like.

Returns ``None`` rather than raising when the file is genuinely absent: a build
that omitted it should degrade to "no manual available", never fail a scrape.
"""

from __future__ import annotations

import os
from pathlib import Path

from scrapper_tool._logging import get_logger

_logger = get_logger(__name__)

_SKILL_RELATIVE = Path("skills") / "scrapper-tool" / "SKILL.md"
# Bounded so a pathological layout cannot make this walk to the filesystem root.
_MAX_PARENTS = 4


def _candidates() -> list[Path]:
    override = os.environ.get("SCRAPPER_TOOL_SKILL_PATH", "").strip()
    found: list[Path] = [Path(override)] if override else []
    here = Path(__file__).resolve()
    # Installed as package data (src/scrapper_tool/skills/...), then the source
    # tree and Docker image layout (/app/skills/...).
    found.append(here.parent / _SKILL_RELATIVE)
    found.extend(parent / _SKILL_RELATIVE for parent in list(here.parents)[:_MAX_PARENTS])
    return found


def skill_path() -> Path | None:
    """Filesystem path to the bundled SKILL.md, or None if this build lacks it."""
    for candidate in _candidates():
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - unreadable mount
            continue
    return None


def skill_markdown() -> str | None:
    """The skill's markdown, or None when the build does not carry it."""
    path = skill_path()
    if path is None:
        _logger.info(
            "skill.not_bundled",
            detail="no SKILL.md found; set SCRAPPER_TOOL_SKILL_PATH to serve one",
        )
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - unreadable mount
        _logger.warning("skill.read_failed", path=str(path), error=str(exc))
        return None


__all__ = ["skill_markdown", "skill_path"]

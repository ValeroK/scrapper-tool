"""Per-domain browser profiles, so a clearance is won once instead of every time.

Clearing a wall is the most expensive thing this tool does. A local vision solve
was measured at roughly 70 s of inference, and the credential it buys -- a
``cf_clearance`` cookie, typically valid for around half an hour -- was discarded
the moment the browser closed. Every subsequent request to that domain re-fought
the same wall from scratch.

The mechanism to keep it already existed and was opt-in: a caller could pass
``persist_browser_profile_dir``. It defaulted to ``None``, so in practice nobody
did, and the cost was paid per request rather than per domain.

``_harvest_cookies`` records five reasons not to put clearances in the recipe
store, and every one of them is correct. This is a different store built to
answer them:

* **Not world-readable.** Created ``0700`` under the user's own cache directory,
  not a shared temp path.
* **Keyed by domain AND by identity.** A profile is only ever reused for a
  request that carried no caller cookies -- see :func:`clearance_dir_for`. An
  anonymous clearance is not a session; a logged-in one is, and those never share.
* **A clearance's own TTL**, ~30 minutes, not the recipe store's 14 days. An
  expired profile is deleted rather than reused.
* **Failure is silent and safe.** Every error degrades to "no shared profile",
  which is exactly the old per-request behaviour.

What it deliberately does not do is share across *users of this sidecar*. There
is one cache root per OS user, which is the same trust boundary the cookie jar
and the recipe store already assume.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path

from scrapper_tool._logging import get_logger
from scrapper_tool.recipe.derive import registrable_domain

_logger = get_logger(__name__)

#: A cf_clearance is typically good for ~30 minutes. Reusing a profile past that
#: buys nothing and risks presenting a stale credential, so it is deleted.
_DEFAULT_TTL_S = 1_800.0

_UNSAFE = str.maketrans({c: "_" for c in ':/\\?*"<>|'})


def clearance_enabled() -> bool:
    """On by default; ``SCRAPPER_TOOL_CLEARANCE_REUSE=0`` disables."""
    raw = os.environ.get("SCRAPPER_TOOL_CLEARANCE_REUSE")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def clearance_ttl_s() -> float:
    """How long a shared profile may be reused before it is discarded."""
    raw = os.environ.get("SCRAPPER_TOOL_CLEARANCE_TTL_S", "")
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TTL_S
    return value if value > 0 else _DEFAULT_TTL_S


def clearance_root() -> Path:
    """Where per-domain profiles live. ``0700``, under the user's cache."""
    override = os.environ.get("SCRAPPER_TOOL_CLEARANCE_DIR", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "scrapper-tool" / "clearance"


def clearance_dir_for(url: str, *, has_caller_cookies: bool) -> Path | None:
    """A reusable profile directory for ``url``'s domain, or None.

    ``has_caller_cookies`` is the identity gate and it is not optional. A request
    carrying the caller's cookies is acting as somebody; its profile is a session
    and must never be shared with another request, because the worst case of a
    *successful* read is impersonating the wrong user. A request with no cookies
    is anonymous, and the only thing worth keeping from it is the clearance --
    which is exactly what this is for.

    Returns None whenever reuse is disabled, the domain cannot be derived, or the
    directory cannot be created. Every one of those degrades to the old
    per-request behaviour rather than to an error.
    """
    if has_caller_cookies or not clearance_enabled():
        return None
    domain = registrable_domain(url)
    if not domain:
        return None

    root = clearance_root()
    path = root / domain.translate(_UNSAFE)
    try:
        if _expired(path):
            _discard(path)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.mkdir(exist_ok=True, mode=0o700)
    except OSError as exc:
        _logger.debug("clearance.unavailable", domain=domain, error=str(exc)[:160])
        return None
    return path


def _expired(path: Path) -> bool:
    """Whether a profile is older than the clearance it was meant to carry."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age > clearance_ttl_s()


def _discard(path: Path) -> None:
    """Delete a stale profile. A failure here just means it is reused once more."""
    try:
        shutil.rmtree(path, ignore_errors=True)
        _logger.info("clearance.expired", path=str(path))
    except OSError as exc:  # pragma: no cover - rmtree already ignores errors
        _logger.debug("clearance.discard_failed", path=str(path), error=str(exc)[:160])


def touch(path: Path) -> None:
    """Mark a profile as freshly used, restarting its TTL.

    Called after a render that reached content, so a domain in continuous use
    keeps its clearance instead of expiring mid-crawl on wall-clock age alone.
    """
    with contextlib.suppress(OSError):  # best effort by design
        path.touch(exist_ok=True)


__all__ = [
    "clearance_dir_for",
    "clearance_enabled",
    "clearance_root",
    "clearance_ttl_s",
    "touch",
]

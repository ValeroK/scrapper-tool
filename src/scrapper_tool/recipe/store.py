"""Per-domain recipe cache.

A JSON-file store by default — one file per domain under a cache dir — behind a
small interface so a Redis/SQLite backend can be dropped in later without
touching the cascade.

Deliberately fault-tolerant in one direction only: **every read failure is
silent and returns a miss**. A corrupt file, a permissions problem, a
half-written entry from a crashed process — none of these should break a scrape,
because the worst case of a cache miss is that this request pays full price,
which is exactly what would have happened without a cache at all. Write failures
are logged but never raised, for the same reason.

The cache key is the registrable domain plus, when the caller supplied one, a
fingerprint of the requested schema shape: two callers wanting different fields
from the same site must not share a recipe.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from scrapper_tool._logging import get_logger
from scrapper_tool.recipe.derive import Recipe, registrable_domain, schema_fingerprint

_logger = get_logger(__name__)

# Recipes go stale as sites change. Drift detection (C4) is the real safety net;
# this is just a backstop so a cache left alone for months isn't trusted blindly.
_DEFAULT_TTL_S = 14 * 24 * 3600.0

# Cache keys become filenames, so anything outside this set is replaced. Without
# it a crafted host could escape the cache dir via path separators.
_UNSAFE_KEY_CHARS = re.compile(r"[^a-z0-9._-]+")


def default_cache_dir() -> Path:
    """Cache location: ``SCRAPPER_TOOL_RECIPE_DIR`` or a temp-dir default."""
    override = os.environ.get("SCRAPPER_TOOL_RECIPE_DIR")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "scrapper-tool-recipes"


def recipe_cache_enabled() -> bool:
    """On by default; ``SCRAPPER_TOOL_RECIPE_CACHE=0`` disables."""
    raw = os.environ.get("SCRAPPER_TOOL_RECIPE_CACHE")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cache_key(url: str, schema: Any = None) -> str:
    """``domain`` or ``domain@schemahash`` — see the module docstring."""
    domain = registrable_domain(url) or "unknown"
    if isinstance(schema, dict) and schema:
        return f"{domain}@{schema_fingerprint(schema)}"
    return domain


class RecipeStore(Protocol):
    """Interface a recipe backend must satisfy."""

    def get(self, key: str) -> Recipe | None: ...

    def put(self, key: str, recipe: Recipe) -> None: ...

    def invalidate(self, key: str) -> bool: ...


class JsonFileRecipeStore:
    """One JSON file per cache key. No locking, by design.

    Concurrent writers race, and the loser's recipe is overwritten — which is
    fine, because both wrote a *verified* recipe for the same domain. Writes are
    atomic (temp file + replace) so a reader never sees a half-written file; that
    is the only guarantee actually needed here.
    """

    def __init__(self, directory: Path | str | None = None, *, ttl_s: float | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_cache_dir()
        self.ttl_s = _DEFAULT_TTL_S if ttl_s is None else ttl_s

    def _path(self, key: str) -> Path:
        safe = _UNSAFE_KEY_CHARS.sub("_", key.lower()).strip("._-") or "unknown"
        return self.directory / f"{safe}.json"

    def get(self, key: str, *, now: datetime | None = None) -> Recipe | None:
        path = self._path(key)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            recipe = Recipe.from_dict(raw)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Corrupt or unreadable: treat as a miss and clear it, so one bad
            # write doesn't permanently pin a domain to the slow path.
            _logger.info("recipe.store.unreadable", key=key, error=str(exc)[:120])
            self.invalidate(key)
            return None
        if self._expired(recipe, now=now):
            _logger.info("recipe.store.expired", key=key, created_at=recipe.created_at)
            self.invalidate(key)
            return None
        return recipe

    def _expired(self, recipe: Recipe, *, now: datetime | None) -> bool:
        if self.ttl_s <= 0:
            return False
        try:
            created = datetime.fromisoformat(recipe.created_at)
        except ValueError:
            return True  # unparseable timestamp: don't trust it
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        return (current - created).total_seconds() > self.ttl_s

    def put(self, key: str, recipe: Recipe) -> None:
        path = self._path(key)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # Atomic swap so a concurrent reader never sees a partial file.
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.directory, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(recipe.to_dict(), handle, ensure_ascii=False, indent=2)
                temp_path = Path(handle.name)
            temp_path.replace(path)
        except OSError as exc:
            # A cache we can't write to just means full price next time.
            _logger.warning("recipe.store.write_failed", key=key, error=str(exc)[:120])
            return
        _logger.info("recipe.store.stored", key=key, tier=recipe.source_tier)

    def invalidate(self, key: str) -> bool:
        try:
            self._path(key).unlink()
        except (FileNotFoundError, OSError):
            return False
        _logger.info("recipe.store.invalidated", key=key)
        return True

    def clear(self) -> int:
        """Drop every cached recipe. Returns how many were removed."""
        removed = 0
        try:
            entries = list(self.directory.glob("*.json"))
        except OSError:
            return 0
        for entry in entries:
            try:
                entry.unlink()
                removed += 1
            except OSError:
                continue  # one unremovable file must not abort the sweep
        return removed


_default_store: JsonFileRecipeStore | None = None


def get_store() -> JsonFileRecipeStore:
    """Process-wide default store (lazily created)."""
    global _default_store  # noqa: PLW0603 — one process-wide cache handle
    if _default_store is None:
        _default_store = JsonFileRecipeStore()
    return _default_store


def set_store(store: JsonFileRecipeStore | None) -> None:
    """Swap the default store. Pass None to reset (used by tests)."""
    global _default_store  # noqa: PLW0603
    _default_store = store


__all__ = [
    "JsonFileRecipeStore",
    "RecipeStore",
    "cache_key",
    "default_cache_dir",
    "get_store",
    "recipe_cache_enabled",
    "set_store",
]

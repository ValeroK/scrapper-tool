"""Unit tests for the per-domain recipe cache (C2).

The store's governing rule: **a cache problem must never break a scrape.** Every
read failure degrades to a miss, which costs one full-price request — exactly
what would have happened with no cache at all. Most of these tests exist to pin
that, because the tempting implementation (raise on corrupt JSON, raise on an
unwritable dir) turns a cache into a new failure mode.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scrapper_tool.recipe.derive import Recipe
from scrapper_tool.recipe.store import (
    JsonFileRecipeStore,
    cache_key,
    default_cache_dir,
    get_store,
    recipe_cache_enabled,
    set_store,
)

_SCHEMA = {
    "baseSelector": "div.card",
    "fields": [
        {"name": "title", "selector": "h2", "type": "text"},
        {"name": "price", "selector": "span.p", "type": "text"},
    ],
}


def _recipe(*, domain: str = "example.com", created: datetime | None = None) -> Recipe:
    return Recipe(
        domain=domain,
        schema=_SCHEMA,
        source_tier="render",
        sample_url=f"https://{domain}/p",
        multi_row=True,
        created_at=(created or datetime.now(UTC)).isoformat(),
        schema_hash="deadbeef",
        field_names=("title", "price"),
    )


@pytest.fixture
def store(tmp_path: Path) -> JsonFileRecipeStore:
    return JsonFileRecipeStore(tmp_path)


# --- round trip -------------------------------------------------------------


def test_put_then_get(store: JsonFileRecipeStore) -> None:
    written = _recipe()
    store.put("example.com", written)
    assert store.get("example.com") == written


def test_get_missing_is_a_miss_not_an_error(store: JsonFileRecipeStore) -> None:
    assert store.get("never-seen.com") is None


def test_invalidate(store: JsonFileRecipeStore) -> None:
    store.put("example.com", _recipe())
    assert store.invalidate("example.com") is True
    assert store.get("example.com") is None
    assert store.invalidate("example.com") is False


def test_put_overwrites(store: JsonFileRecipeStore) -> None:
    store.put("example.com", _recipe())
    newer = Recipe(**{**_recipe().to_dict(), "source_tier": "e1"})
    store.put("example.com", newer)
    got = store.get("example.com")
    assert got is not None
    assert got.source_tier == "e1"


def test_clear_removes_everything(store: JsonFileRecipeStore) -> None:
    store.put("a.com", _recipe(domain="a.com"))
    store.put("b.com", _recipe(domain="b.com"))
    assert store.clear() == 2
    assert store.get("a.com") is None


# --- failures degrade to a miss, never an exception -------------------------


def test_corrupt_file_is_a_miss_and_self_heals(store: JsonFileRecipeStore) -> None:
    """A half-written file must not pin a domain to the slow path forever."""
    store.put("example.com", _recipe())
    path = next(store.directory.glob("*.json"))
    path.write_text("{not json at all", encoding="utf-8")

    assert store.get("example.com") is None
    assert not path.exists(), "the unreadable entry should be cleared, not left to fail again"

    store.put("example.com", _recipe())
    assert store.get("example.com") is not None


def test_valid_json_with_the_wrong_shape_is_a_miss(store: JsonFileRecipeStore) -> None:
    store.put("example.com", _recipe())
    path = next(store.directory.glob("*.json"))
    path.write_text(json.dumps({"unrelated": True}), encoding="utf-8")
    assert store.get("example.com") is None


def test_unwritable_directory_does_not_raise(tmp_path: Path) -> None:
    """A cache we can't write to just means full price next time."""
    # A *file* where the cache dir should be: mkdir fails on every platform.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    store = JsonFileRecipeStore(blocker)

    store.put("example.com", _recipe())  # must not raise
    assert store.get("example.com") is None


def test_clear_on_a_missing_directory_returns_zero(tmp_path: Path) -> None:
    assert JsonFileRecipeStore(tmp_path / "nope").clear() == 0


# --- expiry -----------------------------------------------------------------


def test_expired_recipe_is_dropped(store: JsonFileRecipeStore) -> None:
    store.put("example.com", _recipe(created=datetime.now(UTC) - timedelta(days=40)))
    assert store.get("example.com") is None


def test_fresh_recipe_survives(store: JsonFileRecipeStore) -> None:
    store.put("example.com", _recipe(created=datetime.now(UTC) - timedelta(hours=2)))
    assert store.get("example.com") is not None


def test_ttl_zero_disables_expiry(tmp_path: Path) -> None:
    store = JsonFileRecipeStore(tmp_path, ttl_s=0)
    store.put("example.com", _recipe(created=datetime(2000, 1, 1, tzinfo=UTC)))
    assert store.get("example.com") is not None


def test_unparseable_timestamp_is_treated_as_expired(store: JsonFileRecipeStore) -> None:
    """Can't prove it's fresh, so don't trust it."""
    store.put("example.com", Recipe(**{**_recipe().to_dict(), "created_at": "whenever"}))
    assert store.get("example.com") is None


def test_naive_timestamp_is_assumed_utc(store: JsonFileRecipeStore) -> None:
    """A hand-written or older entry without a timezone must not blow up."""
    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
    store.put("example.com", Recipe(**{**_recipe().to_dict(), "created_at": naive}))
    assert store.get("example.com") is not None


# --- keys -------------------------------------------------------------------


def test_cache_key_ignores_www_and_case() -> None:
    assert cache_key("https://www.Example.com/a") == cache_key("https://example.com/b")


def test_cache_key_separates_different_requested_schemas() -> None:
    """Two callers wanting different fields from one site can't share a recipe."""
    a = cache_key("https://x.co/p", {"fields": ["title"]})
    b = cache_key("https://x.co/p", {"fields": ["price"]})
    assert a != b
    assert a != cache_key("https://x.co/p")


def test_path_traversal_in_a_key_stays_inside_the_cache_dir(tmp_path: Path) -> None:
    """Keys derive from a hostname, so a hostile one must not escape the dir."""
    store = JsonFileRecipeStore(tmp_path)
    store.put("../../etc/passwd", _recipe())
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    assert written[0].parent == tmp_path
    assert store.get("../../etc/passwd") is not None


# --- configuration ----------------------------------------------------------


def test_cache_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRAPPER_TOOL_RECIPE_CACHE", raising=False)
    assert recipe_cache_enabled() is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("0", False), ("no", False), ("", True)],
)
def test_cache_toggle(monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_CACHE", value)
    assert recipe_cache_enabled() is expected


def test_cache_dir_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_DIR", str(tmp_path / "custom"))
    assert default_cache_dir() == tmp_path / "custom"


def test_default_store_is_swappable(tmp_path: Path) -> None:
    custom = JsonFileRecipeStore(tmp_path)
    set_store(custom)
    try:
        assert get_store() is custom
    finally:
        set_store(None)

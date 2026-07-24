"""Unit tests for recipe replay and learning (C3/C4), at the module level.

The cascade tests cover the wiring; these cover the decisions the module makes on
its own — which are the ones that decide whether replay is cheap, correct, or
silently wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool.recipe.derive import Recipe, derive_recipe
from scrapper_tool.recipe.replay import (
    apply_recipe,
    learn_from_success,
    try_replay,
)
from scrapper_tool.recipe.store import JsonFileRecipeStore, cache_key

_LISTING = (
    '<html><body><div class="feed">'
    '<div class="feed-item"><h2 class="t">Mazda 3</h2><span class="p">45,000</span></div>'
    '<div class="feed-item"><h2 class="t">Toyota</h2><span class="p">52,000</span></div>'
    "</div></body></html>"
)
_ROWS = [{"title": "Mazda 3", "price": "45,000"}, {"title": "Toyota", "price": "52,000"}]

# The same data, but only present after JS runs — a raw fetch sees the shell.
_SHELL = '<html><body><div id="root"></div></body></html>'


@pytest.fixture
def store(tmp_path: Any) -> JsonFileRecipeStore:
    return JsonFileRecipeStore(tmp_path)


def _fetcher(html: str, status: int = 200, url: str = "https://x.test/p") -> Any:
    async def fetch() -> tuple[str, int, str]:
        return html, status, url

    return fetch


# --- the source_tier decision ----------------------------------------------


def test_a_fetch_learned_recipe_needs_no_browser(store: JsonFileRecipeStore) -> None:
    recipe = learn_from_success(
        "https://x.test/p", _LISTING, _ROWS, source_tier="a_b_c", store=store
    )
    assert recipe is not None
    assert recipe.needs_render is False


def test_a_render_learned_recipe_needs_a_browser(store: JsonFileRecipeStore) -> None:
    recipe = learn_from_success(
        "https://x.test/p", _LISTING, _ROWS, source_tier="render", store=store
    )
    assert recipe is not None
    assert recipe.needs_render is True


def test_downgrade_when_the_cheap_body_also_matches(store: JsonFileRecipeStore) -> None:
    """A render can win for reasons unrelated to JS.

    It cleared a bot wall, or the page only looked unhydrated. Then the selectors
    work on the raw HTTP body, and pinning the recipe to "render" would make every
    future replay pay for a browser it doesn't need. The downgrade is proved by
    running the derived schema against the body the cheap tier already had.
    """
    recipe = learn_from_success(
        "https://x.test/p",
        _LISTING,
        _ROWS,
        source_tier="render",
        store=store,
        cheap_html=_LISTING,
    )
    assert recipe is not None
    assert recipe.source_tier == "a_b_c"
    assert recipe.needs_render is False


def test_no_downgrade_when_the_page_genuinely_needs_js(store: JsonFileRecipeStore) -> None:
    """The raw body is an empty shell, so the selectors find nothing in it."""
    recipe = learn_from_success(
        "https://x.test/p",
        _LISTING,
        _ROWS,
        source_tier="render",
        store=store,
        cheap_html=_SHELL,
    )
    assert recipe is not None
    assert recipe.source_tier == "render"


def test_no_downgrade_without_a_cheap_body(store: JsonFileRecipeStore) -> None:
    """A/B/C was blocked, so there's nothing to prove the downgrade with."""
    recipe = learn_from_success(
        "https://x.test/p", _LISTING, _ROWS, source_tier="render", store=store, cheap_html=None
    )
    assert recipe is not None
    assert recipe.source_tier == "render"


# --- replay -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_hit_returns_the_rows(store: JsonFileRecipeStore) -> None:
    learn_from_success("https://x.test/p", _LISTING, _ROWS, source_tier="a_b_c", store=store)
    outcome = await try_replay("https://x.test/p", fetch=_fetcher(_LISTING), store=store)
    assert outcome is not None
    assert outcome.rows == _ROWS


@pytest.mark.asyncio
async def test_replay_miss_on_a_cold_cache(store: JsonFileRecipeStore) -> None:
    assert await try_replay("https://x.test/p", fetch=_fetcher(_LISTING), store=store) is None


@pytest.mark.asyncio
async def test_a_render_recipe_uses_the_render_fetcher(store: JsonFileRecipeStore) -> None:
    learn_from_success("https://x.test/p", _LISTING, _ROWS, source_tier="render", store=store)
    used: list[str] = []

    async def fetch() -> tuple[str, int, str]:
        used.append("fetch")
        return _SHELL, 200, "https://x.test/p"

    async def render() -> tuple[str, int, str]:
        used.append("render")
        return _LISTING, 200, "https://x.test/p"

    outcome = await try_replay("https://x.test/p", fetch=fetch, render=render, store=store)
    assert outcome is not None
    assert used == ["render"], "a render recipe must not be replayed over a raw fetch"


@pytest.mark.asyncio
async def test_a_render_recipe_declines_rather_than_faking_drift(
    store: JsonFileRecipeStore,
) -> None:
    """No render available: a raw fetch would find nothing and look like drift.

    Evicting a perfectly good recipe because the browser was switched off would
    make the cache lose everything it learned the first time [llm-agent] was
    absent.
    """
    learn_from_success("https://x.test/p", _LISTING, _ROWS, source_tier="render", store=store)
    key = cache_key("https://x.test/p")

    outcome = await try_replay("https://x.test/p", fetch=_fetcher(_SHELL), render=None, store=store)
    assert outcome is None
    assert store.get(key) is not None, "declining must not evict"


@pytest.mark.asyncio
async def test_drift_evicts_the_recipe(store: JsonFileRecipeStore) -> None:
    learn_from_success("https://x.test/p", _LISTING, _ROWS, source_tier="a_b_c", store=store)
    key = cache_key("https://x.test/p")
    changed = _LISTING.replace("feed-item", "listing-card")

    assert await try_replay("https://x.test/p", fetch=_fetcher(changed), store=store) is None
    assert store.get(key) is None


@pytest.mark.asyncio
async def test_a_fetch_failure_is_a_miss_not_an_eviction(store: JsonFileRecipeStore) -> None:
    """A transient network error says nothing about whether the recipe is stale."""
    learn_from_success("https://x.test/p", _LISTING, _ROWS, source_tier="a_b_c", store=store)
    key = cache_key("https://x.test/p")

    async def failing() -> tuple[str, int, str]:
        raise RuntimeError("connection reset")

    assert await try_replay("https://x.test/p", fetch=failing, store=store) is None
    assert store.get(key) is not None


@pytest.mark.asyncio
async def test_replay_respects_the_cache_toggle(
    store: JsonFileRecipeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    learn_from_success("https://x.test/p", _LISTING, _ROWS, source_tier="a_b_c", store=store)
    monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_CACHE", "0")
    assert await try_replay("https://x.test/p", fetch=_fetcher(_LISTING), store=store) is None


def test_learning_respects_the_cache_toggle(
    store: JsonFileRecipeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCRAPPER_TOOL_RECIPE_CACHE", "0")
    assert (
        learn_from_success("https://x.test/p", _LISTING, _ROWS, source_tier="a_b_c", store=store)
        is None
    )


def test_learning_a_page_with_no_derivable_recipe_is_not_an_error(
    store: JsonFileRecipeStore,
) -> None:
    assert (
        learn_from_success("https://x.test/p", _SHELL, _ROWS, source_tier="render", store=store)
        is None
    )


# --- apply_recipe -----------------------------------------------------------


def test_apply_recipe_on_matching_html() -> None:
    recipe = derive_recipe(_LISTING, _ROWS, source_tier="a_b_c", url="https://x.test/p")
    assert recipe is not None
    assert apply_recipe(recipe, _LISTING) == _ROWS


def test_apply_recipe_returns_empty_on_a_changed_page() -> None:
    recipe = derive_recipe(_LISTING, _ROWS, source_tier="a_b_c", url="https://x.test/p")
    assert recipe is not None
    assert apply_recipe(recipe, _SHELL) == []


def test_apply_recipe_on_empty_html() -> None:
    recipe = Recipe(
        domain="x.test",
        schema={"baseSelector": "div", "fields": [{"name": "a", "selector": "p"}]},
        source_tier="a_b_c",
        sample_url="https://x.test/p",
        multi_row=True,
        created_at="2026-01-01T00:00:00+00:00",
        schema_hash="x",
    )
    assert apply_recipe(recipe, "") == []

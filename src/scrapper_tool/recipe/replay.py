"""Replay a cached recipe (C3) and learn one from a success (C3.2).

Tier 0 of the cascade. A cache hit turns what cost a browser launch or an LLM
call into a fetch plus a selectolax parse.

Two things this module is careful about:

**Replaying at the right tier.** A recipe learned from a rendered DOM targets
nodes that only exist after JS runs. Replaying it over a raw HTTP fetch would
return nothing and look exactly like site drift — so ``Recipe.source_tier``
decides how the page is fetched, and a render-learned recipe with no render
function available is a miss rather than a wrong answer.

**Treating "no rows" as drift, not failure.** A recipe that stops matching means
the site changed. The recipe is invalidated on the spot and the caller falls
through to the normal cascade, which re-derives a fresh one. That self-heal is
why caching a heuristic is safe: a stale recipe costs one wasted fetch, once.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from scrapper_tool._logging import get_logger
from scrapper_tool.recipe.derive import Recipe, derive_recipe
from scrapper_tool.recipe.store import (
    JsonFileRecipeStore,
    cache_key,
    get_store,
    recipe_cache_enabled,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_logger = get_logger(__name__)


@dataclass(frozen=True)
class ReplayOutcome:
    """A successful replay."""

    rows: list[dict[str, Any]]
    html: str
    status: int
    final_url: str
    recipe: Recipe


async def try_replay(
    url: str,
    *,
    fetch: Callable[[], Awaitable[tuple[str, int, str]]],
    render: Callable[[], Awaitable[tuple[str, int, str]]] | None = None,
    schema_json: Any = None,
    store: JsonFileRecipeStore | None = None,
) -> ReplayOutcome | None:
    """Replay this domain's cached recipe, or return None to run the cascade.

    None covers every "just do it the normal way" case — cache disabled, no
    recipe, no way to fetch at the recipe's tier, the fetch failed, or the
    recipe drifted. Only the last one has a side effect: the stale recipe is
    invalidated so the cascade's re-derivation replaces it.
    """
    if not recipe_cache_enabled():
        return None
    backing = store or get_store()
    key = cache_key(url, schema_json)
    recipe = backing.get(key)
    if recipe is None:
        return None

    fetcher = fetch
    if recipe.needs_render:
        if render is None:
            # No browser available; a raw fetch would return nothing and be
            # misread as drift, evicting a recipe that is probably fine.
            _logger.info("recipe.replay.skipped_needs_render", key=key)
            return None
        fetcher = render

    try:
        html, status, final_url = await fetcher()
    except Exception as exc:
        _logger.info("recipe.replay.fetch_failed", key=key, error=str(exc)[:160])
        return None

    rows = apply_recipe(recipe, html)
    if not rows:
        _logger.info("recipe.replay.drift", key=key, base=recipe.schema.get("baseSelector"))
        backing.invalidate(key)
        return None

    _logger.info("recipe.replay.hit", key=key, tier=recipe.source_tier, rows=len(rows))
    return ReplayOutcome(rows=rows, html=html, status=status, final_url=final_url, recipe=recipe)


def apply_recipe(recipe: Recipe, html: str) -> list[dict[str, Any]]:
    """Run a recipe's schema over HTML. Empty list means it no longer matches."""
    from scrapper_tool._extractors import get as get_extractor  # noqa: PLC0415

    result = get_extractor("css").extract(html, options={"schema": recipe.schema})
    if not result.has_signal or not isinstance(result.data, list):
        return []
    return [row for row in result.data if isinstance(row, dict)]


def learn_from_success(
    url: str,
    html: str,
    data: Any,
    *,
    source_tier: str,
    schema_json: Any = None,
    store: JsonFileRecipeStore | None = None,
    cheap_html: str | None = None,
) -> Recipe | None:
    """Derive and cache a recipe from a tier that just succeeded.

    ``cheap_html`` is the raw HTTP body an earlier, cheaper tier already fetched
    (when it had one). If the derived selectors also match *that*, the recipe is
    downgraded to replay over a plain fetch — see :func:`_downgraded`.

    Best-effort by design: any failure returns None and is logged, never raised.
    Learning is an optimisation for the *next* request, so it must not be able
    to fail the current one — the caller already has its answer.
    """
    if not recipe_cache_enabled():
        return None
    try:
        recipe = derive_recipe(html, data, source_tier=source_tier, url=url)
        if recipe is None:
            return None
        recipe = _downgraded(recipe, cheap_html)
        (store or get_store()).put(cache_key(url, schema_json), recipe)
    except Exception as exc:
        _logger.warning("recipe.learn.failed", url=url, error=str(exc)[:160])
        return None
    return recipe


def _downgraded(recipe: Recipe, cheap_html: str | None) -> Recipe:
    """Mark a browser-learned recipe as fetch-replayable when it provably is.

    A render tier can win for reasons that have nothing to do with JS — a bot
    wall the browser cleared, an unhydrated-looking page whose markup was
    actually there all along. In those cases the selectors work fine against the
    raw HTTP body, and pinning the recipe to ``render`` would make every future
    replay pay for a browser it doesn't need.

    So this doesn't guess: it runs the derived schema against the HTML the cheap
    tier already fetched and only downgrades on a match. When the page really
    does need JS, nothing matches and the recipe stays a render recipe.
    """
    if not recipe.needs_render or not cheap_html:
        return recipe
    if not apply_recipe(recipe, cheap_html):
        return recipe
    _logger.info(
        "recipe.learn.downgraded_to_fetch",
        domain=recipe.domain,
        was=recipe.source_tier,
        detail="selectors also match the raw HTTP body, so replay needs no browser",
    )
    return replace(recipe, source_tier="a_b_c")


__all__ = [
    "ReplayOutcome",
    "apply_recipe",
    "learn_from_success",
    "try_replay",
]

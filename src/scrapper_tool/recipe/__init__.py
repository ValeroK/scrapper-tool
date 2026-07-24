"""Learn-once / replay: turn one expensive success into a cheap recipe.

The cost problem this solves: on repeat-vendor scraping, every page pays the
same price as the first. A render costs a browser launch; an E1 extraction costs
an LLM call. But the *second* page from the same vendor has the same DOM shape as
the first, so paying twice buys nothing.

So: when an expensive tier succeeds, derive a CSS recipe that reproduces its
output (:mod:`~scrapper_tool.recipe.derive`), cache it per domain
(:mod:`~scrapper_tool.recipe.store`), and replay it deterministically on the
next hit. A cached recipe turns an LLM call into a selectolax parse.

Deliberately NOT derived for JSON-LD/microdata wins: Pattern B already extracts
those deterministically at tier 1, so a CSS recipe would be strictly more
fragile for zero gain. Derivation is value-matched against *visible* DOM text
(``<script>`` contents excluded), which makes that fall out naturally — data
that only exists inside a JSON-LD block yields no recipe, correctly.
"""

from __future__ import annotations

from scrapper_tool.recipe.derive import Recipe, derive_recipe, registrable_domain
from scrapper_tool.recipe.policy import (
    DomainPolicy,
    DomainPolicyStore,
    domain_policy_enabled,
    get_policy_store,
    set_policy_store,
)
from scrapper_tool.recipe.store import (
    JsonFileRecipeStore,
    RecipeStore,
    cache_key,
    get_store,
    recipe_cache_enabled,
    set_store,
)

__all__ = [
    "DomainPolicy",
    "DomainPolicyStore",
    "JsonFileRecipeStore",
    "Recipe",
    "RecipeStore",
    "cache_key",
    "derive_recipe",
    "domain_policy_enabled",
    "get_policy_store",
    "get_store",
    "recipe_cache_enabled",
    "registrable_domain",
    "set_policy_store",
    "set_store",
]

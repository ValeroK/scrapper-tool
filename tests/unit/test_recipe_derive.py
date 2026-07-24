"""Unit tests for recipe derivation (C1).

Derivation is the cost killer: it turns one expensive success (a browser render,
an LLM call) into a CSS schema that reproduces the same data for free next time.
That only pays off if a derived recipe is *right* — a wrong one silently returns
wrong data forever — so these tests lean hard on the cases where a naive
implementation quietly produces a plausible-but-wrong selector.
"""

from __future__ import annotations

from typing import Any

import pytest

from scrapper_tool._extractors import get as get_extractor
from scrapper_tool.recipe.derive import (
    Recipe,
    derive_recipe,
    registrable_domain,
    schema_fingerprint,
)

# Two rows share a price — the case that breaks common-ancestor derivation,
# because the other row's price node votes on this row's container.
LISTING_HTML = """
<html><body>
<header><h1>Cars for sale</h1></header>
<div class="feed">
  <div class="feed-item card">
    <h2 class="item-title">Mazda 3 2019</h2><span class="price">45,000</span>
  </div>
  <div class="feed-item card">
    <h2 class="item-title">Toyota Corolla 2020</h2><span class="price">52,000</span>
  </div>
  <div class="feed-item card">
    <h2 class="item-title">Honda Civic 2018</h2><span class="price">45,000</span>
  </div>
</div>
</body></html>
"""

LISTING_DATA = [
    {"title": "Mazda 3 2019", "price": "45,000"},
    {"title": "Toyota Corolla 2020", "price": "52,000"},
    {"title": "Honda Civic 2018", "price": "45,000"},
]

DETAIL_HTML = """
<html><body><div class="product-page">
  <h1 class="product-name">Brake Pad Set</h1>
  <div class="pricing"><span class="current-price">$129.99</span></div>
</div></body></html>
"""

DETAIL_DATA = {"name": "Brake Pad Set", "price": "$129.99"}


def _replay(schema: dict[str, Any], html: str) -> list[dict[str, Any]]:
    result = get_extractor("css").extract(html, options={"schema": schema})
    return list(result.data or []) if isinstance(result.data, list) else []


# --- the core promise -------------------------------------------------------


def test_derived_recipe_reproduces_the_listing_it_learned_from() -> None:
    recipe = derive_recipe(LISTING_HTML, LISTING_DATA, source_tier="render", url="https://x.co/c")
    assert recipe is not None
    assert _replay(recipe.schema, LISTING_HTML) == LISTING_DATA


def test_duplicate_values_do_not_pin_the_wrong_row() -> None:
    """Rows 1 and 3 share the price "45,000".

    A common-ancestor implementation walks up until it contains *both* price
    nodes — i.e. the whole feed — and derives a base selector matching one
    container with three rows' worth of fields inside. The replay would then
    return a single mangled row.
    """
    recipe = derive_recipe(LISTING_HTML, LISTING_DATA, source_tier="render", url="https://x.co/c")
    assert recipe is not None
    assert recipe.schema["baseSelector"] == "div.feed-item.card"
    assert len(_replay(recipe.schema, LISTING_HTML)) == 3


def test_recipe_replays_on_a_different_page_of_the_same_shape() -> None:
    """The point of a recipe: it generalises to pages it never saw."""
    recipe = derive_recipe(LISTING_HTML, LISTING_DATA, source_tier="render", url="https://x.co/c")
    assert recipe is not None
    page_two = LISTING_HTML.replace("Mazda 3 2019", "Kia Niro 2022").replace("45,000", "61,500")
    rows = _replay(recipe.schema, page_two)
    assert {"title": "Kia Niro 2022", "price": "61,500"} in rows


def test_single_record_detail_page() -> None:
    recipe = derive_recipe(DETAIL_HTML, DETAIL_DATA, source_tier="e1", url="https://x.co/p/1")
    assert recipe is not None
    assert recipe.multi_row is False
    assert _replay(recipe.schema, DETAIL_HTML) == [DETAIL_DATA]


def test_nested_same_text_is_not_ambiguity() -> None:
    """``<div class="pricing">`` wraps only the price span, so both have the
    same text. The span is the answer, not a conflict."""
    recipe = derive_recipe(DETAIL_HTML, DETAIL_DATA, source_tier="e1", url="https://x.co/p/1")
    assert recipe is not None
    price = next(f for f in recipe.schema["fields"] if f["name"] == "price")
    assert price["selector"] == "span.current-price"


# --- refusing to derive is a valid, important answer ------------------------


def test_json_ld_only_data_yields_no_recipe() -> None:
    """Data that exists only inside a <script> must not produce a CSS recipe.

    Pattern B already extracts JSON-LD deterministically at tier 1, so a CSS
    recipe would be strictly more fragile for zero gain — and any selector
    "found" for invisible text would be a coincidence.
    """
    html = (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"Invisible Widget","price":"19.99"}'
        "</script></head><body><div id=root></div></body></html>"
    )
    assert (
        derive_recipe(
            html,
            {"name": "Invisible Widget", "price": "19.99"},
            source_tier="a_b_c",
            url="https://x.co/p",
        )
        is None
    )


def test_all_classes_hashed_yields_no_recipe() -> None:
    """Every class is build output and there's no semantic hook.

    Refusing is right: the only available selector would be a bare tag, which
    replays the wrapper as an extra bogus row. A missing recipe just means full
    price next time; a wrong one means wrong data forever.
    """
    html = """
    <html><body><div class="Grid_grid__3xY9k">
      <div class="css-1a2b3c Card_card__ab12">
        <h3 class="Card_title__9zQ">Widget One</h3><p class="sc-bdVaJa">10.00</p>
      </div>
      <div class="css-1a2b3c Card_card__ab12">
        <h3 class="Card_title__9zQ">Widget Two</h3><p class="sc-bdVaJa">20.00</p>
      </div>
    </div></body></html>
    """
    data = [{"title": "Widget One", "price": "10.00"}, {"title": "Widget Two", "price": "20.00"}]
    assert derive_recipe(html, data, source_tier="render", url="https://x.co/g") is None


def test_semantic_attributes_rescue_a_fully_hashed_page() -> None:
    """data-testid survives a restyle precisely because it isn't styling."""
    html = """
    <html><body><div class="Grid_grid__3xY9k">
      <div class="css-1a2b3c" data-testid="product-card">
        <h3 class="Card_title__9zQ" data-testid="name">Widget One</h3>
        <p class="sc-bdVaJa" data-testid="price">10.00</p>
      </div>
      <div class="css-1a2b3c" data-testid="product-card">
        <h3 class="Card_title__9zQ" data-testid="name">Widget Two</h3>
        <p class="sc-bdVaJa" data-testid="price">20.00</p>
      </div>
    </div></body></html>
    """
    data = [{"name": "Widget One", "price": "10.00"}, {"name": "Widget Two", "price": "20.00"}]
    recipe = derive_recipe(html, data, source_tier="render", url="https://x.co/g")
    assert recipe is not None
    assert recipe.schema["baseSelector"] == 'div[data-testid="product-card"]'
    assert _replay(recipe.schema, html) == data


def test_single_field_is_not_worth_a_recipe() -> None:
    """One text match is far more likely coincidence than extraction shape."""
    html = "<html><body><div class=box><h1 class=t>Only Title Here</h1></div></body></html>"
    assert (
        derive_recipe(
            html, {"title": "Only Title Here"}, source_tier="render", url="https://x.co/p"
        )
        is None
    )


def test_empty_inputs_return_none() -> None:
    assert derive_recipe("", {"a": "b"}, source_tier="render", url="https://x.co") is None
    assert derive_recipe(LISTING_HTML, None, source_tier="render", url="https://x.co") is None
    assert derive_recipe(LISTING_HTML, [], source_tier="render", url="https://x.co") is None


def test_non_string_values_are_ignored_not_crashed_on() -> None:
    data = [{"title": "Mazda 3 2019", "price": "45,000", "seen": 4, "ok": True, "x": None}]
    recipe = derive_recipe(LISTING_HTML, data, source_tier="render", url="https://x.co/c")
    assert recipe is not None
    assert set(recipe.field_names) == {"title", "price"}


def test_values_absent_from_the_page_yield_no_recipe() -> None:
    """An LLM that paraphrased rather than quoted can't be reverse-engineered."""
    data = [{"title": "A Car That Is Not Listed", "price": "999,999"}]
    assert derive_recipe(LISTING_HTML, data, source_tier="e1", url="https://x.co/c") is None


# --- metadata ---------------------------------------------------------------


def test_source_tier_decides_whether_replay_needs_a_browser() -> None:
    """A render-learned recipe targets nodes that only exist after JS runs.

    Replaying it over a raw fetch would silently return nothing, so the tier the
    recipe was learned from has to travel with it.
    """
    rendered = derive_recipe(LISTING_HTML, LISTING_DATA, source_tier="render", url="https://x.co/c")
    fetched = derive_recipe(LISTING_HTML, LISTING_DATA, source_tier="a_b_c", url="https://x.co/c")
    assert rendered is not None
    assert fetched is not None
    assert rendered.needs_render is True
    assert fetched.needs_render is False


def test_multi_row_flag_distinguishes_listing_from_detail() -> None:
    listing = derive_recipe(LISTING_HTML, LISTING_DATA, source_tier="render", url="https://x.co/c")
    detail = derive_recipe(DETAIL_HTML, DETAIL_DATA, source_tier="e1", url="https://x.co/p")
    assert listing is not None
    assert detail is not None
    assert listing.multi_row is True
    assert detail.multi_row is False


def test_recipe_round_trips_through_dict() -> None:
    recipe = derive_recipe(LISTING_HTML, LISTING_DATA, source_tier="render", url="https://x.co/c")
    assert recipe is not None
    assert Recipe.from_dict(recipe.to_dict()) == recipe


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.yad2.co.il/vehicles/cars", "yad2.co.il"),
        ("https://yad2.co.il/x", "yad2.co.il"),
        ("https://STORE.Mopar.com/p?a=1", "store.mopar.com"),
        ("not a url", ""),
    ],
)
def test_registrable_domain(url: str, expected: str) -> None:
    assert registrable_domain(url) == expected


def test_schema_fingerprint_is_order_independent() -> None:
    a = {"baseSelector": "div", "fields": [{"name": "x", "selector": "p"}]}
    b = {"fields": [{"selector": "p", "name": "x"}], "baseSelector": "div"}
    assert schema_fingerprint(a) == schema_fingerprint(b)
    assert schema_fingerprint(a) != schema_fingerprint({"baseSelector": "span", "fields": []})

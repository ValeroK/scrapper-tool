"""Derive a replayable CSS recipe from one successful extraction.

Given the HTML an expensive tier saw and the data it produced, work backwards to
the selectors that would have produced the same data for free.

The approach is **value matching**, not DOM guessing: for each extracted value,
find the visible-text nodes that literally contain it, then compute the most
stable selector identifying them. That has three properties worth having:

1. It works regardless of which tier produced the data — an LLM's output is just
   as derivable as an extractor's, which is the whole point (an LLM call becomes
   a selectolax parse next time).
2. It cannot invent a selector for data that isn't in the visible DOM. Values
   that live only inside a ``<script>`` JSON-LD block yield nothing, which is
   correct: Pattern B already handles those deterministically at tier 1, so a CSS
   recipe there would be more fragile for zero gain.
3. Every derived recipe is **verified** before it's returned — run through the
   real ``css`` extractor and checked against the data it was derived from. A
   recipe that doesn't reproduce its own training example is discarded rather
   than cached. Derivation is heuristic; verification is not.

Selector stability is a heuristic (framework-hashed classes like
``styles_title__3xY9k`` or ``css-1a2b3c`` are skipped) and it will sometimes be
wrong. That's tolerable because it's caught twice: by verification here, and by
drift detection at replay time, which invalidates the recipe and re-derives.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from scrapper_tool._logging import get_logger

_logger = get_logger(__name__)

# Text inside these never renders, so a "visible value" can't live here. Also
# the guard that keeps us from deriving CSS recipes for JSON-LD-only data.
_NON_VISIBLE_TAGS = frozenset({"script", "style", "noscript", "template", "head", "title"})

# Values shorter than this match too much to identify anything ("10", "-", "OK").
_MIN_VALUE_LEN = 3

# A recipe with one field is rarely worth replaying and is very likely a
# coincidental text match rather than a real extraction shape.
_MIN_FIELDS = 2

# Below this, a "list" isn't repeating structure — treat it as a single record.
_MIN_ROWS_FOR_LIST = 2

# Framework-generated class hashes: CSS-modules (`title__3xY9k`), emotion
# (`css-1a2b3c`), styled-components (`sc-bdVaJa`). Stable within a build,
# regenerated on the next one — the worst possible thing to pin a selector to.
_HASHED_SEGMENT_RE = re.compile(r"(?:^|[-_])[a-z]*\d[a-z\d]{2,}(?:$|[-_])", re.IGNORECASE)
_STYLED_COMPONENTS_RE = re.compile(r"^sc-[a-zA-Z]{5,}$")
_EMOTION_RE = re.compile(r"^css-[a-z\d]+", re.IGNORECASE)

# How far up from a matched value node to look for a repeating row container.
_MAX_ANCESTOR_WALK = 12

# Guards against pathological/recursive markup during the DOM walk.
_MAX_DOM_DEPTH = 80

# Long class names are almost always generated (or a concatenated utility soup).
_MAX_CLASS_NAME_LEN = 40

# Length window for a CSS-modules hash suffix (`Card_root__ab12`).
_MIN_HASH_SUFFIX = 3
_MAX_HASH_SUFFIX = 8

# Non-styling hooks, in preference order. On a site where every class is a build
# hash these are frequently the only stable handle, and they survive restyling
# because they were never styling in the first place.
_SEMANTIC_ATTRS: tuple[str, ...] = (
    "data-testid",
    "data-test-id",
    "data-test",
    "data-qa",
    "data-cy",
    "itemprop",
    "itemtype",
    "data-component",
    "role",
)


@dataclass(frozen=True)
class Recipe:
    """A cached, replayable extraction recipe for one domain.

    ``source_tier`` is what makes replay cheap *correctly*: a recipe learned from
    a plain HTTP fetch can be replayed with a plain HTTP fetch, but one learned
    from a rendered DOM needs a render to replay — the selectors target nodes
    that only exist after JS runs. Replaying a render-learned recipe over a raw
    fetch would silently produce nothing.
    """

    domain: str
    schema: dict[str, Any]
    source_tier: str
    sample_url: str
    multi_row: bool
    created_at: str
    schema_hash: str
    field_names: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_render(self) -> bool:
        """Whether replaying this recipe requires a browser render."""
        return self.source_tier in {"render", "e1", "e2", "d"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Recipe:
        return cls(
            domain=str(raw["domain"]),
            schema=dict(raw["schema"]),
            source_tier=str(raw["source_tier"]),
            sample_url=str(raw.get("sample_url", "")),
            multi_row=bool(raw.get("multi_row", False)),
            created_at=str(raw.get("created_at", "")),
            schema_hash=str(raw.get("schema_hash", "")),
            field_names=tuple(raw.get("field_names") or ()),
        )


def registrable_domain(url: str) -> str:
    """Cache key: host without a leading ``www.``.

    Not a public-suffix-list implementation on purpose — no dependency, and the
    only thing riding on it is a cache key. Over-splitting would just mean two
    cache entries where one would do; the recipe is still verified on use.
    """
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def schema_fingerprint(schema: dict[str, Any]) -> str:
    """Stable short hash of a schema, for cache invalidation on shape change."""
    blob = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --- selector construction --------------------------------------------------


def _is_css_module_hash(name: str) -> bool:
    """CSS-modules ``Block_element__hash`` — keeping BEM's ``block__element``.

    The distinguishing feature of the generated suffix is that it isn't a word:
    it carries a digit or mixed case. ``card__ab12`` and ``title__9zQ`` are
    build output; ``menu__item`` is something a person typed and is stable.
    """
    if "__" not in name:
        return False
    suffix = name.rsplit("__", 1)[1]
    if not (_MIN_HASH_SUFFIX <= len(suffix) <= _MAX_HASH_SUFFIX):
        return False
    has_digit = any(c.isdigit() for c in suffix)
    mixed_case = suffix != suffix.lower() and suffix != suffix.upper()
    return has_digit or mixed_case


def _is_stable_class(name: str) -> bool:
    """Whether a class name looks authored rather than build-generated."""
    if not name or len(name) > _MAX_CLASS_NAME_LEN:
        return False
    if _STYLED_COMPONENTS_RE.match(name) or _EMOTION_RE.match(name):
        return False
    if _is_css_module_hash(name):
        return False
    return not _HASHED_SEGMENT_RE.search(name)


def _stable_classes(node: LexborNode) -> list[str]:
    raw = node.attributes.get("class") or ""
    return [c for c in raw.split() if _is_stable_class(c)]


def _semantic_attribute(node: LexborNode) -> str | None:
    """A test/semantic hook, e.g. ``[data-testid="row"]``.

    Worth preferring over classes on framework-heavy sites: when every class is
    a build hash, these are often the only stable handle on the page — and they
    survive restyling precisely because they aren't styling.
    """
    for attr in _SEMANTIC_ATTRS:
        value = node.attributes.get(attr)
        if value and len(value) <= _MAX_CLASS_NAME_LEN and '"' not in value:
            return f'[{attr}="{value}"]'
    return None


def _node_signature(node: LexborNode) -> str:
    """A selector identifying this node by its most stable available handle."""
    classes = _stable_classes(node)
    if classes:
        # Two classes is enough to be specific without pinning every utility
        # class the page carries (which turns a restyle into a false drift).
        return str(node.tag) + "".join(f".{c}" for c in classes[:2])
    semantic = _semantic_attribute(node)
    if semantic:
        return f"{node.tag}{semantic}"
    return str(node.tag)


class _TextIndex:
    """One DOM walk, then O(1) exact-text lookups.

    Built once per derivation rather than scanning the tree per value: a
    20-field, 20-row page would otherwise mean 400 full-document scans, which
    on a real multi-MB listing page is the difference between milliseconds and
    seconds.

    Nodes are stored deepest-first because an exact-text match propagates up
    every ancestor — a ``<div>`` wrapping only ``<h3>One</h3>`` also has text
    "One" — and the leaf is the node a human would have selected.
    """

    def __init__(self, parser: LexborHTMLParser) -> None:
        self._by_text: dict[str, list[tuple[int, LexborNode]]] = {}
        body = parser.body or parser.root
        if body is not None:
            self._walk(body, depth=0)
        for entries in self._by_text.values():
            entries.sort(key=lambda pair: -pair[0])

    def _walk(self, node: LexborNode, *, depth: int) -> None:
        if depth > _MAX_DOM_DEPTH:
            return
        if str(node.tag).lower() in _NON_VISIBLE_TAGS:
            return  # prunes the whole subtree — JSON-LD included, deliberately
        text = node.text(deep=True, strip=True) or ""
        if len(text) >= _MIN_VALUE_LEN:
            self._by_text.setdefault(text, []).append((depth, node))
        for child in node.iter(include_text=False):
            self._walk(child, depth=depth + 1)

    def leaves_matching(self, value: str) -> list[LexborNode]:
        target = value.strip()
        if len(target) < _MIN_VALUE_LEN:
            return []
        return [node for _, node in self._by_text.get(target, ())]


def _ancestors(node: LexborNode) -> list[LexborNode]:
    chain: list[LexborNode] = []
    cursor: LexborNode | None = node.parent
    while cursor is not None and len(chain) < _MAX_ANCESTOR_WALK:
        chain.append(cursor)
        cursor = cursor.parent
    return chain


def _same_node(a: LexborNode | None, b: LexborNode | None) -> bool:
    """Whether two handles point at the same DOM node.

    NOT ``is``. selectolax hands back a fresh Python wrapper on every ``.parent``
    / ``css_first`` access, so ``node.parent is node.parent`` is False even
    though both describe the same element. ``mem_id`` is the underlying node
    address and is the only reliable identity here.
    """
    return a is not None and b is not None and a.mem_id == b.mem_id


def _common_ancestor(nodes: list[LexborNode]) -> LexborNode | None:
    """Nearest ancestor shared by every node in ``nodes``."""
    if not nodes:
        return None
    chains = [[n, *_ancestors(n)] for n in nodes]
    for candidate in chains[0]:
        if all(any(_same_node(a, candidate) for a in chain) for chain in chains[1:]):
            return candidate
    return None


def _field_selector(row: LexborNode, target: LexborNode) -> str | None:
    """A selector resolving ``target`` from within ``row`` via ``css_first``.

    Mirrors how :class:`~scrapper_tool._extractors.css.CssSchemaExtractor`
    resolves fields, so anything returned here is directly usable — and directly
    verifiable.
    """
    signature = _node_signature(target)
    if _same_node(row.css_first(signature), target):
        return signature
    # Ambiguous within the row — qualify with the parent's signature.
    parent = target.parent
    if parent is not None and not _same_node(parent, row):
        qualified = f"{_node_signature(parent)} > {signature}"
        if _same_node(row.css_first(qualified), target):
            return qualified
    return None


# --- derivation -------------------------------------------------------------


def _is_descendant_of(node: LexborNode, ancestor: LexborNode) -> bool:
    cursor: LexborNode | None = node
    depth = 0
    while cursor is not None and depth <= _MAX_DOM_DEPTH:
        if _same_node(cursor, ancestor):
            return True
        cursor = cursor.parent
        depth += 1
    return False


def _resolve_in_scope(
    scope: LexborNode, candidates: dict[str, list[LexborNode]]
) -> tuple[dict[str, LexborNode], bool]:
    """Resolve each field within ``scope``. Returns ``(located, overshot)``.

    ``overshot`` means a field matched two *unrelated* nodes inside the scope —
    the scope has grown past the record's own container and now spans a sibling
    record. Widening further can only get worse, so it's a stop signal.

    Nested matches are not ambiguity. A ``<div class="pricing">`` whose only
    content is ``<span>$129.99</span>`` has the same text as the span, so both
    are candidates; the span is simply the right answer. Only disjoint matches
    mean we've spilled into another record.
    """
    located: dict[str, LexborNode] = {}
    for name, nodes in candidates.items():
        inside = [n for n in nodes if _is_descendant_of(n, scope)]
        if not inside:
            continue
        # Candidates arrive deepest-first, so inside[0] is the innermost.
        deepest = inside[0]
        if all(_is_descendant_of(deepest, other) for other in inside[1:]):
            located[name] = deepest
        else:
            return located, True
    return located, False


def _locate_record(
    index: _TextIndex, record: dict[str, Any]
) -> tuple[LexborNode, dict[str, LexborNode]] | None:
    """Find the container node for one record, plus the node holding each field.

    Widens a scope upward from the most-specific field until every field resolves
    inside it. That upward walk *is* the row-detection: the smallest ancestor
    containing all of a record's values is its container, whether that's a
    listing row or a whole detail page.

    The scope must be grown rather than computed as a common ancestor of all
    matches, because a page-wide-ambiguous value can't be allowed to vote. Two
    rows priced "45,000" would otherwise let the other row's price node drag the
    ancestor up to the shared container, silently deriving selectors that match
    every row for one record's data. So the anchor is the field with the fewest
    page-wide matches, and ambiguous fields are only resolved once a scope
    exists to disambiguate them.
    """
    candidates: dict[str, list[LexborNode]] = {}
    for name, value in record.items():
        if not isinstance(value, str):
            continue
        found = index.leaves_matching(value)
        if found:
            candidates[name] = found
    if len(candidates) < _MIN_FIELDS:
        return None

    anchor_field = min(candidates, key=lambda k: len(candidates[k]))
    best: tuple[LexborNode, dict[str, LexborNode]] | None = None

    for anchor in candidates[anchor_field]:
        scope: LexborNode | None = anchor
        for _ in range(_MAX_ANCESTOR_WALK):
            if scope is None:
                break
            located, overshot = _resolve_in_scope(scope, candidates)
            if not overshot and len(located) == len(candidates):
                return scope, located  # every field resolved: the tightest fit
            if len(located) >= _MIN_FIELDS and (best is None or len(located) > len(best[1])):
                best = (scope, located)
            if overshot:
                break
            scope = scope.parent
    return best


def _derive_from_records(
    parser: LexborHTMLParser, records: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Build a ``{baseSelector, fields}`` schema reproducing ``records``."""
    index = _TextIndex(parser)
    row_nodes: list[LexborNode] = []
    per_row_fields: list[dict[str, LexborNode]] = []

    for record in records:
        found = _locate_record(index, record)
        if found is None:
            continue
        row, located = found
        if len(located) < _MIN_FIELDS:
            continue
        row_nodes.append(row)
        per_row_fields.append(located)

    if not row_nodes:
        return None

    base_selector = _base_selector_for(parser, row_nodes)
    if base_selector is None:
        return None

    # Field selectors must work for EVERY row, so intersect: a field that can
    # only be located in some rows would silently drop the others (the css
    # extractor drops a row missing a non-optional field).
    fields: list[dict[str, Any]] = []
    common_names = set(per_row_fields[0])
    for located in per_row_fields[1:]:
        common_names &= set(located)

    for name in per_row_fields[0]:
        if name not in common_names:
            continue
        selectors = {
            _field_selector(row, located[name])
            for row, located in zip(row_nodes, per_row_fields, strict=False)
        }
        if len(selectors) != 1 or None in selectors:
            continue
        fields.append({"name": name, "selector": selectors.pop(), "type": "text"})

    if len(fields) < _MIN_FIELDS:
        return None
    return {"baseSelector": base_selector, "fields": fields}


def _base_selector_for(parser: LexborHTMLParser, row_nodes: list[LexborNode]) -> str | None:
    """A selector matching every row node (and preferably nothing else)."""
    signatures = {_node_signature(n) for n in row_nodes}
    if len(signatures) != 1:
        return None
    signature = signatures.pop()
    matched = parser.css(signature)
    if not matched:
        return None
    # Every row must be reachable; extra matches are tolerable because the css
    # extractor drops rows whose required fields are missing.
    if not all(any(_same_node(m, row) for m in matched) for row in row_nodes):
        return None
    # Reject a selector that matches both a row and something containing it.
    # Repeating structure is never nested in itself, so a nested match means the
    # signature degenerated (typically to a bare `div` after every class on the
    # real row turned out to be a build hash) and would replay each row twice —
    # once from the row, once from its wrapper picking up the first row's fields.
    if any(
        _is_descendant_of(inner, outer)
        for i, outer in enumerate(matched)
        for inner in matched[i + 1 :]
    ):
        return None
    return signature


def _verify(schema: dict[str, Any], html: str, records: list[dict[str, Any]]) -> bool:
    """Does this schema actually reproduce the data it was derived from?

    The gate that makes a heuristic trustworthy. Runs the *real* extractor, not
    a reimplementation, so a recipe that passes here is one the replay tier can
    genuinely use.
    """
    from scrapper_tool._extractors import get as get_extractor  # noqa: PLC0415

    result = get_extractor("css").extract(html, options={"schema": schema})
    if not result.has_signal or not isinstance(result.data, list):
        return False
    replayed = {
        tuple(sorted((k, str(v)) for k, v in row.items()))
        for row in result.data
        if isinstance(row, dict)
    }
    field_names = {f["name"] for f in schema["fields"]}
    for record in records:
        expected = tuple(
            sorted(
                (k, str(v)) for k, v in record.items() if k in field_names and isinstance(v, str)
            )
        )
        if expected not in replayed:
            return False
    return True


def derive_recipe(
    html: str,
    data: Any,
    *,
    source_tier: str,
    url: str,
    now: datetime | None = None,
) -> Recipe | None:
    """Derive a verified, replayable recipe — or None when it isn't worth it.

    Returns None (not an error) whenever derivation can't produce something
    trustworthy: data absent from the visible DOM (JSON-LD-only wins), too few
    fields to be worth replaying, unstable selectors, or a schema that fails
    verification. A missing recipe just means the next request pays full price,
    which is the status quo — so being conservative here costs nothing and a
    wrong recipe would cost correctness.
    """
    if not html or not data:
        return None

    records, multi_row = _normalise(data)
    if not records:
        return None

    schema = _derive_from_records(LexborHTMLParser(html), records)
    if schema is None:
        _logger.debug("recipe.derive.no_schema", url=url, tier=source_tier)
        return None

    if not _verify(schema, html, records):
        _logger.info("recipe.derive.failed_verification", url=url, tier=source_tier)
        return None

    recipe = Recipe(
        domain=registrable_domain(url),
        schema=schema,
        source_tier=source_tier,
        sample_url=url,
        multi_row=multi_row,
        created_at=(now or datetime.now(UTC)).isoformat(),
        schema_hash=schema_fingerprint(schema),
        field_names=tuple(f["name"] for f in schema["fields"]),
    )
    _logger.info(
        "recipe.derived",
        domain=recipe.domain,
        tier=source_tier,
        base_selector=schema["baseSelector"],
        fields=len(schema["fields"]),
    )
    return recipe


def _normalise(data: Any) -> tuple[list[dict[str, Any]], bool]:
    """Coerce an extraction payload into ``(records, multi_row)``."""
    if isinstance(data, dict):
        flat = {k: v for k, v in data.items() if isinstance(v, str)}
        return ([flat], False) if flat else ([], False)
    if isinstance(data, list):
        records = [r for r in data if isinstance(r, dict)]
        if not records:
            return [], False
        return records, len(records) >= _MIN_ROWS_FOR_LIST
    return [], False


__all__ = [
    "Recipe",
    "derive_recipe",
    "registrable_domain",
    "schema_fingerprint",
]

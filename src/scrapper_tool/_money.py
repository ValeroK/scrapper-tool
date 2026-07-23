"""Locale-tolerant price parsing, shared by every extractor.

Two real bugs motivated pulling this into one place:

- Pattern C stripped every comma before parsing, so the European
  ``"19,99"`` became ``Decimal("1999")`` — a silent 100x error, which is the
  worst possible failure mode for a price. Nothing raised, nothing logged, and
  the number looked plausible.
- Pattern B handed the raw string to ``Decimal()``, so the US ``"1,299.99"``
  raised ``InvalidOperation`` and the price came back as ``None``. Data loss
  rather than corruption, but from the same root cause: neither knew what a
  separator meant.

The separator is inferred from the string's own shape, which resolves the common
cases without needing to know the vendor's locale:

===================  =========  ==========================================
Input                Result     Why
===================  =========  ==========================================
``1,299.99``         1299.99    Both present; the LAST one is the decimal.
``1.299,99``         1299.99    Same rule, European ordering.
``19,99``            19.99      One comma, 2 trailing digits -> decimal.
``1,299``            1299       One comma, 3 trailing digits -> thousands.
``1,234,567``        1234567    Repeated separator -> thousands.
``1 299,99``         1299.99    Space/NBSP grouping (fr/de).
``1'299.99``         1299.99    Apostrophe grouping (ch).
===================  =========  ==========================================

The one case inference cannot settle is a lone dot with three trailing digits:
``"1.299"`` is 1299 in Germany and 1.299 in the US. It's read as a decimal (the
US convention, and the pre-existing behaviour), because prices with three decimal
places are rare enough that the alternative would break more than it fixed. Pass
``decimal_sep=","`` when you know the source is European and this matters.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

# Currency glyphs and grouping characters that carry no numeric meaning.
_CURRENCY_GLYPHS = "$€£₪¥₽₹¢₩₴₺R"
# Thousands-separator characters, given as code points so the invisible ones
# are reviewable in a diff: space, NBSP, narrow NBSP, thin space, and the
# straight/typographic apostrophes Switzerland uses.
_GROUPING_CODEPOINTS = frozenset({0x20, 0xA0, 0x202F, 0x2009, 0x27, 0x2019, 0x60})

_KEEP_RE = re.compile(r"[^0-9.,\-]")

# A comma/dot followed by exactly this many digits reads as a decimal separator;
# three digits is the unambiguous signature of a thousands group.
_MAX_DECIMAL_PLACES_FOR_INFERENCE = 2

DecimalSep = Literal["auto", ".", ","]


def parse_price(value: Any, *, decimal_sep: DecimalSep = "auto") -> Decimal | None:
    """Parse a price string into a :class:`~decimal.Decimal`.

    Returns ``None`` for empty, missing, or non-numeric input — callers treat a
    missing price as "no signal", so raising here would turn a routine absence
    into an error path.

    ``decimal_sep`` forces an interpretation when the caller knows the locale;
    ``"auto"`` (the default) infers it from the string, per the table in this
    module's docstring.
    """
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        # Already numeric — no separator ambiguity to resolve. float goes via str
        # so 19.99 doesn't become 19.989999999999998.
        return _to_decimal(str(value))

    text = _strip_noise(str(value))
    if not text:
        return None
    return _to_decimal(_normalise_separators(text, decimal_sep))


def _strip_noise(raw: str) -> str:
    """Remove currency glyphs, grouping whitespace, and stray non-numerics."""
    text = "".join(
        ch
        for ch in raw.strip()
        if ch not in _CURRENCY_GLYPHS and ord(ch) not in _GROUPING_CODEPOINTS
    )
    # Whatever is left that isn't a digit, separator, or sign — currency codes
    # like "USD", the "each" in "19.99 each".
    return _KEEP_RE.sub("", text).strip()


def _to_decimal(text: str) -> Decimal | None:
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _normalise_separators(text: str, decimal_sep: DecimalSep) -> str:
    """Rewrite ``text`` so the only separator left is a decimal point.

    Every separator that isn't the decimal point is grouping, and grouping
    characters carry no meaning — so the whole job is deciding *which* character
    is the decimal point and then deleting the rest.
    """
    decimal_char = _decimal_char(text, decimal_sep)
    for char in (".", ","):
        if char != decimal_char:
            text = text.replace(char, "")
    return text.replace(decimal_char, ".") if decimal_char else text


def _decimal_char(text: str, decimal_sep: DecimalSep) -> str | None:
    """Which character marks the decimal point, or None if there isn't one."""
    if decimal_sep != "auto":
        return decimal_sep
    has_dot = "." in text
    has_comma = "," in text
    if has_dot and has_comma:
        # Whichever appears last: 1,299.99 vs 1.299,99.
        return "," if text.rfind(",") > text.rfind(".") else "."
    if has_comma:
        return "," if _marks_decimal(text, ",") else None
    if has_dot:
        # A lone dot stays a decimal point — see the module docstring on why the
        # ambiguous 3-digit case resolves this way. Repeated dots are grouping.
        return "." if text.count(".") == 1 else None
    return None


def _marks_decimal(text: str, sep: str) -> bool:
    """Whether a lone ``sep`` reads as a decimal point rather than grouping."""
    if text.count(sep) > 1:
        return False  # 1,234,567
    trailing = len(text.rsplit(sep, 1)[1])
    return trailing <= _MAX_DECIMAL_PLACES_FOR_INFERENCE  # 19,99 yes; 1,299 no


__all__ = [
    "DecimalSep",
    "parse_price",
]

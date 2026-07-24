"""Unit tests for locale-tolerant price parsing.

Two shipped bugs motivated this module, and both are the kind that don't announce
themselves:

- Pattern C stripped every comma, so ``"19,99"`` parsed as ``1999``. A silent
  100x error on a price is about the worst outcome available — nothing raises, and
  the number looks entirely plausible downstream.
- Pattern B handed the raw string to ``Decimal()``, so ``"1,299.99"`` raised and
  the price came back as ``None``.

So the table below is the contract, and the regression tests for both original
bugs are called out explicitly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from scrapper_tool._money import parse_price

# --- the two shipped bugs ---------------------------------------------------


def test_european_decimal_comma_is_not_multiplied_by_100() -> None:
    """The Pattern C bug: "19,99" used to parse as 1999."""
    assert parse_price("19,99") == Decimal("19.99")


def test_us_thousands_separator_does_not_become_none() -> None:
    """The Pattern B bug: "1,299.99" used to raise and yield None."""
    assert parse_price("1,299.99") == Decimal("1299.99")


# --- separator inference ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Unambiguous single-separator cases.
        ("19.99", "19.99"),
        ("0.5", "0.5"),
        ("1234", "1234"),
        # Both separators present: the LAST one is the decimal.
        ("1,299.99", "1299.99"),
        ("1.299,99", "1299.99"),
        ("1,234,567.89", "1234567.89"),
        ("1.234.567,89", "1234567.89"),
        # Lone comma: 1-2 trailing digits reads as a decimal...
        ("19,99", "19.99"),
        ("19,9", "19.9"),
        # ...3 trailing digits is the signature of a thousands group.
        ("1,299", "1299"),
        ("12,345", "12345"),
        # Repeated separator is always grouping.
        ("1,234,567", "1234567"),
        ("1.234.567", "1234567"),
    ],
)
def test_inference_table(raw: str, expected: str) -> None:
    assert parse_price(raw) == Decimal(expected)


def test_lone_dot_with_three_digits_reads_as_decimal() -> None:
    """The one genuinely ambiguous case, documented rather than guessed.

    "1.299" is 1299 in Germany and 1.299 in the US. It resolves to the US reading
    because three-decimal-place prices are rare enough that the alternative would
    break more than it fixed — and a caller who knows better can say so.
    """
    assert parse_price("1.299") == Decimal("1.299")
    assert parse_price("1.299", decimal_sep=",") == Decimal("1299")


# --- explicit locale --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "sep", "expected"),
    [
        ("1.299,99", ",", "1299.99"),
        ("1,299", ",", "1.299"),
        ("1.299", ",", "1299"),
        ("1,299.99", ".", "1299.99"),
        ("19,99", ".", "1999"),  # forcing dot-decimal means comma groups
        ("1,299", ".", "1299"),
    ],
)
def test_explicit_decimal_sep_overrides_inference(raw: str, sep: str, expected: str) -> None:
    assert parse_price(raw, decimal_sep=sep) == Decimal(expected)  # type: ignore[arg-type]


# --- noise stripping --------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$19.99", "19.99"),
        ("€19,99", "19.99"),
        ("£1,299.99", "1299.99"),
        ("₪299", "299"),
        ("¥1,200", "1200"),
        ("  19.99  ", "19.99"),
        ("19.99 USD", "19.99"),
        ("USD 19.99", "19.99"),
        ("19.99 each", "19.99"),
    ],
)
def test_currency_and_stray_text_are_stripped(raw: str, expected: str) -> None:
    assert parse_price(raw) == Decimal(expected)


# Thousands separators that are grouping characters rather than digits.
# Built from code points because four of them are visually identical in a diff:
# written as literals, a copy-paste slip would silently test one case 4 times.
_GROUPERS = {
    "plain space": chr(0x20),
    "non-breaking space (fr/de)": chr(0xA0),
    "narrow non-breaking space": chr(0x202F),
    "thin space": chr(0x2009),
    "straight apostrophe (ch)": chr(0x27),
    "typographic apostrophe": chr(0x2019),
}


def test_the_grouping_characters_are_distinct() -> None:
    """Guards the table above against a duplicated code point."""
    assert len(set(_GROUPERS.values())) == len(_GROUPERS)


@pytest.mark.parametrize("label", list(_GROUPERS))
def test_whitespace_and_apostrophe_grouping(label: str) -> None:
    assert parse_price(f"1{_GROUPERS[label]}299,99") == Decimal("1299.99")


def test_negative_prices_survive() -> None:
    """Refunds and adjustments are real values, not parse failures."""
    assert parse_price("-19.99") == Decimal("-19.99")
    assert parse_price("-1.299,50", decimal_sep=",") == Decimal("-1299.50")


# --- non-string input -------------------------------------------------------


def test_numeric_input_passes_through() -> None:
    assert parse_price(19) == Decimal("19")
    assert parse_price(Decimal("19.99")) == Decimal("19.99")


def test_float_does_not_pick_up_binary_noise() -> None:
    """Decimal(float) yields 19.989999999999998; via str it doesn't."""
    assert parse_price(19.99) == Decimal("19.99")


# --- absence is not an error ------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", "free", "N/A", "-", "$", "abc"])
def test_unparseable_input_returns_none(raw: object) -> None:
    """Callers treat a missing price as "no signal", so this must not raise."""
    assert parse_price(raw) is None


def test_a_bare_separator_is_not_a_number() -> None:
    assert parse_price(".") is None
    assert parse_price(",") is None

"""Unit tests for ``scrapper_tool.adapter.Adapter`` Protocol.

Verifies:
- A concrete class implementing ``vendor_id`` + ``search`` + ``fetch_detail``
  satisfies ``Adapter[Q, R]`` structurally (no inheritance needed).
- ``isinstance(obj, Adapter)`` works at runtime (``runtime_checkable``).
- A class missing the contract fails ``isinstance``.
- The generic parameters don't constrain runtime — they're for the
  type-checker.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel

from scrapper_tool.adapter import Adapter


class _Query(BaseModel):
    name: str


class _Result(BaseModel):
    name: str
    price: Decimal


@dataclass
class _GoodAdapter:
    """Implements the Adapter[_Query, _Result] contract structurally."""

    vendor_id: str = "good-vendor"

    async def search(self, query: _Query) -> list[_Result]:
        return [_Result(name=query.name, price=Decimal("19.99"))]

    async def fetch_detail(self, url: str) -> _Result | None:
        if url.endswith("/missing"):
            return None
        return _Result(name="detail", price=Decimal("42.00"))


@dataclass
class _MissingMethodAdapter:
    """Has vendor_id + search but no fetch_detail."""

    vendor_id: str = "incomplete"

    async def search(self, query: _Query) -> list[_Result]:
        return []


@dataclass
class _MissingFieldAdapter:
    """Has both methods but no vendor_id attribute."""

    async def search(self, query: _Query) -> list[_Result]:
        return []

    async def fetch_detail(self, url: str) -> _Result | None:
        return None


class TestAdapterProtocolStructural:
    def test_complete_implementation_satisfies_protocol(self) -> None:
        adapter = _GoodAdapter()
        assert isinstance(adapter, Adapter)

    def test_missing_method_fails_isinstance(self) -> None:
        adapter = _MissingMethodAdapter()
        assert not isinstance(adapter, Adapter)

    def test_missing_field_fails_isinstance(self) -> None:
        adapter = _MissingFieldAdapter()
        assert not isinstance(adapter, Adapter)


class TestAdapterRuntime:
    """The Protocol exists primarily for type-checking; verify it
    doesn't impose a runtime cost beyond ``isinstance`` semantics."""

    async def test_search_round_trip(self) -> None:
        adapter: Adapter[_Query, _Result] = _GoodAdapter()
        results = await adapter.search(_Query(name="test"))
        assert len(results) == 1
        assert results[0].price == Decimal("19.99")

    async def test_fetch_detail_round_trip(self) -> None:
        adapter: Adapter[_Query, _Result] = _GoodAdapter()
        detail = await adapter.fetch_detail("https://example.test/found")
        assert detail is not None
        assert detail.price == Decimal("42.00")

    async def test_fetch_detail_returns_none_for_missing(self) -> None:
        adapter: Adapter[_Query, _Result] = _GoodAdapter()
        detail = await adapter.fetch_detail("https://example.test/missing")
        assert detail is None

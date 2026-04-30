"""Generic ``Adapter[QueryT, ResultT]`` Protocol.

A specialisation contract for vendor-specific scrapers built on top of
``scrapper-tool``. Consumers implement a class that conforms to this
Protocol and gain typed integration with the rest of the lib (notably
the M13 MCP server).

Why a Protocol (not an ABC)
---------------------------

`scrapper-tool` doesn't impose inheritance — adapters can be plain
classes, dataclasses, attrs models, or whatever the consumer prefers.
The Protocol is structural: any object exposing ``vendor_id``,
``search()``, and ``fetch_detail()`` with the right shapes satisfies
``Adapter[Q, R]``. ``isinstance(x, Adapter)`` works because the
Protocol is ``runtime_checkable``.

Specialising
------------

::

    from dataclasses import dataclass
    from decimal import Decimal
    from pydantic import BaseModel

    from scrapper_tool import vendor_client, request_with_retry
    from scrapper_tool.patterns.b import extract_product_offer
    from scrapper_tool.adapter import Adapter

    class OEMQuery(BaseModel):
        oem: str

    class PartResult(BaseModel):
        oem: str
        vendor: str
        price: Decimal
        currency: str
        url: str

    @dataclass
    class MyVendorAdapter:  # implements Adapter[OEMQuery, PartResult]
        vendor_id: str = "myvendor"

        async def search(self, query: OEMQuery) -> list[PartResult]:
            url = f"https://myvendor.example/api/search?oem={query.oem}"
            async with vendor_client() as client:
                resp = await request_with_retry(client, "GET", url)
            ...   # parse + return

        async def fetch_detail(self, url: str) -> PartResult | None:
            async with vendor_client() as client:
                resp = await request_with_retry(client, "GET", url)
            offer = extract_product_offer(resp.text, base_url=url)
            ...   # convert to PartResult

    adapter: Adapter[OEMQuery, PartResult] = MyVendorAdapter()

The Protocol is ``runtime_checkable``, so ``isinstance(adapter, Adapter)``
returns True at runtime without inheritance.

Consumers wanting domain-specific extensions (PartsPilot's
``VendorAdapter`` is one) can subclass the Protocol or define their
own narrower one. The lib stays out of domain modelling.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Adapter[QueryT, ResultT](Protocol):
    """Generic vendor-adapter contract.

    Type parameters
    ---------------
    QueryT
        The query / input type. PartsPilot uses an ``OEMQuery``
        Pydantic model with vehicle context; weather scrapers use a
        ``Location`` model; etc.
    ResultT
        The result type. Should be a Pydantic model so consumers can
        snapshot-test against goldens (see
        :func:`scrapper_tool.testing.assert_pydantic_snapshot`).

    Required attributes
    -------------------
    vendor_id : str
        Stable, lowercase, hyphen-or-underscore-free identifier for the
        vendor (e.g. ``"amayama"``, ``"megazip"``). Used by the lib's
        logging + the MCP server (M13) to tag tool calls.

    Required methods
    ----------------
    ``async search(query: QueryT) -> list[ResultT]``
        Search the vendor for results matching ``query``. Return a
        list (possibly empty) of result records.
    ``async fetch_detail(url: str) -> ResultT | None``
        Fetch a single result detail from a canonical URL. Return
        ``None`` when the vendor returns 404 or the URL doesn't
        resolve to a vendor result.

    Both methods are expected to:
    - Wrap the actual HTTP work in :func:`scrapper_tool.http.vendor_client`
      and :func:`scrapper_tool.http.request_with_retry`.
    - Wrap the call site in the consumer's own circuit breaker — the
      lib provides primitives but does not own breaker policy.
    - Raise :class:`scrapper_tool.errors.VendorHTTPError` /
      :class:`scrapper_tool.errors.VendorUnavailable` on transport
      exhaustion (covers the breaker's "vendor down" branch).
    - Raise :class:`scrapper_tool.errors.BlockedError` when the
      anti-bot ladder exhausts (consumer should escalate to Pattern D).
    - Raise :class:`scrapper_tool.errors.ParseError` on parser drift
      — DOES NOT trip the breaker (it's "our bug", not "their fault").
    """

    vendor_id: str

    async def search(self, query: QueryT) -> list[ResultT]: ...

    async def fetch_detail(self, url: str) -> ResultT | None: ...


__all__ = [
    "Adapter",
]

# Pattern B — Embedded JSON

> *Stub — populated in M3.*

**Signals**: the document response is HTML, but the price lives inside a `<script>` tag as JSON. Common markers:

| Marker | What it is |
|---|---|
| `<script type="application/ld+json">` with `"@type": "Product"` | schema.org Product/Offer block. **Most common modern pattern.** |
| `__NEXT_DATA__` | Next.js page data. |
| `window.__INITIAL_STATE__` | Older Vue/React SSR pattern. |
| `self.__next_f.push(...)` | Next.js 13+ App Router streaming. |
| `<script>window.__NUXT__ = ...</script>` | Nuxt SSR data. |

**Helper**: `scrapper_tool.patterns.b.extract_product_offer(html)` (via [extruct](https://github.com/scrapinghub/extruct)).

**Cost**: low. One call, broad markup coverage (handles RDFa, microdata, JSON-LD with multiple `@graph` nesting shapes).

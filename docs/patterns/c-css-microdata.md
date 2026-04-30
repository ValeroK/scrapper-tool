# Pattern C — CSS / schema.org microdata

> *Stub — populated in M4.*

**Signals**: price is visible in the rendered HTML but no embedded JSON state object.

**Helpers**:
- `scrapper_tool.patterns.c.extract_microdata_price(html)` — finds `<meta itemprop="price">` + `<meta itemprop="priceCurrency">`. Try this first.
- `scrapper_tool.patterns.c.extract_via_selectors(html, price_selector=..., currency_selector=...)` — last-resort bespoke CSS selectors via [selectolax](https://github.com/rushter/selectolax) (lexbor backend; 30-40× faster than BeautifulSoup).

**Cost**: medium. CSS selectors break when vendors restructure markup.

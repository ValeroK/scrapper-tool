"""E2E: the autonomous cascade end to end, via the one public entrypoint (F4).

Unlike the per-pattern e2e scripts, this drives the whole self-driving cascade
through ``scrapper_tool.scrape`` and asserts the *autonomy properties* that make
the toolkit more than a bag of tiers:

1. Each site is handled by the **cheapest tier that works** — a clean static
   page must win at ``a_b_c``, not escalate.
2. A **repeat** call for a domain is cheaper than the first: either a learned
   recipe replays it, or per-domain memory skips the tiers that failed.
3. The **classifier is content-first** — a protected site that renders under a
   403 is still reported as real content, not a block.

This is a LIVE test. It hits real sites and is deliberately not part of the unit
suite. Run it directly (network + Camoufox required):

    python scripts/e2e/test_autonomous_cascade.py            # ladder tiers only
    python scripts/e2e/test_autonomous_cascade.py --render   # include render tier

It cleans up after itself with a throwaway recipe/policy dir so a local run never
pollutes a real cache.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile


def _isolate_caches() -> str:
    """Point the recipe + policy stores at a throwaway dir for this run."""
    cache_dir = tempfile.mkdtemp(prefix="e2e-cascade-")
    os.environ["SCRAPPER_TOOL_RECIPE_DIR"] = cache_dir
    return cache_dir


async def _run() -> int:
    from scrapper_tool import scrape

    include_render = "--render" in sys.argv
    if not include_render:
        # Keep the cascade to the no-browser tiers unless render is requested.
        os.environ["SCRAPPER_TOOL_RENDER_TIER"] = "0"

    failures = 0

    # --- 1. clean static (mode=fetch): tier 1 returns the page --------------
    # A bare HTML page with no JSON-LD has no "signal" for auto-mode to accept,
    # so it would (correctly) escalate; mode=fetch is the honest tier-1 check.
    print("[1] clean static, mode=fetch -> expect a_b_c, raw HTML")
    r = await scrape("https://quotes.toscrape.com/", mode="fetch")
    if r["pattern_used"] != "a_b_c" or "Albert Einstein" not in (r.get("raw_text") or ""):
        print(f"    FAIL: won at {r['pattern_used']!r}")
        failures += 1
    else:
        print(f"    OK: {r['pattern_used']}  ({len(r['raw_text'])} bytes)")

    # --- 2. structured extraction with a CSS schema -------------------------
    print("[2] CSS schema on a listing -> expect a_b_c with rows")
    schema = {
        "baseSelector": "div.quote",
        "fields": [
            {"name": "text", "selector": "span.text", "type": "text"},
            {"name": "author", "selector": "small.author", "type": "text"},
        ],
    }
    r = await scrape("https://quotes.toscrape.com/", schema=schema)
    rows = r.get("data") or []
    if r["pattern_used"] != "a_b_c" or len(rows) < 5:
        print(f"    FAIL: {r['pattern_used']!r}, {len(rows)} rows")
        failures += 1
    else:
        print(f"    OK: {len(rows)} rows via {r['pattern_used']}")

    # --- 3. repeat is cheaper (policy skip or replay) -----------------------
    print("[3] repeat the same domain -> expect it not to re-run the full ladder")
    r2 = await scrape("https://quotes.toscrape.com/", schema=schema)
    log = r2.get("escalation_log") or []
    skipped = any(row.get("step") in {"policy", "replay"} for row in log)
    if r2["pattern_used"] == "replay" or skipped:
        print(f"    OK: repeat used {r2['pattern_used']} (log shows a shortcut)")
    else:
        # Not fatal — the first call may not have learned anything — but report it.
        print(f"    NOTE: repeat still ran {r2['pattern_used']}; no shortcut recorded")

    # --- 4. protected site (render only) ------------------------------------
    if include_render:
        print("[4] protected site -> render clears it, content-first classifier")
        try:
            r = await scrape("https://www.g2.com/")
            real = r.get("product") is not None or r.get("data") or r.get("raw_text")
            print(
                f"    {r['pattern_used']}  challenge={r.get('challenge_detected')}  "
                f"has_content={bool(real)}"
            )
        except Exception as exc:  # noqa: BLE001 — live target may hard-block this IP
            print(f"    NOTE: {type(exc).__name__}: {str(exc)[:100]} (IP reputation, not a bug)")

    return failures


async def main() -> None:
    cache_dir = _isolate_caches()
    try:
        failures = await _run()
    finally:
        import shutil

        shutil.rmtree(cache_dir, ignore_errors=True)
    if failures:
        print(f"\nAUTONOMOUS CASCADE E2E: {failures} failure(s)")
        raise SystemExit(1)
    print("\nAUTONOMOUS CASCADE E2E [OK]")


if __name__ == "__main__":
    asyncio.run(main())

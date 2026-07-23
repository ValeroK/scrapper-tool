"""Benchmark the Obscura stealth build against Camoufox on real bot-walls.

Why this exists: Obscura is fast and tiny (~30 MB RAM vs Camoufox's ~200 MB) but
its stealth is unproven. The published Docker Hub image is NOT compiled with
``--features stealth`` (no TLS impersonation), and it was defeated by a Radware
wall that Camoufox passed. Before Obscura is trusted on protected targets — or
promoted in any auto-selection heuristic — it has to be measured.

This script renders a set of anti-bot demo/target pages through each backend via
the render tier (:func:`scrapper_tool.patterns.render.render_html`) and reports,
per backend/target, whether the result looks like real content or a challenge
interstitial.

Prerequisites
-------------
- Camoufox: ``pip install 'scrapper-tool[llm-agent]'`` + ``camoufox fetch``.
- Obscura stealth build + server running, e.g.::

      docker build -f Dockerfile.obscura -t scrapper-tool-obscura:stealth .
      docker run -d --name obscura -p 9222:9222 scrapper-tool-obscura:stealth

Usage
-----
    uv run python scripts/e2e/bench_obscura.py
    OBSCURA_CDP_URL=http://127.0.0.1:9222 uv run python scripts/e2e/bench_obscura.py

Output is a table plus JSON, suitable for pasting into
``docs/research/2026-camoufox-obscura-capabilities.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from scrapper_tool.patterns.render import render_html

OBSCURA_CDP_URL = os.environ.get("OBSCURA_CDP_URL", "http://127.0.0.1:9222")

# Markers that mean "we got a bot-wall, not the page". Kept local to the script
# so it stays runnable standalone; the library-side equivalent is the shared
# challenge detector (Phase B1).
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "challenges.cloudflare.com",
    "are you for real",
    "validate.perfdrive.com",
    "shieldsquare",
    "radware",
    "loader page",
    "geo.captcha-delivery.com",
    "datadome",
    "px-captcha",
)

# (label, url, marker that proves we got REAL content)
#
# Target choice matters. A captcha *demo* page is useless here: it embeds the
# widget by design, so a challenge-marker heuristic can't tell "widget present"
# from "we were walled" — every verdict comes back CHALLENGED regardless of the
# backend. Only use targets where a bot-wall is distinguishable from the content:
#   - js-sandbox: control. JS-only content, no anti-bot. Any backend that fails
#     here is broken, not blocked.
#   - yad2-radware: a real Radware/ShieldSquare wall.
TARGETS: list[tuple[str, str, str]] = [
    ("js-sandbox", "https://quotes.toscrape.com/js/", 'class="quote"'),
    ("yad2-radware", "https://www.yad2.co.il/vehicles/cars", "__NEXT_DATA__"),
]

BACKENDS: list[tuple[str, dict[str, Any]]] = [
    ("camoufox", {"browser": "camoufox"}),
    ("obscura-stealth", {"browser": "obscura", "cdp_url": OBSCURA_CDP_URL}),
]


def _verdict(html: str, success_marker: str) -> str:
    low = html.lower()
    if any(m in low for m in _CHALLENGE_MARKERS):
        return "CHALLENGED"
    if success_marker.lower() in low:
        return "PASSED"
    return "UNKNOWN"


async def _bench_one(backend: str, kwargs: dict[str, Any], url: str, marker: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        result = await render_html(url, settle_s=6.0, timeout_s=60, **kwargs)
    except Exception as exc:  # a backend that can't even launch is a real result
        return {
            "backend": backend,
            "verdict": "ERROR",
            "detail": f"{type(exc).__name__}: {exc}"[:160],
            "seconds": round(time.perf_counter() - started, 1),
        }
    return {
        "backend": backend,
        "verdict": _verdict(result.html, marker),
        "status": result.status,
        "bytes": len(result.html),
        # A changed origin usually means we were redirected to a bot-wall.
        "final_url_changed": not result.final_url.startswith(url.split("?", 1)[0][:40]),
        "seconds": round(time.perf_counter() - started, 1),
    }


async def main() -> None:
    rows: list[dict[str, Any]] = []
    for label, url, marker in TARGETS:
        for backend, kwargs in BACKENDS:
            row = await _bench_one(backend, kwargs, url, marker)
            row["target"] = label
            rows.append(row)
            print(
                f"{label:20} {backend:16} {row['verdict']:11} "
                f"{row.get('bytes', 0):>8} bytes  {row['seconds']:>5}s "
                f"{row.get('detail', '')}"
            )

    print()
    print("NOTE: a CHALLENGED verdict can also mean this IP is flagged — anti-bot")
    print("vendors escalate on IP reputation, so re-run from a clean IP/proxy")
    print("before concluding a backend is weaker (see the D1 proxy-rotation task).")
    print()
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

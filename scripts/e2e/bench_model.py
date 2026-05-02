"""Benchmark battery — runs a fixed set of E1/E2 tests against the
currently-loaded LM Studio model and writes a structured JSON report.

Designed to run **inside the scrapper-tool Docker image** so the same
runtime is exercised across model comparisons. Each test is run N
times (configurable) so we can report a median plus the spread.

Usage (from the host):

    cat scripts/e2e/bench_model.py | docker compose run --rm -T \\
      -e SCRAPPER_TOOL_AGENT_LLM=openai_compat \\
      -e SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:6543 \\
      -e SCRAPPER_TOOL_AGENT_MODEL=google/gemma-4-e4b \\
      -e SCRAPPER_TOOL_AGENT_BROWSER=patchright \\
      -e SCRAPPER_TOOL_CAPTCHA_SOLVER=none \\
      -e BENCH_OUTPUT=/tmp/bench.json \\
      --entrypoint python scrapper-tool -

Then `docker cp` the JSON out, or `cat` from stdout (the script also
prints the final JSON at the end so the host can capture it).
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from scrapper_tool.agent import AgentConfig, agent_browse, agent_extract


class _Book(BaseModel):
    title: str
    price: float


class _Catalogue(BaseModel):
    books: list[_Book]


_QUOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "author": {"type": "string"},
                },
                "required": ["text", "author"],
            },
        }
    },
    "required": ["quotes"],
}


def _model() -> str:
    return os.environ.get("SCRAPPER_TOOL_AGENT_MODEL", "(unset)")


def _now() -> float:
    return time.perf_counter()


async def _trial_e1_quotes(cfg: AgentConfig) -> dict[str, Any]:
    started = _now()
    try:
        r = await agent_extract(
            "https://quotes.toscrape.com/",
            schema=_QUOTE_SCHEMA,
            config=cfg,
            instruction="Extract every quote on the page.",
        )
        elapsed = _now() - started
        if r.error == "schema-validation-failed":
            return {
                "ok": False,
                "elapsed_s": elapsed,
                "reason": "schema-validation-failed",
            }
        quotes = r.data["quotes"] if isinstance(r.data, dict) else (r.data or [])
        return {
            "ok": not r.blocked and bool(quotes),
            "elapsed_s": round(elapsed, 2),
            "items": len(quotes),
            "blocked": r.blocked,
            "error": r.error,
            "tokens_used": r.tokens_used,
        }
    except Exception as exc:  # noqa: BLE001 — capture any failure mode
        return {
            "ok": False,
            "elapsed_s": round(_now() - started, 2),
            "exception": f"{type(exc).__name__}: {exc}",
        }


async def _trial_e1_books(cfg: AgentConfig) -> dict[str, Any]:
    started = _now()
    try:
        r = await agent_extract(
            "https://books.toscrape.com/",
            schema=_Catalogue,
            config=cfg,
            instruction="Extract every book on the page with title and price.",
        )
        elapsed = _now() - started
        if r.error == "schema-validation-failed":
            return {
                "ok": False,
                "elapsed_s": round(elapsed, 2),
                "reason": "schema-validation-failed",
            }
        books = r.data.get("books") if isinstance(r.data, dict) else None
        return {
            "ok": not r.blocked and bool(books),
            "elapsed_s": round(elapsed, 2),
            "items": len(books) if isinstance(books, list) else 0,
            "blocked": r.blocked,
            "error": r.error,
            "tokens_used": r.tokens_used,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "elapsed_s": round(_now() - started, 2),
            "exception": f"{type(exc).__name__}: {exc}",
        }


async def _trial_e2_paginate(cfg: AgentConfig) -> dict[str, Any]:
    started = _now()
    try:
        r = await agent_browse(
            "https://quotes.toscrape.com/",
            instruction=(
                "Click the 'Next' button at the bottom of the page to go to "
                'page 2, then return a JSON object {"page": 2, "count": '
                "<number of quotes shown on page 2>}."
            ),
            config=cfg,
        )
        elapsed = _now() - started
        # Score: did the agent reach page 2 AND return an integer count?
        data = r.data
        good_shape = (
            isinstance(data, dict)
            and data.get("page") in (2, "2")
            and isinstance(data.get("count"), int)
        )
        # gemma-4-e4b sometimes wraps the answer in {_raw: "..."} — count it
        # as ok if the raw text contains the right markers.
        if not good_shape and isinstance(data, dict) and isinstance(data.get("_raw"), str):
            raw = data["_raw"]
            good_shape = '"page": 2' in raw and '"count": 10' in raw
        return {
            "ok": good_shape and not r.blocked,
            "elapsed_s": round(elapsed, 2),
            "steps_used": r.steps_used,
            "blocked": r.blocked,
            "error": r.error,
            "tokens_used": r.tokens_used,
            "data_shape": (
                "good" if good_shape else f"degraded ({type(data).__name__}: {str(data)[:80]!r})"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "elapsed_s": round(_now() - started, 2),
            "exception": f"{type(exc).__name__}: {exc}",
        }


def _summarize(trials: list[dict[str, Any]]) -> dict[str, Any]:
    times = [t["elapsed_s"] for t in trials if "elapsed_s" in t]
    okay = [t for t in trials if t.get("ok")]
    items = [t["items"] for t in trials if "items" in t]
    return {
        "n_trials": len(trials),
        "n_ok": len(okay),
        "success_rate": round(len(okay) / len(trials), 3) if trials else 0.0,
        "median_s": round(statistics.median(times), 2) if times else None,
        "min_s": round(min(times), 2) if times else None,
        "max_s": round(max(times), 2) if times else None,
        "median_items": int(statistics.median(items)) if items else None,
    }


async def main() -> None:
    n_e1 = int(os.environ.get("BENCH_E1_TRIALS", "3"))
    n_e2 = int(os.environ.get("BENCH_E2_TRIALS", "2"))
    output = os.environ.get("BENCH_OUTPUT")

    cfg = AgentConfig.from_env().merged(
        captcha_solver="none",
        max_steps=int(os.environ.get("SCRAPPER_TOOL_AGENT_MAX_STEPS", "8")),
        timeout_s=float(os.environ.get("SCRAPPER_TOOL_AGENT_TIMEOUT_S", "240")),
    )

    report: dict[str, Any] = {
        "model": _model(),
        "browser": cfg.browser,
        "llm_url": cfg.ollama_url,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tests": {},
    }

    print(f"=== Benchmarking model={_model()} browser={cfg.browser} ===", flush=True)
    print(f"  E1 trials: {n_e1}, E2 trials: {n_e2}", flush=True)
    print(file=sys.stderr)

    # --- E1: quotes.toscrape.com (3 trials)
    print(f"-- E1 quotes.toscrape.com x {n_e1}", flush=True)
    trials: list[dict[str, Any]] = []
    for i in range(n_e1):
        r = await _trial_e1_quotes(cfg)
        trials.append(r)
        print(f"   trial {i + 1}: {r}", flush=True)
    report["tests"]["e1_quotes"] = {"trials": trials, **_summarize(trials)}

    # --- E1: books.toscrape.com with pydantic schema (3 trials)
    print(f"-- E1-pydantic books.toscrape.com x {n_e1}", flush=True)
    trials = []
    for i in range(n_e1):
        r = await _trial_e1_books(cfg)
        trials.append(r)
        print(f"   trial {i + 1}: {r}", flush=True)
    report["tests"]["e1_books_pydantic"] = {"trials": trials, **_summarize(trials)}

    # --- E2: paginate (2 trials, heavy)
    print(f"-- E2 paginate quotes.toscrape.com x {n_e2}", flush=True)
    trials = []
    for i in range(n_e2):
        r = await _trial_e2_paginate(cfg)
        trials.append(r)
        print(f"   trial {i + 1}: {r}", flush=True)
    report["tests"]["e2_paginate"] = {"trials": trials, **_summarize(trials)}

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    blob = json.dumps(report, indent=2, sort_keys=True)
    if output:
        await asyncio.to_thread(Path(output).write_text, blob, encoding="utf-8")
        print(f"\n>>> wrote {output}", flush=True)
    print("\n===BENCHMARK_JSON_BEGIN===")
    print(blob)
    print("===BENCHMARK_JSON_END===")


if __name__ == "__main__":
    asyncio.run(main())

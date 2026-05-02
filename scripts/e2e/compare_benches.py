"""Compare two benchmark JSON reports written by bench_model.py.

Prints a side-by-side table to stdout and writes a markdown report
to ``scripts/e2e/reports/comparison.md``. Designed to be human-readable
for ad-hoc model evaluation.

Usage:

    python scripts/e2e/compare_benches.py reports/gemma.json reports/qwen.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPORT_PATH = Path("scripts/e2e/reports/comparison.md")


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _delta(a: Any, b: Any, lower_is_better: bool = True) -> str:
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return ""
    if a == 0:
        return ""
    pct = (b - a) / a * 100
    # ASCII-safe markers so the output works on Windows cp1252 consoles.
    marker = "[BETTER]" if (pct < 0) == lower_is_better else "[WORSE]"
    return f" ({marker} {pct:+.0f}%)"


def main() -> None:
    if len(sys.argv) != 3:
        sys.stderr.write("Usage: compare_benches.py <baseline.json> <candidate.json>\n")
        sys.exit(2)

    a = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    b = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))

    a_name = a["model"]
    b_name = b["model"]

    lines: list[str] = []
    add = lines.append

    add("# Benchmark comparison")
    add("")
    add(f"- Baseline:  **{a_name}** (run at {a['started_at']})")
    add(f"- Candidate: **{b_name}** (run at {b['started_at']})")
    add(f"- Browser: {a['browser']}  LLM URL: {a['llm_url']}")
    add("")

    add("## Summary")
    add("")
    add(f"| Test | {a_name} | {b_name} |")
    add("|------|----------|----------|")
    for test_name in sorted(set(a["tests"].keys()) | set(b["tests"].keys())):
        sa = a["tests"].get(test_name, {})
        sb = b["tests"].get(test_name, {})
        # Success rate
        a_succ = f"{sa.get('n_ok', 0)}/{sa.get('n_trials', 0)}"
        b_succ = f"{sb.get('n_ok', 0)}/{sb.get('n_trials', 0)}"
        # Median wall-clock
        a_med = sa.get("median_s")
        b_med = sb.get("median_s")
        a_items = sa.get("median_items")
        b_items = sb.get("median_items")
        cell_a = f"{a_succ} ok | median {_fmt(a_med)}s | items {_fmt(a_items)}"
        cell_b = (
            f"{b_succ} ok | median {_fmt(b_med)}s{_delta(a_med, b_med, True)}"
            f" | items {_fmt(b_items)}{_delta(a_items, b_items, False)}"
        )
        add(f"| `{test_name}` | {cell_a} | {cell_b} |")
    add("")

    add("## Per-trial detail")
    add("")
    for test_name in sorted(set(a["tests"].keys()) | set(b["tests"].keys())):
        add(f"### `{test_name}`")
        add("")
        sa = a["tests"].get(test_name, {})
        sb = b["tests"].get(test_name, {})
        add(f"| trial | {a_name} | {b_name} |")
        add("|-------|---------|----------|")
        for i in range(max(len(sa.get("trials", [])), len(sb.get("trials", [])))):
            ta = sa.get("trials", [{}])[i] if i < len(sa.get("trials", [])) else {}
            tb = sb.get("trials", [{}])[i] if i < len(sb.get("trials", [])) else {}
            add(f"| {i + 1} | `{json.dumps(ta)}` | `{json.dumps(tb)}` |")
        add("")

    add("## Verdict")
    add("")
    # Aggregate: success rate, median time, total wall-clock
    a_total = sum(t.get("median_s") or 0 for t in a["tests"].values())
    b_total = sum(t.get("median_s") or 0 for t in b["tests"].values())
    a_succ_total = sum(t.get("n_ok", 0) for t in a["tests"].values())
    b_succ_total = sum(t.get("n_ok", 0) for t in b["tests"].values())
    a_n = sum(t.get("n_trials", 0) for t in a["tests"].values())
    b_n = sum(t.get("n_trials", 0) for t in b["tests"].values())

    add(f"- {a_name}: {a_succ_total}/{a_n} trials ok, sum-of-medians = {a_total:.1f}s")
    add(
        f"- {b_name}: {b_succ_total}/{b_n} trials ok, "
        f"sum-of-medians = {b_total:.1f}s{_delta(a_total, b_total)}"
    )
    add("")

    out = "\n".join(lines)
    print(out)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(out, encoding="utf-8")
    print(f"\n>>> wrote {REPORT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()

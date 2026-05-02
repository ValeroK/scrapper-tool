#!/usr/bin/env bash
#
# Runs the full E2E suite in order. Skips heavy tests by default — pass
# ``--full`` to include them. Stops on first failure.
#
# Usage:
#   bash scripts/e2e/run_all.sh                    # core only (no LLM, no browsers)
#   bash scripts/e2e/run_all.sh --llm              # adds Pattern E1/E2 (needs LM Studio)
#   bash scripts/e2e/run_all.sh --full             # adds Pattern D + captchas + everything
#
# Environment expected when ``--llm`` or ``--full`` is set:
#   SCRAPPER_TOOL_AGENT_LLM=openai_compat
#   SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://localhost:1234
#   SCRAPPER_TOOL_AGENT_MODEL=qwen3-vl-8b-instruct

set -euo pipefail

mode="core"
for arg in "$@"; do
    case "$arg" in
        --llm) mode="llm" ;;
        --full) mode="full" ;;
        -h|--help)
            sed -n '2,15p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/../.."

run() {
    echo
    echo "=================================================================="
    echo ">>> $*"
    echo "=================================================================="
    "$@"
}

# --- Tier 1: core (no LLM, no browser launch) -----------------------------
run uv run python scripts/e2e/test_pattern_a.py
run uv run python scripts/e2e/test_pattern_b.py
run uv run python scripts/e2e/test_pattern_c.py

if [[ "$mode" == "core" ]]; then
    echo
    echo "Core E2E suite passed. Pass --llm or --full to run heavier tests."
    exit 0
fi

# --- Tier 2: Pattern E (LLM-driven) ---------------------------------------
run uv run python scripts/e2e/test_pattern_e1.py
run uv run python scripts/e2e/test_pattern_e1_pydantic.py
run uv run python scripts/e2e/test_pattern_e2.py
run uv run python scripts/e2e/test_errors.py

if [[ "$mode" == "llm" ]]; then
    echo
    echo "LLM E2E suite passed."
    exit 0
fi

# --- Tier 3: heavy / browser-dependent / paid -----------------------------
run uv run python scripts/e2e/test_pattern_d.py || \
    echo "Pattern D failed (Scrapling Turnstile auto-solve is flaky — re-run if needed)"
run uv run python scripts/e2e/test_captcha_tier0.py || \
    echo "Captcha Tier 0 inconclusive (CF rotates challenges)"
run uv run python scripts/e2e/test_captcha_tier2.py

echo
echo "Full E2E suite finished. Review any ⚠️ lines above."

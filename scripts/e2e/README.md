# scripts/e2e

Runnable end-to-end test scripts referenced by [docs/E2E_TEST_PLAN.md](../../docs/E2E_TEST_PLAN.md).

Each script is self-contained:

```bash
uv run python scripts/e2e/test_pattern_a.py
uv run python scripts/e2e/test_pattern_b.py
uv run python scripts/e2e/test_pattern_c.py
uv run python scripts/e2e/test_pattern_d.py        # heavy — Pattern D (Scrapling)
uv run python scripts/e2e/test_pattern_e1.py       # heavy — Pattern E1 (LLM)
uv run python scripts/e2e/test_pattern_e1_pydantic.py
uv run python scripts/e2e/test_pattern_e2.py       # heavy — Pattern E2 (LLM agent loop)
uv run python scripts/e2e/test_captcha_tier0.py    # requires Camoufox installed
uv run python scripts/e2e/test_captcha_tier2.py    # requires SCRAPPER_TOOL_CAPTCHA_KEY
uv run python scripts/e2e/test_errors.py           # error taxonomy

# MCP-from-agent simulation — runs INSIDE Docker, spawns scrapper-tool-mcp
# as a sibling subprocess and drives all 6 tools via stdio JSON-RPC.
# This is the canonical "operator demo of an agent driving the MCP server".
cat scripts/e2e/test_mcp_session.py | docker compose run --rm -T \
  -e SCRAPPER_TOOL_AGENT_LLM=openai_compat \
  -e SCRAPPER_TOOL_AGENT_OLLAMA_URL=http://host.docker.internal:6543 \
  -e SCRAPPER_TOOL_AGENT_MODEL=google/gemma-4-e4b \
  -e SCRAPPER_TOOL_AGENT_BROWSER=patchright \
  --entrypoint python scrapper-tool -
```

Or run the whole suite (skips heavy tests by default):

```bash
bash scripts/e2e/run_all.sh
```

The full suite expects `SCRAPPER_TOOL_AGENT_LLM=openai_compat` plus
`SCRAPPER_TOOL_AGENT_OLLAMA_URL` pointing at a running LM Studio (or
equivalent OpenAI-compatible server). See the test plan for setup steps.

> These scripts hit real public sites. Do not loop them in tight cycles.
> Treat each invocation as a single probe and respect target sites' robots.txt.

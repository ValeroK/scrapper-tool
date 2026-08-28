# 3.1 — migrate to the MCP 2.x SDK

Status: **approved, not started.** Written 2026-08-28, immediately after the
3.0.0 release (`8441e11`).

Every file:line anchor below was re-verified against `8441e11` at the time of
writing. If you are reading this much later, spot-check a few before trusting
them — `mcp.py` is edited often.

---

## Why this exists

`uv lock --upgrade` resolved `mcp` 2.1.1 during the 3.0.0 dependency upgrade and
the MCP server stopped working: 2.x renamed `FastMCP` to `MCPServer` and moved it
from `mcp.server.fastmcp` to `mcp.server.mcpserver`, so `_build_server`'s import
fails and `scrapper-tool-mcp` cannot start. We capped `mcp<2` to unblock the
release.

The cap is a deferral, not a fix. 1.x already needed a floor of `>=1.28.1` for
PYSEC-2026-3481/2/3, so the window between floor and cap is narrow.
`CONTRIBUTING.md:23` names this exact situation as a standing trigger: *"The MCP
SDK ships a major version → migrate `mcp.py` and bump the `[agent]` extra pin."*

**The migration is mechanically small. It needs a plan because the suite
currently cannot tell whether it worked.** Under 2.x all MCP-path tests *skip*
rather than fail, and the skip message blames a missing `[agent]` extra that is
in fact installed. A green run means nothing here.

## What actually changed in the SDK

Established by installing `mcp>=2,<3` in a throwaway venv and introspecting it —
not from the migration guide, and not guessed.

**Compatible, no work needed:**
- `run(transport=...)` — same three literals
- `@server.tool(name=..., description=...)`
- `server._tool_manager._tools` and `tool.fn` — so every test's introspection
  path survives
- `name=` and `instructions=` on the constructor

**Changed:**
1. `mcp.server.fastmcp.FastMCP` → `mcp.server.mcpserver.MCPServer`.
2. **`host` / `port` removed from `__init__`** — now keyword args on
   `run_sse_async` / `run_streamable_http_async`, reachable via `run(**kwargs)`.
3. `run(transport="stdio")` dispatches `anyio.run(self.run_stdio_async)` and
   **silently discards kwargs**. Passing host/port on stdio is a no-op, not an
   error.
4. No compatibility shim — importing the old module raises `ModuleNotFoundError`
   with a migration-guide URL.

---

## Work

### 1. `_build_server` — the migration itself

`src/scrapper_tool/mcp.py:1030`, import at `:1053`. Swap the import and class.
**Drop the `host` / `port` parameters** — they only fed the constructor, `main()`
is the sole caller that passes them, and all five test call sites already call it
bare.

Keep the lazy import. It is load-bearing beyond the optional-extra story:
`tests/conftest.py:75-78` imports `scrapper_tool.mcp` in an **autouse** fixture,
so the module must stay importable with no SDK present or the whole unit suite
dies.

### 2. `main()` — relocate host/port to `run()`

`src/scrapper_tool/mcp.py:1708`. Pass them **only for the two HTTP transports**.
Always passing would also work, but relying on a silent discard hides intent and
breaks if the SDK ever validates kwargs.

### 3. Stop misdiagnosing an API break as a missing extra

`src/scrapper_tool/mcp.py:1056` catches `ImportError` and re-raises *"requires the
[agent] extra"*. A wrong module path raises `ModuleNotFoundError` — an
`ImportError` subclass — so it is reclassified as "extra not installed", and
`main()` turns that into a clean exit 1 with a misleading message and no
traceback. **That is exactly how this migration fails quietly.** Distinguish the
two cases and name the expected module.

### 4. The `importorskip` guards — what makes this verifiable

`tests/unit/test_mcp.py:44`, `tests/unit/test_agent_mcp.py:16`,
`tests/unit/test_crawl_endpoints.py:444`.

Each asks *"can I import `mcp.server.fastmcp`?"* and infers *"is `[agent]`
installed?"*. The same question until 2.x renamed the module. Split them: guard on
`importorskip("mcp")`, and let the specific `mcp.server.mcpserver` import fail
**loudly**.

The guards no longer discriminate anything anyway — `test_mcp.py:38-43` and
`ci.yml:47-59` both still reason about matrix rows (`extras=dev`,
`extras=dev,hostile`) that no longer exist. `ci.yml` has one `extras` value now,
`dev,full,agent,http`, so every test-running row installs `[agent]` and the guard
can only fire as a false negative. Update those comments.

### 5. Fix the blast radius in `test_crawl_endpoints.py`

Its `importorskip` sits at module level on line **444**, after `TestMapEndpoint`
(line 127) and `TestCrawlEndpoint` (line 203). `importorskip` skips the whole
*module*, so under 2.x it also skipped **13 REST `/map` and `/crawl` tests with
nothing to do with the MCP SDK**. Convert it to a class-level `pytest.mark.skipif`
on `TestMcpParity` (line 455), the only class that needs it.

### 6. Un-mock the transport plumbing

`tests/unit/test_mcp.py` replaces `_build_server` with a `MagicMock` at lines
1116, 1133 and 1254 — all inside the tests of the wiring this change touches. So
the only tests covering host/port flow never construct a real server, and would
pass against an SDK whose constructor rejects `host=`.

- `TestMainTransportPlumbing` (line 1238) asserts
  `build_mock.assert_called_once_with(host="0.0.0.0", port=8765)` around line
  1269-1270 — wrong by construction once host/port move. Rewrite against
  `run()`'s kwargs, and assert stdio gets **no** host/port.
- Add one test driving `main()` through a **real** `_build_server`. This is the
  test that would have caught the 2.x break.
- `test_extra_not_installed_exits_1` (line 1123) raises its own `ImportError`, so
  the real message at `mcp.py:1056` is pinned by nothing. Pin it alongside item 3.

### 7. Fix the docker-compose MCP services

Both are broken today, independently of the SDK, and both claim otherwise.
Verified at `8441e11`: the only `entrypoint:` keys in the file are at lines 206
and 235, belonging to other services.

- **`scrapper-tool` (stdio, from `docker-compose.yml:48`)** — the comment at
  `:89-90` says *"Default entrypoint is `scrapper-tool-mcp` — the stdio MCP
  server"*, but the service has **no `entrypoint:` key**, so it inherits
  `ENTRYPOINT ["scrapper-tool-serve"]` from `Dockerfile:174`. It runs the REST
  sidecar with stdin attached. The spawn pattern documented at `docs/mcp.md:118-122`
  (`docker compose run --rm -T scrapper-tool`) cannot work as written. Add
  `entrypoint: ["scrapper-tool-mcp"]`.
- **`scrapper-tool-mcp-http` (from `docker-compose.yml:104`)** — same missing key,
  so its `SCRAPPER_TOOL_MCP_TRANSPORT: "streamable-http"` / `_HOST` / `_PORT` env
  vars are read by nobody: only `scrapper-tool-mcp`'s `_parse_args` consults them.
  The container therefore binds the REST port 5792 while compose publishes 8000, so
  nothing answers on the published port and the healthcheck (`wget
  http://127.0.0.1:8000/mcp`) can never pass — the service is permanently
  unhealthy by construction. Add the entrypoint.
- **Healthchecks.** `Dockerfile:163` curls the REST `/health`, so any
  MCP-entrypoint container is unhealthy regardless. Disable the inherited
  healthcheck on the stdio service (it exposes no port at all) and keep the `/mcp`
  probe on the HTTP one, which becomes meaningful once the entrypoint is right.
  Note `/mcp` is an SDK-owned mount path — confirm 2.x still uses it.
- **The same false claim is in the docs**: `docs/docker.md:50` and `:120-123` say
  the image's default entrypoint is `scrapper-tool-mcp`. Those two came in when the
  section moved out of the README, inheriting the error. Fix all three sites.

Verify by actually starting both services and completing a JSON-RPC handshake —
this is the part that has never been exercised.

### 8. Check the client-side SDK in `scripts/e2e/`

`scripts/e2e/test_mcp_session.py` and `test_mcp_session_http.py` are the **only**
things in the repo that speak the MCP wire protocol, and they use client APIs this
migration does not otherwise touch: `ClientSession`, `StdioServerParameters`,
`mcp.client.stdio.stdio_client`,
`mcp.client.streamable_http.streamablehttp_client` (unpacked as a 3-tuple), and
`result.content[0].text` off a `CallToolResult`. Verify each against 2.x.

They are never collected (`testpaths = ["tests"]`) and never linted
(`ruff check src/ tests/`), so nothing reports when they rot.

### 9. Lift both version bounds

- `pyproject.toml:57` — `agent = ["mcp>=1.2,<2"]`
- `pyproject.toml:242` — `"mcp>=1.28.1,<2"` in `[tool.uv] override-dependencies`

**Both.** `override-dependencies` *replaces* rather than intersects, so changing
only the extra is a no-op — that is how the `<2` cap was silently defeated the
first time, recorded in the comment at `pyproject.toml:234-241`.

The cap rationale above it is an argument for *not* migrating; it becomes actively
misleading the moment this lands. Replace it with the resulting floor and why.
Then `uv lock` and `uv sync`.

### 10. Make the tool surface drift-checkable

The server registers **9** tools (`grep -c "@server.tool(" src/scrapper_tool/mcp.py`).
`docs/mcp.md:22-28` and `docs/agent-integration.md:23-31` list **7** — both omit
`map_site` and `crawl_site`; `docs/agent-integration.md:51` says "the six tools".
The `instructions=` string at `mcp.py:1063` also names only 7, so the guidance the
LLM actually reads omits two tools that exist.

`docs/openapi/` is protected from exactly this by `openapi-spec-check`
(`ci.yml:132-149`). The MCP surface has no equivalent, and `scripts/e2e`'s
tool-name sets are stale *and* subset-only, so they cannot fail on an addition.

Commit a generated `tools/list` snapshot plus a drift test mirroring the OpenAPI
job. That turns every table below into one file CI enforces.

### 11. Documentation sync

- Tool tables to 9: `docs/mcp.md:22-28`, `docs/agent-integration.md:23-31,51`.
- `instructions=` string (`mcp.py:1063`) — add `map_site`, `crawl_site`.
- `pyproject.toml:42-45` — the `[agent]` comment lists 4 tools.
- `docs/PROGRESS.md:141-152` — says "all 68 skip"; the real count is **80**. Move
  the entry from "in flight" to shipped.
- `CONTRIBUTING.md:32` — *"Run `tests/unit/test_mcp.py` to catch breaking
  changes"* is the instruction that failed here; it only works once item 4 lands.

### 12. Verify the coverage claim before trusting it

`docs/PROGRESS.md` records that the suite went green under 2.x. That should not
have been possible: `--cov-fail-under=85` is enforced (`ci.yml:76`),
`[tool.coverage.run]` has no `omit`, and `mcp.py` is among the largest modules in
`src/` (517 statements at 3.0.0) — skipping 80 tests ought to sink the total below
the floor.

Coverage was never run in that state, so the claim is unverified. Reproduce it and
correct the doc, or find out why the floor did not catch it — because a floor that
misses 80 skipped tests will miss the next silent regression too.

---

## Out of scope for 3.1, flagged not fixed

- `ci.yml:78` gates the Codecov upload on `matrix.extras == 'dev,agent,http'`, a
  value the matrix never produces — coverage is never uploaded.
- Making `scripts/e2e/` collectable by pytest: they need a live LLM and a
  container, so they stay in the manual tier.
- `doctor` cannot tell whether the captcha vision model can *see*.
  `_captcha_vision_state` (`doctor.py:318`) calls `probe_llm`, which only checks
  reachability and that a model of that name is served — it never sends an image.
  A text-only model reports `ok` and then fails every grid at solve time. Measured
  2026-08-28: the real `complete_vision` path against `qwen3.8-27b-apex` returned
  exactly `2,4,9` on a synthetic 3x3 grid, so the path itself is sound; it is the
  *probe* that is shallow. Fix would be an opt-in `doctor --deep` that sends a
  small image.

## Verification for 3.1

**The decisive check, and the one that was missing:** with `mcp` 2.x installed,
the MCP tests must **run**, not skip.

```bash
uv sync --extra dev --extra full --extra agent --extra http
uv run pytest tests/unit/test_mcp.py tests/unit/test_agent_mcp.py \
              tests/unit/test_crawl_endpoints.py -q
```

Assert on the count — 80 collected, 0 skipped for SDK reasons. **A green run
*with* skips is the failure this plan exists to prevent.**

Then the standard gate set:

```bash
uv run pytest -q --cov --cov-fail-under=85
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
uv run mypy src/
SCRAPPER_TOOL_TIER_PROBE=1 uv run pytest tests/unit -q
uv run python scripts/dump_openapi.py && git diff --exit-code docs/openapi/
uv run pip-audit --skip-editable
```

Plus the wire protocol, which nothing in CI covers:

```bash
uv run python scripts/e2e/test_mcp_session.py          # stdio JSON-RPC handshake
docker compose run --rm -T scrapper-tool               # now actually stdio MCP
docker compose --profile http up -d scrapper-tool-mcp-http
docker compose ps                                      # must report healthy
uv run python scripts/e2e/test_mcp_session_http.py
```

Both must list **9** tools — their current `expected` sets name 6 and assert
subset-only, so fix those or they pass against a server missing three tools.

Note mypy proves little here: `pyproject.toml:346` puts `mcp.*` under
`ignore_missing_imports`, the lint job installs only `[dev]`, and `_build_server`
returns `Any`, so a wrong import path type-checks clean.

Confirm stdio stays clean: `mcp.py:1677` is the module's only stdout `print()`,
reachable only on `--help` before `run()` starts. `mcp.py:852-854` logs from
inside a tool body while stdio JSON-RPC is live — verify `_logging` writes to
stderr, since anything on stdout corrupts the framing.

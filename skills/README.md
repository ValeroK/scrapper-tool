# Agent skill: teaching an LLM to use scrapper-tool

[`scrapper-tool/SKILL.md`](scrapper-tool/SKILL.md) is a self-contained instruction
file that teaches an LLM agent — Claude, Cursor, or any coding/agent tool — how
to drive scrapper-tool: which entrypoint to call, how the auto-escalating cascade
works, how to ask for structured data, and how to read the result.

It's written in the [Agent Skills](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
format (YAML frontmatter + Markdown body), which is portable — the same file
works as a Claude skill, a Cursor rule, or a plain context document for any other
agent.

There are two distinct things you can hand an agent, and you often want both:

1. **The skill (this file)** — the *knowledge* of how to use the tool. It makes
   the agent call scrapper-tool correctly and interpret what comes back.
2. **The MCP server** — the *ability* to actually run scrapes from inside the
   agent, as callable tools. Start it with `scrapper-tool-mcp` (see below).

The skill references the MCP tools by name, so pairing them is the intended
setup: register the MCP server for capability, load the skill for know-how.

## Install into Claude Code

Copy the skill into your project (or your personal skills dir):

```bash
mkdir -p .claude/skills
cp -r skills/scrapper-tool .claude/skills/
```

Claude Code auto-discovers it; the `description` frontmatter decides when it's
surfaced. For the runnable tools, also add the MCP server to `.mcp.json`:

```json
{ "mcpServers": { "scrapper-tool": { "command": "scrapper-tool-mcp", "args": [] } } }
```

## Install into Claude Desktop

Skills load from the Desktop skills directory; the MCP server goes in
`claude_desktop_config.json` with the same `mcpServers` block as above. See
[`docs/agent-integration.md`](../docs/agent-integration.md) for the exact path.

## Install into Cursor

Cursor reads project rules from `.cursor/rules/`. Point one at the skill body:

```bash
mkdir -p .cursor/rules
cp skills/scrapper-tool/SKILL.md .cursor/rules/scrapper-tool.mdc
```

(The YAML frontmatter is compatible with Cursor's `.mdc` rule format.) Cursor
supports MCP servers too — add the same `scrapper-tool-mcp` command in Cursor's
MCP settings.

## Install into any other agent (AutoGen, LangChain, custom)

Two options, use either or both:

- **As context:** feed the contents of `SKILL.md` into the system prompt or a
  retrieval doc. That alone lets the agent generate correct `scrape()` /
  `auto_scrape` calls.
- **As tools:** register the MCP server over stdio (`scrapper-tool-mcp`) using
  your framework's MCP adapter — `mcp-use` for the Anthropic SDK, the community
  MCP toolkits for LangChain/AutoGen. Per-framework wiring is in
  [`docs/agent-integration.md`](../docs/agent-integration.md).

## Keeping the skill accurate

The skill documents the public tool/API surface (`auto_scrape`, `scrape()`,
`/scrape`, the site-level tools, and the result payload). If that surface changes,
update `SKILL.md` in the same PR — a skill that describes tools the code no longer
has is worse than none.

# Running scrapper-tool as a stdio MCP server

The bundled Docker image's default entrypoint is `scrapper-tool-serve` (the REST sidecar on port 5792) since v1.1.2. To run it as a stdio MCP server instead, override the entrypoint.

## Docker run

```bash
docker run --rm -i \
    --entrypoint scrapper-tool-mcp \
    ghcr.io/valerok/scrapper-tool:latest
```

`-i` keeps stdin open — MCP-stdio clients pipe JSON-RPC over it.

## Docker compose

```yaml
services:
  scrapper-mcp:
    image: ghcr.io/valerok/scrapper-tool:latest
    entrypoint: ["scrapper-tool-mcp"]
    stdin_open: true
    tty: false
    # No port mapping — stdio MCP doesn't listen on a TCP port.
```

## Claude Desktop / mcp.json

```json
{
  "mcpServers": {
    "scrapper-tool": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--entrypoint", "scrapper-tool-mcp",
        "ghcr.io/valerok/scrapper-tool:latest"
      ]
    }
  }
}
```

## Why the entrypoint changed in v1.1.2

The README + [`http-sidecar.md`](http-sidecar.md) treat the REST sidecar as the primary surface for non-MCP callers. Pre-1.1.2 the image's default entrypoint was `scrapper-tool-mcp`, which forced every REST caller to override it (and the override was easy to miss — the failure mode was `unknown argument: 'scrapper-tool-serve'`). Flipping the default to `scrapper-tool-serve` matches the docs; MCP-mode users now carry the one-liner override above.

The `scrapper-tool-mcp` console script is unchanged. Both `scrapper-tool-serve` and `scrapper-tool-mcp` are installed in the image; only the *default* moved.

## All three console scripts

| Script | Mode | Default in v1.1.2 image? |
|---|---|---|
| `scrapper-tool-serve` | REST sidecar (FastAPI on :5792) | **yes** |
| `scrapper-tool-mcp` | Stdio MCP server | no — override `--entrypoint` |
| `scrapper-tool` | CLI (one-shot canary, etc.) | no — override `--entrypoint` |

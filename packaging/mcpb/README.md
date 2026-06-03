# MCPB bundle (Smithery / Claude Desktop one-click)

This packages db-conn-mcp as an **MCP Bundle** (`.mcpb`) — the format behind
Claude Desktop's one-click extensions and Smithery's local-server publishing.

The bundle is tiny: a `manifest.json` plus a `server/main.py` launcher. The launcher
uses **`uv run` with PEP 723 inline dependencies**, so it installs `db-conn-mcp` from
PyPI into an ephemeral environment at launch — nothing is vendored, and it always
tracks the published release.

> **Prerequisite for end users:** [`uv`](https://docs.astral.sh/uv/) must be installed
> (the bundle launches via `uv run`).

## Build

```bash
cd packaging/mcpb
npx @anthropic-ai/mcpb validate manifest.json   # optional
npx @anthropic-ai/mcpb pack . ../db-conn-mcp.mcpb
```

Produces `packaging/db-conn-mcp.mcpb` (git-ignored; also attached to GitHub releases).

## Publish to Smithery

1. Install the CLI: `npm i -g @smithery/cli` (or use `npx @smithery/cli@latest …`).
2. Authenticate: `smithery auth login` (browser) **or** create an API key in the
   Smithery dashboard (Account → API Keys) and `export SMITHERY_API_KEY=<key>`.
3. Publish:

   ```bash
   smithery mcp publish ./packaging/db-conn-mcp.mcpb -n Idle-Sync/db-conn-mcp
   ```

## Use in Claude Desktop

The same `.mcpb` can be double-clicked / dragged into Claude Desktop's Extensions to
install db-conn-mcp locally. It will prompt for the `connections.json` path defined in
`manifest.json`'s `user_config`.

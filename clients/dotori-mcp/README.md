# Dotori MCP adapter

This is an optional, read-only MCP server for a Dotori deployment. It calls
the existing Bearer API and never reads the Django database directly. The
server therefore uses the same token scopes, Workspace selection, stale-result
checks, and capability availability as the web and CLI clients.

Install it separately from the Dotori Core image:

```bash
pipx install ./clients/dotori-mcp
DOTORI_SERVER_URL=http://127.0.0.1:8000 \
DOTORI_TOKEN=dtr_cli_... \
DOTORI_WORKSPACE=<optional-workspace-uid> \
dotori-mcp
```

The exposed tools are `server_status`, `capabilities`, `list_documents`,
`list_results`, `read_result`, and `search`. No upload, delete, retry, or
workspace mutation tool is exposed.

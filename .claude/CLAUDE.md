
<!-- cw-onboarding -->
## cw Agent Integration

This workspace is managed by `cw`. Background sessions receive tasks
via the MCP channels wired in `.mcp.json`.

- Queue events: `cw-queue-events` MCP server (port 8789)
- PR events: `cw-pr-events` MCP server (port 8788)
- Dispatch status: `cw orchestrate status` (SessionStart hook)
- Run `cw schema <command>` for machine-readable output schemas;
  most cw commands accept `--json`.
- Example: `cw schema list` shows all available schemas.

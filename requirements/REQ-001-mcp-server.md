# Requirements: MCP Server Implementation for OWID Data Tools

## Goal
Expose existing `owid_tools.py` functionality as a remote MCP server using FastMCP and streamable HTTP transport, without modifying the original file.

## Design
- Use `FastMCP` class.
- Import helpers from `owid_tools.py`.
- Expose 4 tools: `search_owid`, `get_owid_data`, `chart_owid_data`, `custom_chart`.
- Use `streamable-http` transport on `0.0.0.0:PORT` (default 8000).
- Configuration via environment variables: `MAX_TABLE_ROWS`, `MAX_SEARCH_RESULTS`, `PORT`.

## Constraints
- No changes to `owid_tools.py`.
- No direct usage of FastAPI.
- No OAuth/Auth.
- Proper requirement list.

"""Tool modules — кожен реєструє свої функції на `app.mcp.server.mcp`
через `@mcp.tool()` як побічний ефект імпорту. Порядок імпорту в
`app/mcp/server.py` фіксує порядок `tools/list` (план §3.1)."""

from __future__ import annotations

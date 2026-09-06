"""DTO для `GET /api/v1/mcp/catalog` (task-77).

Форма тут — це те, що сторінка `#/mcp` уміє показати про MCP-сервер:
тули з людськими назвами й аргументами, prompts із текстом-заготовкою,
resources. Джерело правди — сам `app.mcp.server.mcp` (`app/api/mcp_catalog.py`
його читає), тому ці DTO навмисно не дублюють жодного протокольного поля,
якого там нема — розсинхрон між сторінкою й сервером мав би читатись як
брехня, а не як застарілий довідник.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class McpArgSpec(BaseModel):
    name: str
    type: str
    required: bool
    description: str


class McpToolSpec(BaseModel):
    name: str
    title: str
    group: str
    summary: str
    description: str
    read_only: bool
    args: list[McpArgSpec]


class McpPromptArgSpec(BaseModel):
    name: str
    required: bool
    description: str


class McpPromptSpec(BaseModel):
    name: str
    title: str
    description: str
    args: list[McpPromptArgSpec]
    rendered_text: str


class McpResourceSpec(BaseModel):
    uri: str
    name: str
    description: str
    mime_type: str


class McpStarterPrompt(BaseModel):
    """Звичайний копійований запит для чату — працює й без жодного
    підключеного MCP-prompt-а, показує цінність до того, як людина
    щось налаштувала (план task-77 §4 п.4)."""

    text: str
    scenario: str


class McpConnectSnippet(BaseModel):
    host: str
    title: str
    config: str
    note: str


class McpCatalogDTO(BaseModel):
    enabled: bool
    server_name: str
    server_version: str
    api_base_url: str
    groups: list[str]
    tools: list[McpToolSpec]
    # One copyable briefing mentioning EVERY tool by name (task-77 follow-up:
    # "a prompt that covers all tools") — for pasting into an assistant that
    # already has the server connected but hasn't been told what's on it,
    # distinct from the two scenario-specific MCP `prompts` below.
    all_tools_prompt: str
    prompts: list[McpPromptSpec]
    resources: list[McpResourceSpec]
    starter_prompts: list[McpStarterPrompt]
    connect: list[McpConnectSnippet] = Field(default_factory=list)

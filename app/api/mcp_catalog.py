"""MCP-каталог для людини (task-77): `#/mcp` читає це, а не грає у вгадайку
з `app/mcp/**` напряму.

**Каталог генерується з живого сервера, не пишеться руками.** Хвиля 2
task-74 додасть тули — захардкоджена сторінка розсинхронізувалась би
тихо, а документація, яка бреше, гірша за відсутню (план task-77 §2).
Тому кожне поле нижче читається з `app.mcp.server.mcp.list_tools()` /
`list_prompts()` / `list_resources()`, і єдиний тест анти-розсинхрону
(`tests/unit/api/test_mcp_catalog_api.py`) звіряє множину назв тулів
у відповіді з тим самим списком.

Напрям імпорту `app/api → app/mcp` **дозволений**: інваріант ізоляції
task-74 §2 односторонній (`app/mcp/**` не сміє імпортувати доменний шар),
і AST-тест `tests/unit/mcp/test_isolation.py` сканує лише `app/mcp/**`.

Імпорт `app.mcp.server` виконує лише декоратори реєстрації — жодного
з'єднання, сокета чи фонового потоку він не відкриває (`ApiClient`
створюється лениво в `get_api_client()`, тобто лише коли МОДЕЛЬ
викликає тул, а не коли людина відкриває цю сторінку). Перевірено
явним тестом, не коментарем.
"""

from __future__ import annotations

import re

from fastapi import APIRouter

from app.api import register_router
from app.core.config import get_settings
from app.mcp.server import mcp
from app.schemas.mcp_catalog import (
    McpArgSpec,
    McpCatalogDTO,
    McpConnectSnippet,
    McpPromptArgSpec,
    McpPromptSpec,
    McpResourceSpec,
    McpStarterPrompt,
    McpToolSpec,
)

router = APIRouter(prefix="/api/v1/mcp", tags=["mcp"])

# Мапа порядку реєстрації (алфавіт імпорту в `app/mcp/server.py`) на групу зі
# смислом (план task-74 §5). Навмисно НЕ керує порядком видачі тулів — той
# лишається порядком реєстрації заради детермінізму `tools/list` (план §3.1,
# кеш моделі); групування — робота подачі, а не протоколу. Мапа обов'язково
# вичерпна: тул без запису тут провалить `test_every_tool_has_exactly_one_group`
# замість того, щоб тихо зникнути зі сторінки.
_TOOL_GROUPS: dict[str, str] = {
    "list_clients": "Orientation",
    "get_client_overview": "Orientation",
    "find_archived_url": "Web archive",
    "get_url_snapshot_timeline": "Web archive",
    "compare_url_versions": "Web archive",
    "list_archive_changes": "Web archive",
    "list_site_changes": "Site monitor",
    "diff_monitored_page": "Site monitor",
    "get_client_monthly_performance": "Reports",
    "get_report_data": "Reports",
    "run_client_report": "Reports",
    "get_job_status": "Jobs",
}

# Рівно один тул хвилі 1 пише (запускає збір проти реальної квоти
# провайдера) — решта одинадцять лише читають. Явний allowlist замість
# "усе, крім..." — новий тул за замовчуванням вважається write, і мовчки
# лишитись непозначеним небезпечніше, ніж мовчки лишитись read-only.
_WRITE_TOOLS: frozenset[str] = frozenset({"run_client_report"})

_TITLES: dict[str, str] = {
    "list_clients": "List clients",
    "get_client_overview": "Client overview",
    "find_archived_url": "Find an archived URL",
    "get_url_snapshot_timeline": "URL snapshot timeline",
    "compare_url_versions": "Compare URL versions",
    "list_archive_changes": "List archive changes",
    "list_site_changes": "List site changes",
    "diff_monitored_page": "Diff a monitored page",
    "get_client_monthly_performance": "Monthly performance",
    "get_report_data": "Get report data",
    "run_client_report": "Run a client report",
    "get_job_status": "Check job status",
}

_GROUP_ORDER: list[str] = ["Orientation", "Web archive", "Site monitor", "Reports", "Jobs"]

_ARG_LINE = re.compile(r"^(\w+):\s*(.*)$")


def _split_summary_and_args(docstring: str) -> tuple[str, str, dict[str, str]]:
    """Google-style docstring → (summary, prose-без-Args, {arg: опис}).

    SDK `mcp` віддає в `Tool.description` увесь докстрінг дослівно (live-
    перевірено — жодного власного парсингу параметрів воно не робить,
    `input_schema.properties[x]` несе лише `type`/`title`, без опису). Це
    зроблено тут, а не в `app/mcp/tools/**`, бо докстрінги пишуться для
    моделі (яка сам докстрінг і отримує в `description`) — сторінці
    потрібне ЩЕ й розбиття на поля, моделі воно не потрібне.
    """
    text = re.sub(r"\n[ \t]+", "\n", docstring.strip())
    if "\nArgs:\n" not in f"\n{text}":
        # No `Args:` block at all (e.g. `list_clients`, which takes none) —
        # still take only the first paragraph as `summary`, same as the
        # branch below, so a card's blurb length doesn't depend on whether
        # its tool happens to have arguments.
        first_paragraph = text.strip().split("\n\n")[0].strip()
        return first_paragraph, text.strip(), {}
    before, _, after = text.partition("Args:")
    summary = before.strip().split("\n\n")[0].strip()
    args: dict[str, str] = {}
    current: str | None = None
    for line in after.strip("\n").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _ARG_LINE.match(stripped)
        if match:
            current = match.group(1)
            args[current] = match.group(2).strip()
        elif current is not None:
            args[current] = f"{args[current]} {stripped}".strip()
    return summary, before.strip(), args


def _schema_type(prop: dict[str, object]) -> str:
    if "type" in prop:
        return str(prop["type"])
    variants = prop.get("anyOf")
    if isinstance(variants, list):
        names = [str(v.get("type")) for v in variants if isinstance(v, dict) and "type" in v]
        if names:
            return " | ".join(names)
    return "any"


async def _build_tools() -> list[McpToolSpec]:
    specs: list[McpToolSpec] = []
    for tool in await mcp.list_tools():
        group = _TOOL_GROUPS.get(tool.name)
        if group is None:
            raise RuntimeError(
                f'MCP tool "{tool.name}" has no entry in _TOOL_GROUPS — '
                "add it before it can appear on the catalog page."
            )
        summary, _, arg_docs = _split_summary_and_args(tool.description or "")
        schema = tool.input_schema or {}
        properties = schema.get("properties", {})
        required = set(schema.get("required") or [])
        args = [
            McpArgSpec(
                name=name,
                type=_schema_type(prop),
                required=name in required,
                description=arg_docs.get(name, ""),
            )
            for name, prop in properties.items()
        ]
        specs.append(
            McpToolSpec(
                name=tool.name,
                title=_TITLES.get(tool.name, tool.name),
                group=group,
                summary=summary,
                description=tool.description or "",
                read_only=tool.name not in _WRITE_TOOLS,
                args=args,
            )
        )
    return specs


async def _build_prompts() -> list[McpPromptSpec]:
    specs: list[McpPromptSpec] = []
    for prompt in await mcp.list_prompts():
        args = [
            McpPromptArgSpec(
                name=a.name,
                required=bool(a.required),
                description=a.description or "",
            )
            for a in (prompt.arguments or [])
        ]
        # Плейсхолдери замість реальних значень — рендеримо готовий текст
        # заготовки, не запит на дані живого клієнта (§4.1 плану task-77).
        placeholder_args = {a.name: f"{{{{{a.name}}}}}" for a in args}
        rendered = await mcp.get_prompt(prompt.name, placeholder_args)
        if not hasattr(rendered, "messages"):
            # MRTR (план §3.1): prompt повернув `InputRequiredResult` замість
            # готового тексту навіть із плейсхолдерами — самому prompt-у
            # тоді бракує аргументу, який ми не передали; це баг у самому
            # prompt-і, а не привід тихо показати порожню заготовку.
            raise RuntimeError(f'Prompt "{prompt.name}" needs more input than its declared args.')
        rendered_text = "\n\n".join(
            msg.content.text for msg in rendered.messages if hasattr(msg.content, "text")
        )
        specs.append(
            McpPromptSpec(
                name=prompt.name,
                title=prompt.title or prompt.name,
                description=prompt.description or "",
                args=args,
                rendered_text=rendered_text,
            )
        )
    return specs


async def _build_resources() -> list[McpResourceSpec]:
    return [
        McpResourceSpec(
            uri=str(r.uri),
            name=r.name or str(r.uri),
            description=r.description or "",
            mime_type=r.mime_type or "text/plain",
        )
        for r in await mcp.list_resources()
    ]


def _build_all_tools_prompt(tools: list[McpToolSpec], groups: list[str]) -> str:
    """One copyable briefing that names every registered tool — generated,
    not hand-maintained, for the same reason the rest of this module is:
    a wave-2 tool must show up here without anyone remembering to update a
    hardcoded paragraph. Grouped in the same order as the Tools section so
    the two read the same way."""
    lines = [
        "You have access to the yovth-clients-report MCP server. Here is "
        "every tool on it — use the one that answers the question, and "
        "don't guess at data you could just call a tool for:",
        "",
    ]
    for group in groups:
        group_tools = [t for t in tools if t.group == group]
        if not group_tools:
            continue
        lines.append(f"{group}:")
        for tool in group_tools:
            write_note = (
                " (writes — starts a real job, ask before calling)" if not tool.read_only else ""
            )
            lines.append(f"- {tool.name}{write_note}: {tool.summary}")
        lines.append("")
    lines.append(
        "Client tools take a NAME or DOMAIN, not an internal id — call "
        "list_clients first if you don't know the exact spelling. "
        "Long-running tools return a job id; poll it with get_job_status."
    )
    return "\n".join(lines).strip()


_STARTER_PROMPTS: list[McpStarterPrompt] = [
    McpStarterPrompt(
        text="What changed on Alpha Corp's site this week?",
        scenario="Site monitor",
    ),
    McpStarterPrompt(
        text="What happened to the /pricing page over the last month — show me the diff.",
        scenario="Web archive",
    ),
    McpStarterPrompt(
        text="Give me Alpha Corp's numbers for August, and tell me what's actually up or down.",
        scenario="Reports",
    ),
    McpStarterPrompt(
        text="List every client and flag anyone whose health looks off right now.",
        scenario="Orientation",
    ),
]


def _connect_snippets(base_url: str) -> list[McpConnectSnippet]:
    stdio_config = (
        "{\n"
        '  "mcpServers": {\n'
        '    "yovth-clients-report": {\n'
        '      "command": "docker",\n'
        '      "args": ["compose", "exec", "-T", "api", "poetry", "run",\n'
        '                "python", "-m", "app.mcp.stdio"],\n'
        '      "env": { "MCP_API_BASE_URL": "' + base_url + '", "MCP_TOKEN": "<your-token>" }\n'
        "    }\n"
        "  }\n"
        "}"
    )
    return [
        McpConnectSnippet(
            host="Claude Desktop",
            title="claude_desktop_config.json",
            config=stdio_config,
            note="Ask an admin for a token, then restart Claude Desktop to pick up the change.",
        ),
        McpConnectSnippet(
            host="Claude Code",
            title=".mcp.json",
            config=stdio_config,
            note="Drop this in the project root, or run `claude mcp add` "
            "and paste the same command.",
        ),
        McpConnectSnippet(
            host="ChatGPT",
            title="Not available yet",
            config="# Streamable HTTP transport ships in task-74 wave 2.",
            note="ChatGPT needs an HTTP endpoint, not stdio — this arrives "
            "with the agent_tokens follow-up.",
        ),
    ]


@router.get("/catalog", response_model=McpCatalogDTO)
async def get_mcp_catalog() -> McpCatalogDTO:
    settings = get_settings()
    tools = await _build_tools()
    prompts = await _build_prompts()
    resources = await _build_resources()
    return McpCatalogDTO(
        enabled=True,
        server_name=mcp.name,
        server_version=mcp.version or "0.1.0",
        api_base_url=settings.mcp_api_base_url,
        groups=_GROUP_ORDER,
        tools=tools,
        all_tools_prompt=_build_all_tools_prompt(tools, _GROUP_ORDER),
        prompts=prompts,
        resources=resources,
        starter_prompts=_STARTER_PROMPTS,
        connect=_connect_snippets(settings.mcp_api_base_url),
    )


register_router(router)

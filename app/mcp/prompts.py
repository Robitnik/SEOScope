"""Prompts (план §8): те, що обирає ЛЮДИНА (кнопкою/слеш-командою в хості),
а не модель. Без них сервер лишається «можна питати» — саме prompts
дають відділам відчуття «є готовий маршрут» для двох питань, які
звучать найчастіше (план §0).

Кожен prompt — це параметризований рецепт: текст-інструкція, яка каже
моделі, які tools викликати і в якому порядку. Сама робота лишається за
моделлю через звичайні виклики tools цього ж сервера.
"""

from __future__ import annotations

from app.mcp.server import mcp


@mcp.prompt(
    name="page_history_answer",
    title="What happened to this page?",
    description=(
        "Answer 'what changed on this page and when' using the web archive: "
        "timeline, diff, and a plain-language summary."
    ),
)
def page_history_answer(url: str, client: str | None = None) -> str:
    """Build the instruction for answering a page-history question.

    Args:
        url: The page URL (or a distinctive fragment of it) to investigate.
        client: Optionally narrow the search to one client (name or
            domain), if the URL alone is ambiguous.
    """
    scope = f' for client "{client}"' if client else ""
    return (
        f"Find out what happened to the page at or matching {url!r}{scope}, using the "
        "web archive tools on this server:\n"
        f"1. Call find_archived_url(query={url!r}"
        f"{f', client={client!r}' if client else ''}) to get its url_id. If there are "
        "multiple matches, pick the one whose URL is the closest match, or ask which "
        "one was meant.\n"
        "2. Call get_url_snapshot_timeline(url_id) to see every archived date and "
        "whether each one changed from the previous.\n"
        "3. For the most recent change (or a specific date the user asked about), call "
        "compare_url_versions(url_id, target_date=...) to get the actual diff.\n"
        "4. Answer in plain language: WHEN the page last changed, WHAT changed "
        "(summarize the diff, don't just paste it), and whether earlier changes in the "
        "timeline look relevant too. If the URL was never archived, say so plainly "
        "instead of guessing."
    )


@mcp.prompt(
    name="weekly_change_digest",
    title="Weekly change digest",
    description=(
        "Summarize what changed for a client over the last week: site monitor "
        "changes and web archive changes, ranked by importance."
    ),
)
def weekly_change_digest(client: str) -> str:
    """Build the instruction for a client's weekly change digest.

    Args:
        client: The client's name or domain.
    """
    return (
        f'Build a weekly change digest for client "{client}", using the tools on this '
        "server:\n"
        f"1. Call list_site_changes(client={client!r}, days=7) for monitor-detected "
        "changes (page content, robots.txt, sitemap, DNS).\n"
        f"2. Call list_archive_changes(client={client!r}, days=7) for archived page "
        "changes the monitor doesn't cover.\n"
        "3. Merge both lists and rank by severity first (critical, then warning, then "
        "info for the monitor changes), then by how much text changed for the archive "
        "changes.\n"
        "4. Write a short digest: lead with anything critical, then a few bullets for "
        "the rest, grouped by page/subject rather than listed chronologically. If "
        "nothing changed in either list, say so — an empty week is a valid, useful "
        "answer, not a failure."
    )

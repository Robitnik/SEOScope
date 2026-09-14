"""Звірка вихідних посилань: Framework → Copy → Site (task-95).

Заміна вердикту task-91 §2.3 (`matrix.py`'s колишній `_link_verdict`), який
порівнював приписані посилання з УСІМА `<a href>` живої сторінки — 94-97%
з них навігаційний каркас (task-95 §1.1/§1.2), тож вердикт `match` світився
навіть коли жодне приписане посилання в копію не потрапило (`matrix.py`'s
докстрінг це вже не описує — колишня поведінка видалена, не документується
задвіч). Копія теж раніше не входила в звірку взагалі (`readers/gdoc.py`
читав лише `textRun.content`, викидаючи `textStyle.link.url`).

Це ЛАНЦЮГ, не трикутник: `Framework` каже, які посилання МАЮТЬ бути в копії
(«in copy»); `Copy` (все, що вона реально несе — і приписане, і додаткове)
каже, що МАЄ бути на сайті («on site»). `Strategy` у цю звірку не входить
(task-91 §1.2: `links` там `absent`/`empty` на всьому живому портфелі;
follow-up — task-95 §6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin

from app.contentqa.model import ContentItem
from app.contentqa.readers.gdoc import CopyLink
from app.contentqa.site import LivePage
from app.contentqa.verdict import FieldVerdict
from app.urlinspector.urlkey import inspector_key

# ── видобуття цілі з рядка Framework-посилання (task-91 §1.4a + task-95 §1.6) ──
# П'ять живих форм, по черзі, перша знахідка перемагає; `None`, коли жодна
# не спрацювала (не здогадка) — типово для рядків-прози без URL узагалі
# ("order online", "Already on the page, staying as they are:").
_ABS_URL_RE = re.compile(r"https?://\S+")
_PAREN_PATH_RE = re.compile(r"\((/[^\s)]*)\)")
_ARROW_PATH_RE = re.compile(r"→\s*(/\S*)")
# task-95 §1.6: заміряно на живому корпусі — SDS-шаблон `"emergency dental
# care - /services/emergency-dental-care/"`, 5.3% усіх Framework-рядків
# портфеля, чинний екстрактор (до task-95) губив цю форму цілком.
_DASH_PATH_RE = re.compile(r"[—–-]\s+(/\S+)")
_TRAILING_PUNCT = ").,;:'\""


def _extract_link_target(raw: str, *, base_url: str | None) -> str | None:
    match = _ABS_URL_RE.search(raw)
    if match:
        return match.group(0).rstrip(_TRAILING_PUNCT)
    match = _PAREN_PATH_RE.search(raw)
    if match:
        path = match.group(1)
        return urljoin(base_url, path) if base_url else path
    match = _ARROW_PATH_RE.search(raw)
    if match:
        path = match.group(1).rstrip(_TRAILING_PUNCT)
        return urljoin(base_url, path) if base_url else path
    match = _DASH_PATH_RE.search(raw)
    if match:
        path = match.group(1).rstrip(_TRAILING_PUNCT)
        return urljoin(base_url, path) if base_url else path
    return None


def _match_key(url: str) -> str:
    """Ключ для звірки трьох джерел (task-95 §1.8): `inspector_key` без
    query, казефолджений ЛОКАЛЬНО тут — `inspector_key` сам регістр шляху
    навмисно не чіпає (чужий модуль, на ньому стоять GSC↔GA4-джойни, task-
    61), а копірайтер цілком міг написати `/Locations/` там, де сайт
    насправді віддає `/locations/`."""
    return inspector_key(url).partition("?")[0].casefold()


LinkPresence = Literal["yes", "no", "not_checked"]
LinkOrigin = Literal["framework", "copy"]
LinkAuditState = Literal["checked", "not_applicable"]


@dataclass(frozen=True)
class LinkRow:
    """Одне порівнюване посилання (task-95 §2.5). `origin` — хто перший
    заявив цю ціль: `"framework"`, якщо вона є серед приписаних Framework-
    посилань (навіть коли копія її теж має), інакше `"copy"` — посилання,
    якого Framework не приписував, але копія несе (§1.7: саме так
    знайдено застарілий PDF-сертифікат на EFFYS). `in_copy`/`on_site` —
    третій стан `"not_checked"`, не мовчазне `"no"`, коли крок фізично не
    міг відбутися (нема копі-вкладки / сайт недоступний, PLAN §12)."""

    target: str
    display: str
    origin: LinkOrigin
    in_copy: LinkPresence
    on_site: LinkPresence
    note: str | None = None


@dataclass(frozen=True)
class LinkAudit:
    """Результат звірки одного фреймворка (task-95 §2.5). `state=
    "not_applicable"` — і Framework, і Copy чесно не заявляють жодної
    цілі (не помилка інструменту — щось із 13/56 живих копі-вкладок
    портфеля справді не має жодного гіперпосилання, task-95 §1.5).
    `skipped` — рядки без видобутої цілі (Framework-проза без URL) і
    рядки-інструкції верстальнику (`[MAP: …]`) з Copy — показати
    оператору, не рахувати у вердикт."""

    state: LinkAuditState
    reason: str | None
    in_copy: FieldVerdict
    on_site: FieldVerdict
    rows: tuple[LinkRow, ...] = ()
    skipped: tuple[str, ...] = ()


_NOT_APPLICABLE_REASON = "No links to compare: the framework declares none and the copy has none."


def _copy_verdict(
    framework_targets: dict[str, str], copy_target_keys: frozenset[str], *, has_copy_tab: bool
) -> FieldVerdict:
    if not framework_targets:
        return FieldVerdict("not_comparable")
    if not has_copy_tab:
        return FieldVerdict("not_comparable", detail="no copy tab in this document")
    found = sum(1 for key in framework_targets if key in copy_target_keys)
    total = len(framework_targets)
    detail = f"{found}/{total} framework link(s) in copy"
    return FieldVerdict("match" if found == total else "mismatch", detail=detail)


def _site_verdict(
    rows: list[LinkRow], *, site_keys: frozenset[str] | None, reason: str | None
) -> FieldVerdict:
    if not rows:
        return FieldVerdict("not_comparable")
    if site_keys is None:
        return FieldVerdict("not_comparable", detail=reason)
    found = sum(1 for row in rows if row.on_site == "yes")
    total = len(rows)
    detail = f"{found}/{total} copy link(s) on the live page"
    return FieldVerdict("match" if found == total else "mismatch", detail=detail)


def audit_links(
    framework_item: ContentItem | None,
    *,
    copy_links: tuple[CopyLink, ...],
    has_copy_tab: bool,
    copy_genre: Literal["copy", "edit_brief"] | None,
    site_page: LivePage | None,
    has_url: bool,
) -> LinkAudit:
    """Чиста функція — жодного HTTP/БД, той самий принцип, що `build_matrix`
    (task-91). `copy_links`/`has_copy_tab`/`copy_genre` — уже вирішені
    викликачем (`package.py`, §2.3): яка копі-вкладка є "основною" для цієї
    сторінки (роль з task-92 §2.5, покриття проти сайту, не назва) — ця
    функція його не вгадує вдруге."""
    base_url = framework_item.url if framework_item else None
    raw_framework_links = (
        (framework_item.internal_links + framework_item.external_links)
        if framework_item else ()
    )

    framework_targets: dict[str, str] = {}
    skipped: list[str] = []
    for raw in raw_framework_links:
        target = _extract_link_target(raw, base_url=base_url)
        if target is None:
            skipped.append(raw)
            continue
        framework_targets.setdefault(_match_key(target), raw)

    copy_targets: dict[str, tuple[str, str]] = {}  # key -> (anchor, url)
    for link in copy_links:
        if link.in_marker:
            skipped.append(f"{link.anchor or link.url} (instruction, skipped)")
            continue
        copy_targets.setdefault(_match_key(link.url), (link.anchor, link.url))

    if not framework_targets and not copy_targets:
        return LinkAudit(
            state="not_applicable", reason=_NOT_APPLICABLE_REASON,
            in_copy=FieldVerdict("not_comparable"), on_site=FieldVerdict("not_comparable"),
            skipped=tuple(skipped),
        )

    site_keys: frozenset[str] | None = None
    on_site_reason: str | None = None
    if site_page is not None and site_page.ok:
        site_keys = frozenset(
            _match_key(link.url) for link in site_page.internal_links + site_page.external_links
        )
    elif site_page is not None:
        on_site_reason = site_page.error or "unreachable"
    elif not has_url:
        on_site_reason = "no URL"
    else:
        on_site_reason = "site not checked"

    def on_site_presence(key: str) -> LinkPresence:
        if site_keys is None:
            return "not_checked"
        return "yes" if key in site_keys else "no"

    note = (
        "edit brief — links may already live on the existing page"
        if copy_genre == "edit_brief" else None
    )

    rows: list[LinkRow] = []
    seen: set[str] = set()
    for key, raw in framework_targets.items():
        rows.append(LinkRow(
            target=key, display=raw, origin="framework",
            in_copy="yes" if key in copy_targets else "no",
            on_site=on_site_presence(key), note=note,
        ))
        seen.add(key)
    for key, (anchor, url) in copy_targets.items():
        if key in seen:
            continue
        rows.append(LinkRow(
            target=key, display=f"{anchor} → {url}" if anchor else url, origin="copy",
            in_copy="yes", on_site=on_site_presence(key), note=note,
        ))

    return LinkAudit(
        state="checked", reason=None,
        in_copy=_copy_verdict(
            framework_targets, frozenset(copy_targets), has_copy_tab=has_copy_tab
        ),
        on_site=_site_verdict(rows, site_keys=site_keys, reason=on_site_reason),
        rows=tuple(rows), skipped=tuple(skipped),
    )


__all__ = ["LinkAudit", "LinkAuditState", "LinkOrigin", "LinkPresence", "LinkRow", "audit_links"]

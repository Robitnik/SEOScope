#!/usr/bin/env python
"""Разове прибирання вже накопиченого сміттєвого фронтиру Web Archive
(task-82 §4) — URL, які ніколи не мали потрапляти в архів:

  1. action-лінки (`?add-to-cart=…`, `?wc-ajax=…`, …) — кнопка, не сторінка
     (те саме рішення, що `app.webarchive.urls.frontier_decision`, task-82
     §2, застосовує тепер до НОВИХ відкриттів на вході);
  2. адреси з буквальним HTML-entity (`&amp;`/`&#038;`) у query — сайт
     екранує `&` у власній розмітці, і без розекранування ми запитуємо
     параметр із буквальним іменем `amp;add-to-cart` та архівуємо знімок
     під адресою, якої на сайті ніколи не було (task-82 §0.2).

Цей скрипт лише прибирає те, що вже лежить у базі — task-82 §1/§2 самі
зупиняють приріст на вході, наступний обхід новий мотлох уже не створить.

Ніколи не чіпає `source="sitemap"` рядки, навіть якщо форма URL збігається
з критерієм сміття: клієнт поклав цю адресу в сайтмап сам, і другий раз
вирішувати за нього — не наша справа (та сама логіка, що в
`frontier_decision`'s docstring).

Видалення йде через `archive_urls` — каскад `ondelete="CASCADE"` знімає
снапшоти й `archive_changes` разом з рядком; самі блоби лишаються на
місці (контентно-адресовані, штатний sweeper прибере їх, коли на них не
лишиться жодного посилання).

    docker compose exec api poetry run python scripts/archive_prune_junk_urls.py \\
        --client LAXTON --dry-run
    docker compose exec api poetry run python scripts/archive_prune_junk_urls.py \\
        --client LAXTON --i-have-a-backup

Перед реальним запуском — бекап (CLAUDE.md, розділ «Перед будь-якою дією,
що торкається бойової БД»):

    docker compose exec api poetry run python scripts/backup_sqlite.py \\
        --out-dir /app/app_data/backups --keep 14
"""

from __future__ import annotations

import argparse
import sys


def _is_junk(url: str) -> bool:
    """task-82 §0.1/§0.2 — the same two, independently-measured defect
    classes. Kept as ONE small function (not re-deriving `frontier_decision`
    logic here) so the running policy and this cleanup can never drift apart.
    """
    from app.webarchive.urls import frontier_decision

    if frontier_decision(url) is None:
        return True
    # An entity-escaped query is junk on its own terms, independent of
    # `frontier_decision`'s verdict — `parse_qsl` splits only on a literal
    # `&`, so `?orderby=price&amp;add-to-cart=1` is seen as ONE param named
    # literally `amp;add-to-cart`, not the recognizable `add-to-cart` — the
    # very reason a separate check is needed here at all.
    return "&amp;" in url or "&#038;" in url


def main(argv: list[str] | None = None) -> int:
    from sqlalchemy import func, select

    from app.db.models import ArchiveSnapshot
    from app.db.repositories.clients import ClientRepository
    from app.db.repositories.web_archive import WebArchiveRepository
    from app.db.session import worker_session_scope

    parser = argparse.ArgumentParser(prog="archive_prune_junk_urls")
    parser.add_argument(
        "--client", required=True, help="назва клієнта (Client.name), напр. LAXTON"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="лише порахувати й показати список, нічого не видаляти (за замовчуванням)",
    )
    parser.add_argument(
        "--i-have-a-backup", action="store_true",
        help="підтвердження, що бекап зроблено — обов'язкове для реального видалення",
    )
    args = parser.parse_args(argv)

    real_run = args.i_have_a_backup and not args.dry_run
    if not args.dry_run and not args.i_have_a_backup:
        print(
            "відмова: реальне видалення вимагає --i-have-a-backup "
            "(CLAUDE.md, «Перед будь-якою дією, що торкається бойової БД»). "
            "Продовжую як --dry-run.",
            file=sys.stderr,
        )

    with worker_session_scope() as session:
        clients_by_name = {c.name: c for c in ClientRepository(session).list_all()}
        client = clients_by_name.get(args.client)
        if client is None:
            print(f"відмова: клієнта {args.client!r} не знайдено", file=sys.stderr)
            return 2

        repo = WebArchiveRepository(session)
        urls = repo.list_urls_for_client(client.id)
        # `source="sitemap"` is NEVER a deletion candidate, even if its
        # shape happens to match `_is_junk` — see module docstring.
        junk = [u for u in urls if u.source != "sitemap" and _is_junk(u.url)]

        snapshot_count = 0
        if junk:
            junk_ids = [u.id for u in junk]
            snapshot_count = (
                session.scalar(
                    select(func.count())
                    .select_from(ArchiveSnapshot)
                    .where(ArchiveSnapshot.archive_url_id.in_(junk_ids))
                )
                or 0
            )

        print(
            f"{args.client}: {len(urls)} URL відстежено, "
            f"{len(junk)} сміттєвих (буде видалено), "
            f"{snapshot_count} їхніх знімків піде разом з ними"
        )
        for archive_url in junk[:20]:
            print(f"  {archive_url.url}")
        if len(junk) > 20:
            print(f"  … ще {len(junk) - 20}")

        if not real_run:
            print("dry-run: нічого не видалено")
            return 0

        for archive_url in junk:
            repo.delete_url(archive_url)

    print(f"видалено {len(junk)} URL і {snapshot_count} знімків клієнта {args.client}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

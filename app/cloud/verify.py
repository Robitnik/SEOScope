"""`cloud-verify` (task-52 §3.9): pull a random sample of already-`uploaded`
ledger rows back from B2 and compare sha256 against the locally recorded
value. A backup nobody has ever read back is not a verified backup.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cloud.client import CloudStore
from app.db.models import CloudObject


@dataclass
class VerifyOutcome:
    kind: str
    source_ref: str
    key: str
    ok: bool
    detail: str


def verify_sample(
    session: Session, store: CloudStore, *, kind: str | None, sample: int
) -> list[VerifyOutcome]:
    stmt = select(CloudObject).where(CloudObject.status == "uploaded")
    if kind is not None:
        stmt = stmt.where(CloudObject.kind == kind)
    rows = list(session.scalars(stmt))
    random.shuffle(rows)

    outcomes: list[VerifyOutcome] = []
    for row in rows[:sample]:
        try:
            data = store.get_bytes(row.key)
        except Exception as exc:  # noqa: BLE001 - any download failure is a verify failure, not a crash
            detail = f"download_failed:{type(exc).__name__}"
            outcomes.append(VerifyOutcome(row.kind, row.source_ref, row.key, False, detail))
            continue
        actual = hashlib.sha256(data).hexdigest()
        ok = row.sha256 is None or actual == row.sha256
        outcomes.append(
            VerifyOutcome(row.kind, row.source_ref, row.key, ok, "ok" if ok else "sha256_mismatch")
        )
    return outcomes

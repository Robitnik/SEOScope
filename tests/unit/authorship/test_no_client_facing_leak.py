"""Task-93 plan §5 DoD point 9: no client-facing artifact ever carries an
agency employee's name.

`created_by_user_id`/`created_by_kind` are new columns on `ReportRun`/
`ReportBatch`/`ContentQaPackage` — every document generator that turns one
of those rows into something a client receives (the client PDF, the
portfolio PDF/XLSX, the matrix XLSX, the report document HTML) was written
long before task-93 and has no reason to ever read them. This is a static
guard, not a rendered-output diff: it fails loudly the moment someone adds
a `created_by_*` reference to one of these files, rather than relying on
every future document-format change to remember the invariant by hand.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Every module that renders something handed to (or downloaded by) a
# client, across both document families this app produces.
_CLIENT_FACING_MODULES = (
    "app/reports/client_pdf.py",
    "app/reports/batch_pdf.py",
    "app/reports/batch_xlsx.py",
    "app/reports/document.py",
    "app/reports/monthly_xlsx.py",
    "app/contentqa/matrix_xlsx.py",
)


def test_no_client_facing_module_reads_the_author_columns() -> None:
    offenders = []
    for relative_path in _CLIENT_FACING_MODULES:
        path = _REPO_ROOT / relative_path
        assert path.is_file(), f"expected client-facing module missing: {relative_path}"
        if "created_by" in path.read_text():
            offenders.append(relative_path)
    assert offenders == [], (
        f"{offenders} reference(s) created_by_user_id/created_by_kind — task-93's "
        "author columns must never reach a client-facing document"
    )

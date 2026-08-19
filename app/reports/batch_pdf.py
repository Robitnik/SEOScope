"""Merge a batch's already-rendered client PDFs into one portfolio document
(task-50).

Reuses `pdfunite` (poppler-utils — the same package that already gives
`client_pdf.py::_pdf_page_count` its `pdfinfo` call, baked into the image
since task-42 §1.5) instead of a new Python PDF library: every page here was
already produced by `runner.py`'s own `_write_client_pdf` during each child
run, so this is a pure concatenation, not a render pass.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class NoClientPdfsError(RuntimeError):
    """None of the batch's children have a rendered client PDF yet."""


def build_portfolio_pdf(pdf_paths: list[Path], out_path: Path) -> Path:
    """`pdf_paths` in the order pages should appear — the caller decides
    that ordering (alphabetical by client name); this just concatenates.
    """
    if not pdf_paths:
        raise NoClientPdfsError("No client PDFs available to merge")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["pdfunite", *(str(p) for p in pdf_paths), str(out_path)],
        check=True, capture_output=True, timeout=60,
    )
    return out_path

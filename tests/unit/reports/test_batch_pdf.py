from __future__ import annotations

import pytest

from app.reports import batch_pdf
from app.reports.batch_pdf import NoClientPdfsError, build_portfolio_pdf


def test_raises_when_no_pdfs_are_available(tmp_path):
    with pytest.raises(NoClientPdfsError):
        build_portfolio_pdf([], tmp_path / "portfolio.pdf")


def test_calls_pdfunite_with_every_path_in_order(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        batch_pdf.subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs))
    )

    pdfs = [tmp_path / "swm.pdf", tmp_path / "grp.pdf"]
    out = tmp_path / "out" / "portfolio.pdf"
    result = build_portfolio_pdf(pdfs, out)

    assert result == out
    assert out.parent.is_dir()  # parent directory created before pdfunite runs
    [(args, kwargs)] = calls
    assert args == ["pdfunite", str(pdfs[0]), str(pdfs[1]), str(out)]
    assert kwargs["check"] is True

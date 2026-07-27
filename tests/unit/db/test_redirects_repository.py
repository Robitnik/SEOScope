"""Тести persistence-шару інструмента Redirect Checker (task-29)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import Client, RedirectCheck, RedirectCheckTarget
from app.db.repositories.redirects import (
    RedirectRepository,
    TargetFilters,
    TargetInput,
    TargetResultInput,
)


def _targets(count: int) -> list[TargetInput]:
    return [
        TargetInput(
            row_index=index,
            input_url=f"a.example/{index}",
            normalized_url=f"https://a.example/{index}",
            notes=("scheme_added",),
        )
        for index in range(1, count + 1)
    ]


def _create(session: Session, *, client_id: str | None = None, count: int = 3) -> RedirectCheck:
    return RedirectRepository(session).create_check(
        client_id=client_id,
        title="Migration spot-check",
        input_count=count + 2,
        options={"max_hops": 20, "follow_meta_refresh": True, "cache_buster": False},
        rejected=[{"input_url": "mailto:hi@a.example", "error_code": "unsupported_scheme",
                   "message": "mailto: links cannot be traced."}],
        targets=_targets(count),
    )


def _result(**overrides: object) -> TargetResultInput:
    payload: dict[str, object] = {
        "verdict": "ok_redirect",
        "final_url": "https://a.example/new",
        "final_status_code": 200,
        "hop_count": 1,
        "chain": [
            {"index": 1, "url": "https://a.example/1", "status": 301, "location": "https://a.example/new"},
            {"index": 2, "url": "https://a.example/new", "status": 200, "location": None},
        ],
        "flags": ["temporary_redirect"],
        "final_page": {"title": "New", "canonical": "https://a.example/new"},
        "error_code": None,
        "error_detail": None,
        "elapsed_ms": 412,
    }
    payload.update(overrides)
    return TargetResultInput(**payload)  # type: ignore[arg-type]


def test_create_check_persists_targets_queued(session: Session) -> None:
    check = _create(session)
    session.commit()

    assert check.target_count == 3
    assert check.input_count == 5
    assert check.rejected_json[0]["error_code"] == "unsupported_scheme"
    targets = RedirectRepository(session).all_targets(check.id)
    assert [target.row_index for target in targets] == [1, 2, 3]
    assert {target.run_status for target in targets} == {"queued"}
    # «Не перевіряли» — це `None`, а не «редіректів немає» і не нуль.
    assert targets[0].checked_at is None
    assert targets[0].verdict is None
    assert targets[0].hop_count is None
    assert targets[0].input_notes_json == ["scheme_added"]


def test_row_index_is_unique_per_check(session: Session) -> None:
    check = _create(session, count=1)
    session.commit()
    session.add(
        RedirectCheckTarget(
            check_id=check.id, row_index=1, input_url="x", normalized_url="https://x.example/"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_client_can_be_attached(session: Session) -> None:
    client = Client(name="Acme", slug="acme", domain="acme.test")
    session.add(client)
    session.flush()
    check = _create(session, client_id=client.id)
    session.commit()
    assert check.client_id == client.id


def test_claim_target_is_atomic(session: Session) -> None:
    check = _create(session, count=1)
    session.commit()
    repository = RedirectRepository(session)
    target = repository.all_targets(check.id)[0]

    claimed = repository.claim_target(check.id, target.id)
    assert claimed is not None
    assert claimed.run_status == "running"
    # Другий клік по тому ж рядку не має дати другий запит до чужого сайту.
    assert repository.claim_target(check.id, target.id) is None


def test_claim_allows_re_running_a_finished_target(session: Session) -> None:
    check = _create(session, count=1)
    repository = RedirectRepository(session)
    target = repository.all_targets(check.id)[0]
    repository.claim_target(check.id, target.id)
    repository.save_result(target, _result())
    session.commit()

    again = repository.claim_target(check.id, target.id)
    assert again is not None
    assert again.run_status == "running"


def test_save_result_stores_the_whole_chain(session: Session) -> None:
    check = _create(session, count=1)
    repository = RedirectRepository(session)
    target = repository.all_targets(check.id)[0]
    repository.claim_target(check.id, target.id)
    saved = repository.save_result(target, _result())
    session.commit()

    assert saved.run_status == "done"
    assert saved.verdict == "ok_redirect"
    assert saved.hop_count == 1
    assert len(saved.chain_json) == 2
    assert saved.chain_json[0]["status"] == 301
    assert saved.flags_json == ["temporary_redirect"]
    assert saved.final_page_json is not None
    assert saved.checked_at is not None
    assert saved.elapsed_ms == 412


def test_save_result_can_be_repeated_and_overwrites(session: Session) -> None:
    check = _create(session, count=1)
    repository = RedirectRepository(session)
    target = repository.all_targets(check.id)[0]
    repository.save_result(target, _result())
    repository.save_result(
        target,
        _result(verdict="broken", final_status_code=404, flags=[], final_page=None, hop_count=0),
    )
    session.commit()

    assert target.verdict == "broken"
    assert target.final_status_code == 404
    assert target.flags_json == []
    assert target.final_page_json is None


def test_release_target_marks_failure_without_a_verdict(session: Session) -> None:
    check = _create(session, count=1)
    repository = RedirectRepository(session)
    target = repository.all_targets(check.id)[0]
    repository.release_target(target, error_detail="worker crashed")
    session.commit()

    assert target.run_status == "failed"
    assert target.error_code == "run_failed"
    assert target.verdict is None


def test_save_result_truncates_long_error_detail(session: Session) -> None:
    check = _create(session, count=1)
    repository = RedirectRepository(session)
    target = repository.all_targets(check.id)[0]
    repository.save_result(target, _result(verdict="fetch_failed", error_code="timeout",
                                           error_detail="x" * 5000))
    session.commit()
    assert target.error_detail is not None
    assert len(target.error_detail) == 1000


def test_counts_cover_progress_and_verdicts(session: Session) -> None:
    check = _create(session, count=3)
    repository = RedirectRepository(session)
    targets = repository.all_targets(check.id)
    repository.save_result(targets[0], _result())
    repository.save_result(targets[1], _result(verdict="broken", final_status_code=404))
    session.commit()

    assert repository.run_status_counts(check.id) == {
        "queued": 1,
        "running": 0,
        "done": 2,
        "failed": 0,
    }
    assert repository.verdict_counts(check.id) == {"ok_redirect": 1, "broken": 1}


def test_updating_a_target_touches_the_check(session: Session) -> None:
    check = _create(session, count=1)
    session.commit()
    before = check.updated_at
    repository = RedirectRepository(session)
    repository.save_result(repository.all_targets(check.id)[0], _result())
    session.commit()
    assert check.updated_at >= before


class TestFilters:
    @pytest.fixture()
    def prepared(self, session: Session) -> RedirectCheck:
        check = _create(session, count=3)
        repository = RedirectRepository(session)
        targets = repository.all_targets(check.id)
        repository.save_result(
            targets[0], _result(final_url="https://a.example/alpha", flags=["host_change"])
        )
        repository.save_result(
            targets[1],
            _result(verdict="broken", final_status_code=404, final_url="https://a.example/beta",
                    flags=["temporary_redirect", "protocol_downgrade"]),
        )
        session.commit()
        return check

    def test_filter_by_verdict(self, session: Session, prepared: RedirectCheck) -> None:
        rows, total = RedirectRepository(session).list_targets(
            prepared.id, filters=TargetFilters(verdict="broken"), limit=50, offset=0
        )
        assert total == 1
        assert rows[0].verdict == "broken"

    def test_filter_by_run_status(self, session: Session, prepared: RedirectCheck) -> None:
        _rows, total = RedirectRepository(session).list_targets(
            prepared.id, filters=TargetFilters(run_status="queued"), limit=50, offset=0
        )
        assert total == 1

    def test_filter_by_flag(self, session: Session, prepared: RedirectCheck) -> None:
        rows, total = RedirectRepository(session).list_targets(
            prepared.id, filters=TargetFilters(flag="protocol_downgrade"), limit=50, offset=0
        )
        assert total == 1
        assert "protocol_downgrade" in rows[0].flags_json

    def test_flag_filter_does_not_match_a_similar_name(
        self, session: Session, prepared: RedirectCheck
    ) -> None:
        _rows, total = RedirectRepository(session).list_targets(
            prepared.id, filters=TargetFilters(flag="host"), limit=50, offset=0
        )
        assert total == 0

    def test_search_matches_final_url(self, session: Session, prepared: RedirectCheck) -> None:
        rows, total = RedirectRepository(session).list_targets(
            prepared.id, filters=TargetFilters(query="BETA"), limit=50, offset=0
        )
        assert total == 1
        assert rows[0].final_url == "https://a.example/beta"

    def test_search_matches_input_url(self, session: Session, prepared: RedirectCheck) -> None:
        _rows, total = RedirectRepository(session).list_targets(
            prepared.id, filters=TargetFilters(query="a.example/3"), limit=50, offset=0
        )
        assert total == 1

    def test_pagination_keeps_source_order(self, session: Session, prepared: RedirectCheck) -> None:
        rows, total = RedirectRepository(session).list_targets(
            prepared.id, filters=TargetFilters(), limit=2, offset=1
        )
        assert total == 3
        assert [row.row_index for row in rows] == [2, 3]


def test_get_target_rejects_a_foreign_id(session: Session) -> None:
    first = _create(session, count=1)
    second = _create(session, count=1)
    session.commit()
    repository = RedirectRepository(session)
    foreign = repository.all_targets(second.id)[0]
    assert repository.get_target(first.id, foreign.id) is None


def test_delete_check_cascades(session: Session) -> None:
    check = _create(session, count=3)
    session.commit()
    repository = RedirectRepository(session)

    assert repository.delete_check(check.id) is True
    session.commit()
    assert repository.get_check(check.id) is None
    assert session.query(RedirectCheckTarget).count() == 0
    assert repository.delete_check(check.id) is False


def test_list_checks_is_newest_first(session: Session) -> None:
    older = _create(session, count=1)
    newer = _create(session, count=1)
    newer.created_at = newer.created_at.replace(year=newer.created_at.year + 1)
    session.commit()

    checks, total = RedirectRepository(session).list_checks(limit=10, offset=0)
    assert total == 2
    assert [check.id for check in checks] == [newer.id, older.id]

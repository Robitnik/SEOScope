"""Юніти фазової телеметрії прогресу (task-32).

Тут перевіряється не «чи емітяться події», а три властивості, порушення яких
безпосередньо повертає дефект, заради якого писався `PhaseTracker`:

1. фази однієї клітинки не перекриваються і покривають 0..100 — інакше смуга
   стрибає назад або доходить до кінця раніше за роботу;
2. відсоток монотонний — зміна стадії не смикає смугу;
3. крок, тривалість якого невідома, НЕ отримує вигаданого відсотка
   (frontend/AGENTS.md §9).
"""

from __future__ import annotations

import pytest

from app.integrations.progress import (
    CELL_PHASE_WEIGHTS,
    DEFAULT_PHASE_WEIGHTS,
    PhaseTracker,
    phase_bounds,
)
from app.schemas.enums import Provider


@pytest.mark.parametrize(
    "weights",
    [*CELL_PHASE_WEIGHTS.values(), DEFAULT_PHASE_WEIGHTS],
    ids=[*(provider.value for provider in CELL_PHASE_WEIGHTS), "default"],
)
def test_every_phase_layout_covers_the_full_scale_exactly_once(weights):
    assert sum(weights.values()) == pytest.approx(100.0)

    covered = []
    for phase in weights:
        base, span = phase_bounds(weights, phase)
        covered.append((base, base + span))

    # Суміжні, без розривів і без перекриття: кінець однієї фази = початок наступної.
    assert covered[0][0] == pytest.approx(0.0)
    assert covered[-1][1] == pytest.approx(100.0)
    for (_, end), (next_start, _) in zip(covered, covered[1:], strict=False):
        assert end == pytest.approx(next_start)


def test_unknown_phase_falls_back_to_the_whole_scale_instead_of_failing():
    # Прогрес — діагностика: незнайома фаза не має ані падати, ані «з'їдати» шкалу.
    assert phase_bounds({"kpi": 100.0}, "does_not_exist") == (0.0, 100.0)


def test_steps_are_measured_inside_the_phase_and_never_leave_it():
    events = []
    tracker = PhaseTracker(
        Provider.reporting_ninja, events.append, "datasets", throttle_seconds=0
    )
    for step in range(1, 5):
        tracker.step("fetch_current_ga4_daily", completed=step, total=4)

    base, span = phase_bounds(CELL_PHASE_WEIGHTS[Provider.reporting_ninja], "datasets")
    percents = [event.percent for event in events]
    assert percents[0] == pytest.approx(base + span / 4)
    assert percents[-1] == pytest.approx(base + span)
    assert all(base <= value <= base + span for value in percents)


def test_percent_never_goes_backwards_when_a_later_stage_reports_fewer_steps():
    # Реальний випадок: фаза складається з кількох циклів із різними `total`.
    events = []
    tracker = PhaseTracker(Provider.ahrefs, events.append, "datasets", throttle_seconds=0)
    tracker.step("parse_affected", completed=9, total=10)
    tracker.step("parse_issues", completed=1, total=10)

    percents = [event.percent for event in events]
    assert percents == sorted(percents)


def test_indeterminate_step_carries_no_percent_and_is_marked_for_the_ui():
    events = []
    tracker = PhaseTracker(Provider.ahrefs, events.append, "datasets", throttle_seconds=0)
    tracker.indeterminate("export_issues", message="Ahrefs is building the export")

    assert len(events) == 1
    assert events[0].percent is None  # жодного вигаданого числа
    assert events[0].details["indeterminate"] is True
    assert events[0].details["phase"] == "datasets"


def test_throttle_collapses_repeats_but_never_drops_the_final_step():
    clock = iter([0.0] * 40)
    events = []
    tracker = PhaseTracker(
        Provider.gsc,
        events.append,
        "datasets",
        throttle_seconds=10.0,
        clock=lambda: next(clock),
    )
    for step in range(1, 6):
        tracker.step("fetch_current_daily", completed=step, total=5)

    stages = [event.completed for event in events]
    # Перший крок (зміна стадії) і останній (кінець фази) проходять завжди;
    # проміжні за нерухомим годинником — придушені.
    assert stages[0] == 1
    assert stages[-1] == 5
    assert len(stages) < 5


def test_warning_level_bypasses_the_throttle():
    clock = iter([0.0] * 20)
    events = []
    tracker = PhaseTracker(
        Provider.reporting_ninja,
        events.append,
        "datasets",
        throttle_seconds=10.0,
        clock=lambda: next(clock),
    )
    tracker.step("fetch_current_ga4_daily", completed=1, total=10)
    tracker.step("fetch_current_ga4_daily", completed=2, total=10, level="warning")

    assert [event.level for event in events] == ["info", "warning"]


def test_bridge_maps_external_stages_onto_measured_steps():
    events = []
    tracker = PhaseTracker(
        Provider.reporting_ninja, events.append, "kpi", throttle_seconds=0
    )
    on_event = tracker.bridge(("discover", "fetch_current", "fetch_previous"))

    class _External:
        def __init__(self, stage):
            self.stage = stage
            self.message = f"external {stage}"
            self.level = "info"
            self.details = {}

    for stage in ("discover", "fetch_current", "fetch_previous"):
        on_event(_External(stage))

    base, span = phase_bounds(CELL_PHASE_WEIGHTS[Provider.reporting_ninja], "kpi")
    assert [event.completed for event in events] == [1, 2, 3]
    assert events[-1].percent == pytest.approx(base + span)


def test_bridge_treats_an_unknown_external_stage_as_indeterminate():
    # AISeoTools пінований, але може додати стадію: вона не має ані падати,
    # ані вигадувати відсоток.
    events = []
    tracker = PhaseTracker(Provider.ahrefs, events.append, "kpi", throttle_seconds=0)
    on_event = tracker.bridge(("site_audit",))

    class _External:
        stage = "brand_new_stage"
        message = "something new"
        level = "info"
        details = {"detail": "kept"}

    on_event(_External())

    assert events[0].percent is None
    assert events[0].details["indeterminate"] is True
    assert events[0].details["detail"] == "kept"


def test_a_broken_emit_never_escapes_the_tracker():
    def _explode(_event):
        raise RuntimeError("event pipe is down")

    tracker = PhaseTracker(Provider.ahrefs, _explode, "kpi", throttle_seconds=0)
    tracker.begin("open_session")
    tracker.step("site_audit", completed=1, total=2)
    tracker.indeterminate("export_issues")
    tracker.done()  # жодного винятку — збір важливіший за прогрес


def test_external_event_with_an_invalid_level_is_still_delivered():
    # Чужий рівень зронив би валідацію `ProgressEvent` і подія зникла б мовчки.
    events = []
    tracker = PhaseTracker(Provider.gsc, events.append, "kpi", throttle_seconds=0)
    on_event = tracker.bridge(("discover",))

    class _External:
        stage = "discover"
        message = "hello"
        level = "critical"
        details = {}

    on_event(_External())

    assert len(events) == 1
    assert events[0].level == "info"

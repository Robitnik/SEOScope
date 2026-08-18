from __future__ import annotations

from app.reports.svg_charts import render_line_chart


def _points(*values: float | None) -> list[tuple[str, float | None]]:
    labels = [f"M{i}" for i in range(len(values))]
    return list(zip(labels, values, strict=True))


def test_all_none_renders_the_empty_state_not_a_crash():
    svg = render_line_chart(_points(None, None, None), chart_id="c1")
    assert "No data" in svg
    assert "<path" not in svg


def test_max_zero_renders_the_empty_state_not_a_division_by_zero():
    svg = render_line_chart(_points(0, 0, 0), chart_id="c2")
    assert "No data" in svg


def test_equal_nonzero_values_do_not_degenerate_to_zero_height():
    svg = render_line_chart(_points(100, 100, 100), chart_id="c3")
    # A flat line still renders a real path with two distinct x coordinates
    # at the SAME y — never a `nan`/`inf` from a `(v-lo)/(hi-lo)` 0/0.
    assert "nan" not in svg.lower()
    assert "<path d=\"M" in svg


def test_a_gap_breaks_the_line_into_two_segments():
    svg = render_line_chart(_points(10, None, 30), chart_id="c4")
    assert svg.count('fill="none" stroke="#ff4a17"') == 2


def test_eight_and_twelve_points_both_render_every_tick_label():
    for count in (8, 12):
        svg = render_line_chart(_points(*range(1, count + 1)), chart_id=f"c{count}")
        assert svg.count("report-doc-chart__tick") >= count


def test_chart_id_seeds_a_unique_gradient_id():
    svg_a = render_line_chart(_points(1, 2, 3), chart_id="alpha")
    svg_b = render_line_chart(_points(1, 2, 3), chart_id="beta")
    assert "chart-fill-alpha" in svg_a
    assert "chart-fill-beta" in svg_b


def test_labels_are_escaped():
    svg = render_line_chart([("<script>", 1.0)], chart_id="c5")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg

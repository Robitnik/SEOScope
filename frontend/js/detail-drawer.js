import { getClientHistory } from "./api.js?v=20260817-spin1";
import { panelSkeleton } from "./skeleton.js?v=20260817-spin1";
import { PROVIDERS, formatDate, formatRange, formatValue, metricLabel } from "./formatters.js?v=20260817-spin1";

export function createDetailDrawer({ onError }) {
  const dialog = document.querySelector("#detail-dialog");
  const title = document.querySelector("#detail-title");
  const subtitle = document.querySelector("#detail-subtitle");
  const body = document.querySelector("#detail-body");
  let chart = null;
  let opener = null;
  let payload = null;

  dialog.addEventListener("close", () => {
    chart?.destroy();
    chart = null;
    opener?.focus();
  });

  async function open(client, period, trigger) {
    opener = trigger;
    title.textContent = client.name;
    subtitle.textContent = `${client.domain} · last 12 closed periods`;
    body.innerHTML = panelSkeleton({ label: "history", rows: 6, columns: 3 });
    dialog.showModal();
    try {
      payload = await getClientHistory(client.client_id, period);
      render();
    } catch (error) {
      body.innerHTML = `<div class="state-panel state-failed"><p><strong>Failed to load history.</strong><br>${error.message}</p></div>`;
      onError(error);
    }
  }

  function render() {
    const series = payload.series || [];
    if (!series.length) {
      body.innerHTML = `<div class="state-panel"><p><strong>No history yet.</strong><br>Metric trends will appear after a few collections.</p></div>`;
      return;
    }
    body.innerHTML = `
      <div class="drawer-kpis">
        <div><span>Period</span><strong>${payload.period_kind === "month" ? "Month" : "Week"}</strong></div>
        <div><span>Series</span><strong>${series.length}</strong></div>
        <div><span>Latest value</span><strong id="history-latest">No data</strong></div>
      </div>
      <div class="history-toolbar">
        <label>Service<select id="history-provider">${[...new Set(series.map((item) => item.provider))].map((provider) => `<option value="${provider}">${PROVIDERS[provider]?.label || provider}</option>`).join("")}</select></label>
        <label>Metric<select id="history-metric"></select></label>
      </div>
      <div class="chart-wrap"><canvas id="history-chart" aria-label="Metric history chart" role="img"></canvas></div>
      <p class="history-note">The chart shows raw API values without forecasts or automated conclusions.</p>`;
    const providerSelect = body.querySelector("#history-provider");
    const metricSelect = body.querySelector("#history-metric");
    providerSelect.addEventListener("change", () => { populateMetrics(providerSelect.value, metricSelect); draw(metricSelect.value); });
    metricSelect.addEventListener("change", () => draw(metricSelect.value));
    populateMetrics(providerSelect.value, metricSelect);
    draw(metricSelect.value);
  }

  function populateMetrics(provider, select) {
    const matching = payload.series.filter((item) => item.provider === provider);
    select.innerHTML = matching.map((item) => `<option value="${item.metric_key}">${metricLabel(item)}</option>`).join("");
  }

  // "Latest value" must track the metric actually on screen — it used to be
  // frozen to series[0] (Ahrefs health), so viewing CTR showed "100" next to a
  // 0.7 line. Uses the last NON-NULL point: a not-yet-collected latest period
  // is a gap in the chart, not a reason to report "No data" when there is a
  // real recent value.
  function updateLatestValue(series) {
    const el = body.querySelector("#history-latest");
    if (!el) return;
    const point = [...series.points].reverse().find((p) => p.value_numeric !== null && p.value_numeric !== undefined);
    el.textContent = point ? formatValue({ ...series, current_value: point.value_numeric }, "current") : "No data";
  }

  function draw(metricKey) {
    chart?.destroy();
    const provider = body.querySelector("#history-provider").value;
    const series = payload.series.find((item) => item.provider === provider && item.metric_key === metricKey);
    if (!series || !window.Chart) return;
    updateLatestValue(series);

    const values = series.points.map((point) => point.value_numeric === null ? null : Number(point.value_numeric));
    // A line needs at least two readings. With one — the common case until a
    // client has been collected across several periods — Chart.js draws a lone
    // dot that reads as "no chart at all". Fall back to a single bar: it is
    // unambiguously a chart, shows the value against the axis, and the note
    // says plainly why there is only one. (Re-collecting the same week does not
    // add points — the series is one reading per period, deduped.)
    const sparse = values.filter((v) => v !== null).length < 2;
    const note = body.querySelector(".history-note");
    if (note) {
      note.textContent = sparse
        ? "Only one period collected so far — the trend line appears once there are at least two."
        : "The chart shows raw API values without forecasts or automated conclusions.";
    }

    chart = new window.Chart(body.querySelector("#history-chart"), {
      type: sparse ? "bar" : "line",
      data: {
        // Range metrics carry a period; snapshot metrics (e.g. Ahrefs Site
        // Audit) carry only `observed_at`. Labelling both with `formatRange`
        // printed "Period unavailable" for every snapshot point — fall back to
        // the observed date so the x-axis reads honestly either way.
        labels: series.points.map((point) => point.period_start && point.period_end
          ? formatRange({ start: point.period_start, end: point.period_end })
          : (formatDate(point.observed_at) || "Period unavailable")),
        datasets: [{ label: metricLabel(series), data: values, borderColor: "#ff4a17", backgroundColor: "#ff4a17", pointRadius: 3, pointHoverRadius: 5, borderWidth: 2, spanGaps: false, maxBarThickness: 72 }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (context) => context.raw === null ? "No data" : `${metricLabel(series)}: ${context.formattedValue}` } } },
        // A single bar reads clearly only from a zero baseline; a multi-point
        // line keeps its tight auto-scale so real variation stays visible.
        scales: { x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 5 } }, y: { beginAtZero: sparse, grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0" } } },
      },
    });
  }

  return { open };
}

import { escapeHtml } from "./formatters.js?v=20260821-spin3";
import { formatUaNumber, formatUaRange } from "./client-page.js?v=20260821-spin3";

const uaShortDay = new Intl.DateTimeFormat("en-US", { day: "numeric", month: "short" });
const uaShortMonth = new Intl.DateTimeFormat("en-US", { month: "short" });

// Compact tick label ("8–14 Jun") — the full "8 Jun – 14 Jun 2026"
// from `formatUaRange` overlaps at narrow widths with `maxRotation: 0`
// (chosen to keep labels flat/readable rather than diagonal).
function shortChartLabel(start, end) {
  if (!start || !end) return "";
  const startDate = new Date(`${start}T12:00:00`);
  const endDate = new Date(`${end}T12:00:00`);
  if (start === end) return uaShortDay.format(startDate);
  if (startDate.getMonth() === endDate.getMonth() && startDate.getDate() === 1) {
    return uaShortMonth.format(startDate);
  }
  return `${startDate.getDate()}–${uaShortDay.format(endDate)}`;
}

let chartInstance = null;

export function destroyChart() {
  chartInstance?.destroy();
  chartInstance = null;
}

function reducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

// Plain-language trend sentence per series — the accessible alternative to
// the canvas required by AGENTS.md §7, not a caption of it. Uses our own
// Ukrainian label, never the backend catalog's `label` (English — task-04
// zone, out of scope here), so it stays consistent with the legend below.
function summarize(series) {
  return series.map(({ label, item }) => {
    const points = (item.points || []).filter((point) => point.value_numeric !== null);
    if (points.length < 2) return `${label}: not enough data for a trend.`;
    const first = Number(points[0].value_numeric);
    const last = Number(points.at(-1).value_numeric);
    const deltaPct = first !== 0 ? ((last - first) / first) * 100 : null;
    const direction = last > first ? "rose" : last < first ? "fell" : "did not change";
    const pct = deltaPct !== null ? ` (${deltaPct > 0 ? "+" : ""}${deltaPct.toFixed(1)}%)` : "";
    return `${label} ${direction} from ${formatUaNumber(first)} to ${formatUaNumber(last)}${pct}.`;
  }).join(" ");
}

export function renderChart(container, series, period) {
  destroyChart();

  const clicksSeries = series?.find((item) => item.metric_key === "gsc_clicks");
  const impressionsSeries = series?.find((item) => item.metric_key === "gsc_impressions");
  const anySeries = clicksSeries || impressionsSeries;
  const hasPoints = anySeries?.points?.some((point) => point.value_numeric !== null);
  if (!hasPoints) {
    container.innerHTML = `<h2>Clicks and impressions trend</h2><div class="state-panel"><p><strong>No daily GSC series for this range yet.</strong><br>Refresh live data to collect the chart.</p></div>`;
    return;
  }

  const labels = anySeries.points.map((point) => shortChartLabel(point.period_start, point.period_end));
  container.innerHTML = `
    <h2>Clicks and impressions trend</h2>
    <div class="chart-wrap"><canvas id="cp-canvas" role="img" aria-label="Line chart of clicks and impressions trend over recent periods"></canvas></div>
    <p class="chart-summary">${escapeHtml(summarize([
      clicksSeries && { label: "Clicks", item: clicksSeries },
      impressionsSeries && { label: "Impressions", item: impressionsSeries },
    ].filter(Boolean)))}</p>`;
  if (!window.Chart) return;

  chartInstance = new window.Chart(container.querySelector("#cp-canvas"), {
    type: "line",
    data: {
      labels,
      datasets: [
        clicksSeries && {
          label: "Clicks", yAxisID: "y", borderColor: "#ff4a17", backgroundColor: "#ff4a17",
          pointRadius: 3, pointHoverRadius: 5, borderWidth: 2, spanGaps: false,
          data: clicksSeries.points.map((point) => point.value_numeric === null ? null : Number(point.value_numeric)),
        },
        impressionsSeries && {
          label: "Impressions", yAxisID: "y1", borderColor: "#7bc7ff", backgroundColor: "#7bc7ff", borderDash: [6, 4],
          pointRadius: 3, pointHoverRadius: 5, borderWidth: 2, spanGaps: false,
          data: impressionsSeries.points.map((point) => point.value_numeric === null ? null : Number(point.value_numeric)),
        },
      ].filter(Boolean),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: reducedMotion() ? false : undefined,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: true, labels: { color: "#aaa6a0", usePointStyle: true } },
        tooltip: {
          callbacks: {
            title: (items) => {
              const point = anySeries.points[items[0]?.dataIndex];
              return point ? formatUaRange(point.period_start, point.period_end) : "";
            },
            label: (context) => context.raw === null
              ? `${context.dataset.label}: no data`
              : `${context.dataset.label}: ${new Intl.NumberFormat("en-US").format(context.raw)}`,
          },
        },
      },
      scales: {
        x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 6 } },
        y: { position: "left", grid: { color: "#2a2a2a" }, ticks: { color: "#ff8a5c" }, title: { display: true, text: "Clicks", color: "#aaa6a0" } },
        y1: { position: "right", grid: { display: false }, ticks: { color: "#7bc7ff" }, title: { display: true, text: "Impressions", color: "#aaa6a0" } },
      },
    },
  });
}

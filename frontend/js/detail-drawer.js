import { getClientHistory } from "./api.js";
import { PROVIDERS, formatRange, formatValue, metricLabel } from "./formatters.js";

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
    body.innerHTML = `<div class="skeleton-line skeleton-line--title"></div><div class="skeleton-block"></div>`;
    dialog.showModal();
    try {
      payload = await getClientHistory(client.client_id, period);
      render();
    } catch (error) {
      body.innerHTML = `<div class="state-panel state-failed"><p><strong>History could not be loaded.</strong><br>${error.message}</p></div>`;
      onError(error);
    }
  }

  function render() {
    const series = payload.series || [];
    if (!series.length) {
      body.innerHTML = `<div class="state-panel"><p><strong>No history yet.</strong><br>Metric trends will appear after several refreshes.</p></div>`;
      return;
    }
    const first = series[0];
    const firstPoint = first.points?.at(-1);
    body.innerHTML = `
      <div class="drawer-kpis">
        <div><span>Period</span><strong>${payload.period_kind === "month" ? "Month" : "Week"}</strong></div>
        <div><span>Series</span><strong>${series.length}</strong></div>
        <div><span>Latest value</span><strong>${firstPoint ? formatValue({ ...first, current_value: firstPoint.value_numeric }, "current") : "No data"}</strong></div>
      </div>
      <div class="history-toolbar">
        <label>Service<select id="history-provider">${[...new Set(series.map((item) => item.provider))].map((provider) => `<option value="${provider}">${PROVIDERS[provider]?.label || provider}</option>`).join("")}</select></label>
        <label>Metric<select id="history-metric"></select></label>
      </div>
      <div class="chart-wrap"><canvas id="history-chart" aria-label="Metric history chart" role="img"></canvas></div>
      <p class="history-note">The chart shows API values without forecasts or automated conclusions.</p>`;
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

  function draw(metricKey) {
    chart?.destroy();
    const provider = body.querySelector("#history-provider").value;
    const series = payload.series.find((item) => item.provider === provider && item.metric_key === metricKey);
    if (!series || !window.Chart) return;
    chart = new window.Chart(body.querySelector("#history-chart"), {
      type: "line",
      data: {
        labels: series.points.map((point) => formatRange({ start: point.period_start, end: point.period_end })),
        datasets: [{ label: metricLabel(series), data: series.points.map((point) => point.value_numeric === null ? null : Number(point.value_numeric)), borderColor: "#ff4a17", backgroundColor: "#ff4a17", pointRadius: 3, pointHoverRadius: 5, borderWidth: 2, spanGaps: false }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: { legend: { display: false }, tooltip: { callbacks: { label: (context) => context.raw === null ? "No data" : `${metricLabel(series)}: ${context.formattedValue}` } } },
        scales: { x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 5 } }, y: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0" } } },
      },
    });
  }

  return { open };
}

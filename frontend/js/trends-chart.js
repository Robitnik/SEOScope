// Portfolio Trends chart (task-28): one Chart.js line per client, plus the
// small-multiples "All clients" view for portfolios past the safe color cap.
//
// Color rule (dataviz skill, validated against surface #1e1e1e — CVD floor
// 8.4/tritan 8.7, normal-vision floor 19.3, all >= 3:1 contrast): exactly 8
// categorical slots, assigned in a FIXED order and never generated past 8.
// `ColorAllocator` below keeps a client's slot stable across refetches (same
// client always gets the same color while it stays in view) and frees a slot
// the moment its client drops out, rather than reshuffling everyone still on
// screen — a filter change must not repaint the survivors.

import { escapeHtml } from "./formatters.js?v=20260812-monitor7";

export const PALETTE = [
  "#3987e5", "#d95926", "#199e70", "#c98500",
  "#d55181", "#008300", "#9085e9", "#e66767",
];
export const MAX_SERIES = PALETTE.length;

const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compactFormatter = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const shortMonth = new Intl.DateTimeFormat("en-US", { month: "short" });
const shortDay = new Intl.DateTimeFormat("en-US", { day: "numeric", month: "short" });

function fmt(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return Math.abs(n) >= 100_000 ? compactFormatter.format(n) : numberFormatter.format(n);
}

// Compact bucket label ("8–14 Jun" / "Jun" for a month bucket / "8 Jun" for a
// day bucket) — mirrors the client-page chart's own `shortChartLabel`
// (frontend/js/charts.js), reimplemented locally rather than imported since
// that module belongs to a different owned zone (task-18).
export function bucketLabel(bucket, kind) {
  const start = new Date(`${bucket.start}T12:00:00`);
  const end = new Date(`${bucket.end}T12:00:00`);
  if (kind === "day") return shortDay.format(start);
  if (kind === "month") return shortMonth.format(start);
  if (bucket.start === bucket.end) return shortDay.format(start);
  return `${start.getDate()}–${shortDay.format(end)}`;
}

class ColorAllocator {
  constructor() {
    this._slotByClient = new Map();
  }

  /** Returns `Map(client_id -> hex)` for at most the first `MAX_SERIES` of
   * `orderedClientIds`. Existing owners keep their slot; new ids take the
   * lowest free one; ids no longer present free theirs for reuse. */
  assign(orderedClientIds) {
    const wanted = orderedClientIds.slice(0, MAX_SERIES);
    const wantedSet = new Set(wanted);
    for (const id of this._slotByClient.keys()) {
      if (!wantedSet.has(id)) this._slotByClient.delete(id);
    }
    const used = new Set(this._slotByClient.values());
    const result = new Map();
    for (const id of wanted) {
      let slot = this._slotByClient.get(id);
      if (slot === undefined) {
        slot = 0;
        while (used.has(slot)) slot += 1;
        this._slotByClient.set(id, slot);
        used.add(slot);
      }
      result.set(id, PALETTE[slot]);
    }
    return result;
  }
}

export const colorAllocator = new ColorAllocator();

function reducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

let chartInstance = null;

export function destroyTrendsChart() {
  chartInstance?.destroy();
  chartInstance = null;
}

/** First bucket index (in encounter order) where every visible series has a
 * non-null value — the fair baseline for the `Indexed` view, and also the
 * boundary of the "leading sparse" region some clients haven't reached yet
 * (the `dashed hole` marker, plan follow-up: the user asked to mark this
 * region visibly rather than let lines silently "appear" mid-chart).
 * Returns `null` when no such bucket exists (nobody has full coverage). */
function firstCommonBaseline(series) {
  if (!series.length) return null;
  const bucketCount = series[0].points.length;
  for (let i = 0; i < bucketCount; i += 1) {
    if (series.every((item) => item.points[i]?.value !== null)) return i;
  }
  return null;
}

/** Shades the leading region `[0, bandEndIndex)` where at least one displayed
 * client has no data yet, and labels it — so a line that only starts partway
 * through the window reads as "not collected yet here", not as a rendering
 * bug. Returns `null` (no plugin) when there is nothing to mark. */
function buildSparseBandPlugin(bandEndIndex, bucketCount) {
  if (!bandEndIndex || bandEndIndex <= 0 || bandEndIndex >= bucketCount) return null;

  function bandPixels(chart) {
    const { chartArea, scales } = chart;
    const xScale = scales.x;
    const cellWidth = bucketCount > 1 ? xScale.getPixelForValue(1) - xScale.getPixelForValue(0) : chartArea.width;
    const left = chartArea.left;
    const right = Math.min(chartArea.right, xScale.getPixelForValue(bandEndIndex - 1) + cellWidth / 2);
    return { left, right };
  }

  return {
    id: "trendsSparseBand",
    beforeDatasetsDraw(chart) {
      const { left, right } = bandPixels(chart);
      if (right <= left) return;
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.fillStyle = "rgba(255, 255, 255, 0.035)";
      ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
      ctx.restore();
    },
    afterDraw(chart) {
      const { left, right } = bandPixels(chart);
      const width = right - left;
      if (width < 70) return;
      const { ctx, chartArea } = chart;
      ctx.save();
      ctx.fillStyle = "#77736e";
      ctx.font = "11px Archivo, Arial, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillText("Not all clients yet", left + width / 2, chartArea.top + 6);
      ctx.restore();
    },
  };
}

export function renderLineChart(canvas, { series, buckets, bucketKind, indexed }) {
  destroyTrendsChart();
  if (!window.Chart || !series.length) return null;

  const colors = colorAllocator.assign(series.map((item) => item.client_id));
  const commonBaselineIndex = firstCommonBaseline(series);
  const baseline = indexed ? commonBaselineIndex : null;
  const labels = buckets.map((bucket) => bucketLabel(bucket, bucketKind));
  const lastBucketIndex = buckets.length - 1;
  const sparseBand = buildSparseBandPlugin(commonBaselineIndex, buckets.length);

  const datasets = series
    .filter((item) => !indexed || (baseline !== null && item.points[baseline]?.value !== null))
    .map((item) => {
      const color = colors.get(item.client_id) || "#77736e";
      const baseValue = baseline !== null ? Number(item.points[baseline].value) : null;
      const data = item.points.map((point) => {
        if (point.value === null) return null;
        if (indexed) return baseValue ? (Number(point.value) / baseValue) * 100 : null;
        return Number(point.value);
      });
      return {
        label: item.name,
        clientId: item.client_id,
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        pointRadius: (ctx) => (item.points[ctx.dataIndex]?.quality === "partial" ? 4 : 3),
        pointHoverRadius: 5,
        pointBackgroundColor: (ctx) => (item.points[ctx.dataIndex]?.quality === "partial" ? "#1e1e1e" : color),
        pointBorderColor: color,
        pointBorderWidth: (ctx) => (item.points[ctx.dataIndex]?.quality === "partial" ? 2 : 0),
        spanGaps: false,
        segment: {
          borderDash: (ctx) => {
            const toIndex = ctx.p1DataIndex;
            const isProvisionalEdge = toIndex === lastBucketIndex && buckets[lastBucketIndex]?.provisional;
            const isPartialEdge = item.points[toIndex]?.quality === "partial";
            return isProvisionalEdge || isPartialEdge ? [5, 4] : undefined;
          },
        },
        data,
      };
    });

  chartInstance = new window.Chart(canvas, {
    type: "line",
    data: { labels, datasets },
    plugins: sparseBand ? [sparseBand] : [],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: reducedMotion() ? false : undefined,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { display: true, position: "bottom", labels: { color: "#aaa6a0", usePointStyle: true, boxHeight: 8 } },
        tooltip: {
          itemSort: (a, b) => (b.parsed.y ?? -Infinity) - (a.parsed.y ?? -Infinity),
          callbacks: {
            title: (items) => {
              const bucket = buckets[items[0]?.dataIndex];
              if (!bucket) return "";
              const flag = bucket.provisional ? " (still filling)" : "";
              return `${bucketLabel(bucket, bucketKind)}${flag}`;
            },
            label: (context) => {
              const item = series.find((s) => s.client_id === context.dataset.clientId);
              const point = item?.points[context.dataIndex];
              if (!point || point.value === null) return `${context.dataset.label}: no data`;
              const suffix = point.quality === "partial" ? ` (${point.days_with_data}d partial)` : "";
              const value = indexed ? `${fmt(context.raw)} idx` : fmt(point.value);
              return `${context.dataset.label}: ${value}${suffix}`;
            },
          },
        },
      },
      scales: {
        x: { grid: { color: "#2a2a2a" }, ticks: { color: "#aaa6a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } },
        y: {
          grid: { color: "#2a2a2a" },
          ticks: { color: "#aaa6a0", callback: (value) => (indexed ? `${value}` : fmt(value)) },
          title: { display: true, text: indexed ? "Indexed (first common week = 100)" : undefined, color: "#aaa6a0" },
        },
      },
    },
  });
  return chartInstance;
}

const sparklineInstances = [];

export function destroySparklines() {
  sparklineInstances.forEach((chart) => chart.destroy());
  sparklineInstances.length = 0;
}

/** One tiny axis-less line per client — the fallback for portfolios beyond
 * the 8-line safe cap. Each card gets its own color and scale, so volume
 * differences between clients never hide a smaller one (plan §6.3). */
export function renderSparklineGrid(container, { series, excluded, buckets }) {
  destroySparklines();
  // The headline value and its delta must read from the last FINISHED
  // bucket, exactly like the main chart's table (`lastFinishedBucketIndex`
  // in trends-page.js) — otherwise a still-filling provisional day (real but
  // partial) reads as a dramatic, misleading swing against a full prior day.
  let lastFinishedIndex = buckets.length - 1;
  for (let i = buckets.length - 1; i >= 0; i -= 1) {
    if (!buckets[i].provisional) { lastFinishedIndex = i; break; }
  }
  const cards = series.map((item) => {
    const values = item.points.map((point) => (point.value === null ? null : Number(point.value)));
    const finishedPoints = item.points.slice(0, lastFinishedIndex + 1);
    const last = [...finishedPoints].reverse().find((point) => point.value !== null);
    const first = finishedPoints.find((point) => point.value !== null);
    const trendPct = first && last && Number(first.value) !== 0
      ? ((Number(last.value) - Number(first.value)) / Number(first.value)) * 100
      : null;
    return `
      <article class="trend-card">
        <header>
          <strong>${escapeHtml(item.name)}</strong>
          <span>${escapeHtml(item.domain)}</span>
        </header>
        <div class="trend-card__chart"><canvas data-client="${escapeHtml(item.client_id)}" role="img" aria-label="Trend for ${escapeHtml(item.name)}"></canvas></div>
        <footer>
          <span class="trend-card__value">${last ? fmt(last.value) : "No data"}</span>
          ${trendPct !== null ? `<span class="trend-card__delta" data-trend="${trendPct >= 0 ? "good" : "bad"}">${trendPct >= 0 ? "+" : ""}${trendPct.toFixed(1)}%</span>` : ""}
        </footer>
      </article>`;
  }).join("");

  const excludedCards = excluded.map((item) => `
    <article class="trend-card trend-card--excluded">
      <header>
        <strong>${escapeHtml(item.name)}</strong>
        <span>${escapeHtml(item.domain)}</span>
      </header>
      <p class="trend-card__reason">${escapeHtml(item.reason)}</p>
    </article>`).join("");

  container.innerHTML = `<div class="trend-grid">${cards}${excludedCards}</div>`;
  if (!window.Chart) return;

  series.forEach((item, index) => {
    const canvas = container.querySelector(`canvas[data-client="${CSS.escape(item.client_id)}"]`);
    if (!canvas) return;
    const color = PALETTE[index % PALETTE.length];
    const chart = new window.Chart(canvas, {
      type: "line",
      data: {
        labels: item.points.map((_, i) => i),
        datasets: [{
          data: item.points.map((point) => (point.value === null ? null : Number(point.value))),
          borderColor: color, backgroundColor: color, borderWidth: 2,
          pointRadius: 0, spanGaps: false, tension: 0.15,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        animation: reducedMotion() ? false : undefined,
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: { x: { display: false }, y: { display: false } },
      },
    });
    sparklineInstances.push(chart);
  });
}

// Plain-language trend sentence — the accessible alternative to the canvas
// (AGENTS.md §7), not a caption of it. Excludes the provisional bucket from
// the "rose/fell" read since it is not a finished comparison point yet.
export function buildSummary({ series, buckets, metric, bucketKind }) {
  if (!series.length) return "No client has collected data in this range yet.";
  const lastFinishedIndex = buckets.findLastIndex ? buckets.findLastIndex((b) => !b.provisional)
    : (() => { for (let i = buckets.length - 1; i >= 0; i -= 1) if (!buckets[i].provisional) return i; return -1; })();
  if (lastFinishedIndex < 1) return `Showing ${series.length} client(s); not enough finished periods yet for a trend.`;

  const moves = series.map((item) => {
    const current = item.points[lastFinishedIndex]?.value;
    const previous = item.points[lastFinishedIndex - 1]?.value;
    if (current === null || previous === null || current === undefined || previous === undefined) return null;
    return { name: item.name, delta: Number(current) - Number(previous) };
  }).filter(Boolean);

  const rising = moves.filter((m) => m.delta > 0).length;
  const falling = moves.filter((m) => m.delta < 0).length;
  const biggest = [...moves].sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta))[0];
  const provisionalNote = buckets[buckets.length - 1]?.provisional
    ? " The most recent period is still filling and is excluded from this comparison."
    : "";
  const biggestNote = biggest
    ? ` ${biggest.name} moved the most (${biggest.delta >= 0 ? "+" : ""}${fmt(biggest.delta)} ${metric.label.toLowerCase()}).`
    : "";

  // The text counterpart of the chart's shaded "leading sparse" band (plan
  // follow-up): names which clients don't have data yet, instead of just
  // leaving their line to silently appear mid-chart.
  const baselineIndex = firstCommonBaseline(series);
  const lateStarters = series.filter((item) => item.points[0]?.value === null);
  const sparseNote = baselineIndex !== null && baselineIndex > 0 && lateStarters.length > 0
    ? ` ${lateStarters.length} of ${series.length} client(s) have no data before ${bucketLabel(buckets[baselineIndex], bucketKind)}.`
    : "";

  return `${rising} client(s) rose and ${falling} fell period over period.${biggestNote}${provisionalNote}${sparseNote}`;
}

/* Indexing Planner — Analytics (task-100 §2.6): does this work at all?
 *
 * The journal alone proves nothing — pages enter the index without us. So
 * the headline here is not the median lag of submitted pages, it is the
 * DIFFERENCE between that and a control group: pages that reached the index
 * over the same window having never been submitted.
 *
 * If there is no difference, this screen says so. A hundred manual
 * submissions a day is an expensive habit, and "stop doing this" is the most
 * valuable thing the tool can report.
 *
 * Two honesty rules, both visible in the markup:
 * - an empty sample prints "—", never 0. Zero days would read as "indexed
 *   the same day", the best possible outcome.
 * - pages submitted and still waiting are counted separately as "in flight",
 *   never as failures, or the median would drop after every big batch.
 */
import { getIndexingAnalytics } from "./api.js?v=20260921-t100";
import { escapeHtml, icon } from "./formatters.js?v=20260921-t100";
import { reasonLabel } from "./indexing-tool.js?v=20260921-t100";

function days(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(1)}d`;
}

function pct(value) {
  if (value === null || value === undefined) return "—";
  return `${(Number(value) * 100).toFixed(0)}%`;
}

function plain(value) {
  return value === null || value === undefined ? "—" : String(value);
}

export async function renderAnalytics(host) {
  const data = await getIndexingAnalytics(180);
  host.innerHTML = `
    ${verdictBlock(data)}
    ${funnelBlock(data)}
    ${reasonBlock(data)}
    ${retentionBlock(data)}
    ${clientBlock(data)}`;
}

function verdictBlock(data) {
  const { submitted, organic, difference_days: difference } = data.control_group;
  let verdict;
  if (difference === null || difference === undefined) {
    verdict = `
      <p class="indexing-verdict__none">Not enough evidence yet. A verdict needs completed observations in
        both groups: pages we submitted, and pages that reached the index on their own.</p>`;
  } else if (Number(difference) > 1) {
    verdict = `<p class="indexing-verdict__good">${icon("trending-up")} Submitted pages reach the index about
      <strong>${days(difference)}</strong> sooner than pages left alone.</p>`;
  } else if (Number(difference) < -1) {
    verdict = `<p class="indexing-verdict__bad">${icon("trending-down")} Submitted pages are
      <strong>${days(Math.abs(Number(difference)))}</strong> <em>slower</em> than pages left alone — the queue is
      picking the hard cases, not the manual submissions failing.</p>`;
  } else {
    verdict = `<p class="indexing-verdict__flat">${icon("equal")} No measurable difference between submitting by
      hand and doing nothing. On this evidence the manual work is not paying for itself.</p>`;
  }
  return `
    <section class="indexing-verdict">
      <h2>Does submitting by hand help?</h2>
      ${verdict}
      <table class="indexing-table indexing-table--compact">
        <thead>
          <tr><th scope="col">Group</th><th scope="col">Sample</th><th scope="col">Median</th>
            <th scope="col">p25</th><th scope="col">p75</th></tr>
        </thead>
        <tbody>
          <tr><th scope="row">Submitted by us</th><td>${submitted.sample}</td><td>${days(submitted.median_days)}</td>
            <td>${days(submitted.p25_days)}</td><td>${days(submitted.p75_days)}</td></tr>
          <tr><th scope="row">Control group — never submitted</th><td>${organic.sample}</td>
            <td>${days(organic.median_days)}</td><td>${days(organic.p25_days)}</td><td>${days(organic.p75_days)}</td></tr>
        </tbody>
      </table>
      <p class="indexing-note">Both groups are measured the same way: from the day we ourselves saw the page
        outside the index, to the day we saw it in. A page that was already indexed when the planner first
        met it is not an observation and is left out entirely — counting it would score as zero days and make
        doing nothing look instant. Pages still waiting are excluded from the median and counted as
        “in flight” below.</p>
    </section>`;
}

function funnelBlock(data) {
  const f = data.funnel;
  const steps = [
    ["Planned", f.planned],
    ["Sent", f.sent],
    ["Blocked by GSC quota", f.quota_blocked],
    ["Reached the index", f.indexed],
    ["Still in flight", f.in_flight],
  ];
  return `
    <section class="indexing-panel">
      <h2>Last ${data.window_days} days</h2>
      <dl class="indexing-stats">
        ${steps
          .map(
            ([label, value]) => `
          <div class="indexing-stat">
            <dt>${escapeHtml(label)}</dt>
            <dd>${value}</dd>
          </div>`,
          )
          .join("")}
      </dl>
    </section>`;
}

function reasonBlock(data) {
  if (!data.by_reason.length) {
    return `
      <section class="indexing-panel">
        <h2>By reason</h2>
        <p class="indexing-note">Nothing has been submitted yet, so there is nothing to compare.</p>
      </section>`;
  }
  const rows = data.by_reason
    .map(
      (row) => `
      <tr>
        <td>${escapeHtml(reasonLabel(row.reason_code))}</td>
        <td class="indexing-num">${row.sent}</td>
        <td class="indexing-num">${row.indexed}</td>
        <td class="indexing-num">${row.in_flight}</td>
        <td class="indexing-num">${days(row.median_days)}</td>
        <td class="indexing-num">${pct(row.conversion)}</td>
      </tr>`,
    )
    .join("");
  return `
    <section class="indexing-panel">
      <h2>By reason</h2>
      <p class="indexing-note">Which reason actually converts. After a quarter of real data this is what
        re-weights the queue — the current weights are a reasoned guess, not a measurement.</p>
      <table class="indexing-table indexing-table--compact">
        <thead>
          <tr><th scope="col">Reason</th><th scope="col" class="indexing-num">Sent</th>
            <th scope="col" class="indexing-num">Indexed</th><th scope="col" class="indexing-num">In flight</th>
            <th scope="col" class="indexing-num">Median</th><th scope="col" class="indexing-num">Conversion</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function retentionBlock(data) {
  const rows = data.retention
    .map(
      (row) => `
      <tr>
        <td>${row.horizon_days} days</td>
        <td class="indexing-num">${row.observed}</td>
        <td class="indexing-num">${row.still_indexed}</td>
        <td class="indexing-num">${row.dropped}</td>
      </tr>`,
    )
    .join("");
  return `
    <section class="indexing-panel">
      <h2>Do they stay in?</h2>
      <table class="indexing-table indexing-table--compact">
        <thead>
          <tr><th scope="col">Observed for</th><th scope="col" class="indexing-num">Pages</th>
            <th scope="col" class="indexing-num">Still indexed</th><th scope="col" class="indexing-num">Dropped out</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </section>`;
}

function clientBlock(data) {
  const rows = data.by_client
    .map(
      (row) => `
      <tr>
        <td>${escapeHtml(row.client_name)}</td>
        <td class="indexing-num">${plain(row.urls_total)}</td>
        <td class="indexing-num">${row.coverage_ratio === null || row.coverage_ratio === undefined ? "—" : `${(Number(row.coverage_ratio) * 100).toFixed(1)}%`}</td>
        <td class="indexing-num">${plain(row.queue)}</td>
        <td class="indexing-num">${row.sent_30d}</td>
        <td class="indexing-num">${days(row.median_days)}</td>
        <td>${row.cycle_holds === null || row.cycle_holds === undefined ? "—" : row.cycle_holds ? "holds" : "at risk"}</td>
      </tr>`,
    )
    .join("");
  return `
    <section class="indexing-panel">
      <h2>By client</h2>
      <div class="indexing-table-wrap">
        <table class="indexing-table indexing-table--compact">
          <thead>
            <tr><th scope="col">Client</th><th scope="col" class="indexing-num">Pages</th>
              <th scope="col" class="indexing-num">Coverage</th><th scope="col" class="indexing-num">Queue</th>
              <th scope="col" class="indexing-num">Sent 30d</th><th scope="col" class="indexing-num">Median</th>
              <th scope="col">90-day cycle</th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </section>`;
}

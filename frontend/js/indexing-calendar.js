/* Indexing Planner — Calendar (task-100 §2.5).
 *
 * Today's column is frozen: the nightly replan rebuilds the next seven days
 * but never touches today, so an operator mid-way through carrying a batch
 * to GSC does not watch rows vanish. The grid marks that difference, because
 * "this can still change" and "this is what you are doing now" are different
 * promises.
 *
 * Beyond the horizon there are no rows, only a forecast: index state moves
 * daily, and a materialised 90-day plan would be stale within a week.
 */
import { getIndexingCalendar } from "./api.js?v=20260921-t100";
import { escapeHtml, icon } from "./formatters.js?v=20260921-t100";
import { reasonLabel } from "./indexing-tool.js?v=20260921-t100";

const WEEKDAY = new Intl.DateTimeFormat("en-US", { weekday: "short" });
const DAY = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });

export async function renderCalendar(host) {
  const data = await getIndexingCalendar(8);
  const enabled = data.clients.filter((client) => client.settings.enabled);

  if (!enabled.length) {
    host.innerHTML = `
      <div class="state-panel">
        ${icon("calendar-off")}
        <p><strong>The planner isn't switched on for any client yet.</strong><br>
          Turn it on per client in the backlog view to start building a calendar.</p>
      </div>`;
    return;
  }

  const cells = new Map();
  data.cells.forEach((cell) => cells.set(`${cell.client_id}|${cell.day}`, cell));

  const header = data.days
    .map((day) => {
      const frozen = day === data.frozen_day;
      const date = new Date(`${day}T00:00:00Z`);
      return `<th scope="col" class="${frozen ? "is-frozen" : ""}">
        <span class="indexing-cal__weekday">${escapeHtml(WEEKDAY.format(date))}</span>
        <span class="indexing-cal__date">${escapeHtml(DAY.format(date))}</span>
        ${frozen ? `<span class="indexing-cal__frozen">${icon("lock")} frozen</span>` : ""}
      </th>`;
    })
    .join("");

  const rows = enabled
    .map((client) => {
      const columns = data.days
        .map((day) => {
          const cell = cells.get(`${client.id}|${day}`);
          const frozen = day === data.frozen_day ? " is-frozen" : "";
          if (!cell) {
            return `<td class="indexing-cal__cell is-empty${frozen}"><span class="indexing-cal__none">—</span></td>`;
          }
          const reasons = Object.entries(cell.reason_counts)
            .map(([code, n]) => `${reasonLabel(code)}: ${n}`)
            .join(", ");
          return `<td class="indexing-cal__cell${frozen}" title="${escapeHtml(reasons)}">
            <span class="indexing-cal__count">${cell.planned + cell.sent}</span>
            ${cell.sent ? `<span class="indexing-cal__sent">${cell.sent} sent</span>` : ""}
          </td>`;
        })
        .join("");
      return `<tr><th scope="row">${escapeHtml(client.name)}<span class="indexing-cal__quota">${client.settings.daily_quota}/day</span></th>${columns}</tr>`;
    })
    .join("");

  const forecast =
    data.days_to_clear_queue === null || data.days_to_clear_queue === undefined
      ? `The queue is empty — nothing is waiting to be submitted.`
      : `${data.queue_size} pages queued · ${data.daily_capacity} slots a day → about ${data.days_to_clear_queue} working days to clear.`;
  const cycle =
    data.cycle_holds === false
      ? `<p class="indexing-forecast__warn">${icon("alert-triangle")} At this pace the 90-day re-submission cycle will slip.</p>`
      : "";

  host.innerHTML = `
    <div class="indexing-cal-wrap">
      <table class="indexing-cal">
        <thead><tr><th scope="col">Client</th>${header}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="indexing-forecast">
      <p>${escapeHtml(forecast)}</p>
      ${cycle}
      <p class="indexing-forecast__note">Days past the horizon aren't planned on purpose: index state changes
        daily, so a fixed three-month schedule would be wrong within a week.</p>
    </div>`;
}

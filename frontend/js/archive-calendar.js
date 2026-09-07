// Shared "Wayback-style" version calendar — one day per cell, versions
// grouped underneath. Originally built inline in the Web Archive hub
// (task-59); extracted here (task-63 §4) so the replay viewer's own banner
// can show the exact same calendar without a second implementation.
//
// Pure rendering + cell math, no module-level state and no DOM lookups of
// its own: the caller owns its state slice (`year`/`month`/`selectedDay`)
// and wires up the markup this module returns via `bindCalendarActions`.
import { icon } from "./formatters.js?v=20260905-t63";

export const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export const MONTH_LABELS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// One cell per day of `year`/`month` (0-indexed), padded with `null`s so the
// grid always starts on the correct weekday column — same shape the real
// Wayback Machine calendar uses.
export function buildCalendarCells(year, month, groupsByDay) {
  const firstWeekday = new Date(Date.UTC(year, month, 1)).getUTCDay();
  const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const cells = new Array(firstWeekday).fill(null);
  for (let day = 1; day <= daysInMonth; day += 1) {
    const iso = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    cells.push({ iso, day, points: groupsByDay.get(iso) || [] });
  }
  return cells;
}

export function groupSnapshotsByDay(snapshots) {
  const groups = new Map();
  for (const snapshot of snapshots) {
    const day = snapshot.observed_at.slice(0, 10);
    if (!groups.has(day)) groups.set(day, []);
    groups.get(day).push(snapshot);
  }
  return groups;
}

export function calendarMarkup({ year, month, selectedDay, groups }) {
  const cells = buildCalendarCells(year, month, groups);
  return `
    <div class="archive-calendar">
      <div class="archive-calendar__header">
        <button type="button" class="icon-button" data-action="cal-prev" aria-label="Previous month">${icon("chevron-left")}</button>
        <p class="archive-calendar__month">${MONTH_LABELS[month]} ${year}</p>
        <button type="button" class="icon-button" data-action="cal-next" aria-label="Next month">${icon("chevron-right")}</button>
      </div>
      <div class="archive-calendar__weekdays">
        ${WEEKDAY_LABELS.map((label) => `<span>${label}</span>`).join("")}
      </div>
      <div class="archive-calendar__grid">
        ${cells.map((cell) => {
          if (!cell) return `<span class="archive-calendar__day archive-calendar__day--empty"></span>`;
          const hasCapture = cell.points.length > 0;
          const changed = cell.points.some((p) => p.has_change);
          const isSelected = cell.iso === selectedDay;
          return `
            <button type="button" class="archive-calendar__day${hasCapture ? " archive-calendar__day--has-capture" : ""}${isSelected ? " archive-calendar__day--selected" : ""}${changed ? " archive-calendar__day--changed" : ""}"
              data-day="${cell.iso}" ${hasCapture ? "" : "disabled"}
              aria-current="${isSelected ? "date" : "false"}" aria-label="${cell.iso}${hasCapture ? `, ${cell.points.length} version${cell.points.length === 1 ? "" : "s"}` : ", no capture"}">
              ${cell.day}
            </button>`;
        }).join("")}
      </div>
    </div>`;
}

// Wires month prev/next + day-pick clicks inside `container`, which must
// already hold the markup `calendarMarkup` produced. `onSelectDay` gets the
// clicked day's ISO string; `onPrevMonth`/`onNextMonth` take no arguments.
export function bindCalendarActions(container, { onPrevMonth, onNextMonth, onSelectDay }) {
  container.querySelector('[data-action="cal-prev"]')?.addEventListener("click", onPrevMonth);
  container.querySelector('[data-action="cal-next"]')?.addEventListener("click", onNextMonth);
  container.querySelectorAll(".archive-calendar__day[data-day]").forEach((button) => {
    button.addEventListener("click", () => onSelectDay(button.dataset.day));
  });
}

export function reasonLabel(reason) {
  return (
    {
      first: "First capture", changed: "Changed",
      weekly_refresh: "Confirmed unchanged", status_changed: "Status changed",
      forced: "Forced copy",
    }[reason] || reason
  );
}

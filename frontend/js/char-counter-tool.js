/* Character Counter tool.
 *
 * Повністю клієнтський скретчпад: жодного бекенд-виклику й нічого не
 * зберігається, тому в каталозі (app/api/tools.py) в нього свідомо немає
 * статистики (PLAN §12) — картка завжди показує «Not used yet».
 */
import { buildToolsHash } from "./router.js?v=20260821-spin3";
import { icon, stateClass } from "./formatters.js?v=20260821-spin3";

let root = null;

// Common on-page SEO length guidelines. Ranges, not hard rules — worded as
// such in the hint so the block reads as advice, not a pass/fail data truth.
const SEO_FIELDS = [
  {
    key: "title",
    label: "Title tag",
    min: 30,
    max: 60,
    hint: "30–60 characters usually stays on one line in search results.",
  },
  {
    key: "description",
    label: "Meta description",
    min: 70,
    max: 160,
    hint: "70–160 characters — Google typically shows up to ~155–160 before cutting it off.",
  },
  {
    key: "alt",
    label: "Image alt text",
    min: 20,
    max: 125,
    hint: "20–125 characters: enough to describe the image, short enough that screen readers don't cut it off.",
  },
];

export async function initTool(mount) {
  root = mount;
  root.innerHTML = `
    <a class="back-link" href="${buildToolsHash()}">${icon("arrow-left")}<span>All tools</span></a>
    <header class="tool-header">
      <p class="eyebrow">Tools / Character Counter</p>
      <h1>Character counter</h1>
      <p class="tool-lead">Type or paste text below and watch the character count update as you go —
        handy for meta descriptions, title tags and any character-limited copy.</p>
    </header>
    <section class="tool-panel" aria-labelledby="tc-panel-title">
      <div class="section-heading"><div><h2 id="tc-panel-title">Text</h2></div></div>
      <div class="tool-panel__body">
        <label class="form-field" for="tc-input">Text
          <textarea id="tc-input" class="tc-textarea" rows="14" spellcheck="false"
            placeholder="Type or paste your text here…"></textarea>
        </label>
        <div class="tc-toolbar">
          <p class="field-help tc-count" id="tc-count" aria-live="polite">0 characters</p>
          <button class="button button--quiet" type="button" id="tc-paste">${icon("clipboard-typography", "icon icon--tiny")}Paste</button>
        </div>
      </div>
    </section>
    <section class="tool-panel" aria-labelledby="tc-seo-title">
      <div class="section-heading"><div><h2 id="tc-seo-title">SEO length check</h2>
        <p class="tool-panel__hint">How the text above measures up against common length guidelines for a
          title tag, meta description and image alt text.</p></div></div>
      <div class="tool-panel__body">
        <div class="tc-seo" id="tc-seo" aria-live="polite"></div>
      </div>
    </section>`;

  const textarea = root.querySelector("#tc-input");
  textarea.addEventListener("input", updateAll);
  root.querySelector("#tc-paste").addEventListener("click", () => void pasteFromClipboard());
  textarea.focus();
  updateAll();
}

function updateAll() {
  updateCount();
  updateSeoBlock();
}

function updateCount() {
  const n = root.querySelector("#tc-input").value.length;
  root.querySelector("#tc-count").textContent = `${n} ${n === 1 ? "character" : "characters"}`;
}

function seoStatus(field, length) {
  if (length === 0) return { state: "attention", icon: "alert-triangle", label: "Empty" };
  if (length < field.min) return { state: "attention", icon: "alert-triangle", label: "Too short" };
  if (length > field.max) return { state: "failed", icon: "circle-x", label: "Too long" };
  return { state: "ok", icon: "circle-check", label: "Good" };
}

function updateSeoBlock() {
  const length = root.querySelector("#tc-input").value.length;
  root.querySelector("#tc-seo").innerHTML = SEO_FIELDS.map((field) => {
    const status = seoStatus(field, length);
    return `
      <div class="tc-seo-card ${stateClass(status.state)}">
        <div class="tc-seo-card__head">
          ${icon(status.icon, "icon")}
          <strong>${field.label}</strong>
        </div>
        <p class="tc-seo-card__count">${length} <small>/ ${field.min}–${field.max}</small></p>
        <p class="tc-seo-card__status">${status.label}</p>
        <p class="tc-seo-card__hint">${field.hint}</p>
      </div>`;
  }).join("");
}

async function pasteFromClipboard() {
  const textarea = root.querySelector("#tc-input");
  try {
    const text = await navigator.clipboard.readText();
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? textarea.value.length;
    textarea.setRangeText(text, start, end, "end");
    textarea.focus();
    updateAll();
  } catch {
    toast("Could not paste — clipboard access isn't available in this browser. Use Ctrl/Cmd+V instead.");
  }
}

function toast(message) {
  const region = document.querySelector("#toast-region");
  const element = document.createElement("div");
  element.className = "toast-message";
  element.textContent = message;
  region.append(element);
  setTimeout(() => element.remove(), 4200);
}

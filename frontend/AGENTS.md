# Frontend Agent Contract — The Yovth Clients Report

These instructions apply to every file under `frontend/`.

The goal is not to produce a merely clean dashboard. Build a distinctive, production-grade internal tool that unmistakably belongs to The Yovth, remains fast under dense real data, and avoids generic AI-generated UI patterns.

Normative words are intentional:

- **MUST / NEVER** — release blocker.
- **SHOULD** — follow unless the task gives a concrete reason not to.
- **MAY** — optional.

## 1. Read before designing or coding

Before changing UI:

1. Read the relevant product, metric, period, and UX sections in `../PLAN.md`.
2. Inspect the existing page in a browser and take a screenshot at the affected viewport.
3. Reuse established tokens, components, states, and vocabulary before adding anything.
4. Identify the user's single job in the affected view.
5. State the information hierarchy and interaction states in a compact implementation plan.
6. Implement with real representative content, not empty lorem-ipsum geometry.
7. Take after-screenshots, critique them, fix visible problems, then run the release checklist below.

Do not start by assembling a collection of familiar components. Start from the work the operator must complete.

## 2. Fixed aesthetic direction

The direction is **The Yovth operations control room**: editorial confidence, dark industrial precision, high information density, and one controlled orange signal. It is not a marketing landing page and not a generic SaaS admin template.

### Brand tokens

Use semantic CSS custom properties. These are the starting brand tokens:

```css
:root {
  color-scheme: dark;
  --color-canvas: #0e0e0e;
  --color-surface: #1e1e1e;
  --color-surface-raised: #242424;
  --color-ink: #ffffff;
  --color-ink-muted: #d7d7d7;
  --color-ink-subtle: #b9b9b9;
  --color-accent: #ff4a17;
  --color-border: rgb(255 255 255 / 14%);
  --color-border-strong: rgb(255 74 23 / 48%);
}
```

- Display/headings: **League Spartan**, usually 900 and uppercase.
- Body, controls, labels: **Archivo**, 400/700.
- Numeric comparisons: Archivo with `font-variant-numeric: tabular-nums`.
- Self-host fonts and the official logo. Do not hotlink production brand assets.
- Keep body copy at high contrast. `#747373` is decorative/disabled only, not small text.
- Use the orange `V`, a restrained line/grid motif, or one strong orange edge as the visual signature.
- Spend visual boldness once per view. Everything around the signature stays disciplined.

New colors, typefaces, shadows, radii, or spacing values MUST enter the token system first. Do not scatter unexplained literals through component CSS.

## 3. Anti-slop rules

### NEVER ship these defaults

- Purple/blue gradients, aurora blobs, glassmorphism, or glowing orbs.
- A giant centered “Welcome back”/hero section above operational data.
- Decorative metric charts that do not answer an operator question.
- Cards nested inside cards merely to fill space.
- A separate rounded container around every label, icon, and value.
- Excessive pills, capsule buttons, or arbitrary large border radii.
- Orange glow on every interactive element. Glow is reserved for the primary action or active progress.
- Generic fonts such as Inter, Roboto, Arial, or default system UI as the chosen design voice.
- Emoji as product icons. Use the approved Tabler SVG sprite and official service marks.
- Random `01 / 02 / 03` labels unless the content is genuinely sequential.
- Fake testimonials, fake activity, fabricated metrics, invented client logos, or placeholder “insights”.
- Generic copy such as “Unlock insights”, “Boost your performance”, “Everything in one place”, or “Seamless experience”.
- A hamburger menu on desktop when the available navigation fits visibly.
- Hover-only access to required information or actions.
- Animation added to compensate for weak hierarchy.

### Questions every new visual element must answer

- What real information or action does it carry?
- Why is this hierarchy correct for an agency operator?
- Why does it look like The Yovth rather than another SaaS dashboard?
- Does it still work with missing, stale, failed, and very long real data?

If an element has no strong answer, remove it.

## 4. Dashboard composition

- The first viewport MUST prioritize client state and period comparison, not branding spectacle.
- The global period control, exact date ranges, freshness, filters, and active jobs remain easy to scan.
- Client cards SHOULD feel close to square on desktop but MUST grow when content needs space.
- Mobile cards use natural height; never shrink text to preserve a square.
- Each provider summary shows no more than 3–4 approved KPIs.
- Each KPI exposes current, previous, delta, unit, and quality without forcing mental reconstruction.
- Exact period dates and data freshness are available without opening a separate report.
- Provider identity uses an icon plus a text label; never color/icon alone.
- Detail history belongs in a drawer/detail view, not compressed into the overview card.
- Keep comparison columns aligned across cards. Use tabular numbers and stable widths.
- Prefer dividers, alignment, type scale, and spacing over extra containers.
- Do not hide primary operational information behind carousels, accordions, or horizontal card scrolling.

## 5. Data truth is part of visual quality

- `null`, zero, stale, partial, truncated, unavailable, auth-required, and failed are distinct states.
- NEVER render missing data as `0`.
- NEVER assign positive/negative color from the arithmetic sign alone. Use each metric's `better_direction`.
- Never present a derived or truncated value as an exact provider total.
- Show last successful collection separately from the last failed attempt.
- Keep the latest valid value visible after refresh failure, with an explicit warning state.
- Current and previous values MUST name or expose their exact date ranges.
- Snapshot metrics and range metrics MUST not imply the same aggregation semantics.
- Use `Intl.NumberFormat` and `Intl.DateTimeFormat`; never hand-build localized output.
- Format one metric consistently everywhere, including precision, units, signs, and compact notation.
- Do not calculate business meaning in the DOM component when the API already returns it.

## 6. Required states

Every feature MUST be designed and implemented for:

- initial loading;
- fast loading without spinner flicker;
- empty/no clients;
- client with no runs yet;
- sparse provider configuration;
- fully populated dense data;
- stale data;
- partial/truncated data;
- provider authentication required;
- recoverable network error;
- provider/job failure;
- active sync;
- completed sync;
- cancelled sync;
- offline/reconnecting SSE.

Skeletons MUST mirror final content dimensions and hierarchy. Do not use arbitrary gray rectangles. Add a short display delay and minimum visibility window when needed to prevent flashing on fast responses.

Every empty or failed state gives a specific next action. Never end at “Something went wrong”.

## 7. Semantics and accessibility

- Use native HTML first: `<button>` for actions, `<a>` for navigation, `<label>` for controls, `<table>` for genuinely tabular data.
- NEVER make a clickable `<div>` or `<span>`.
- Every flow MUST work by keyboard alone.
- Every focusable element MUST have a clear `:focus-visible` style.
- NEVER remove outlines without an equal or stronger replacement.
- Modal/dialog focus MUST move in, remain trapped, and return to the opener on close.
- Include a skip link and hierarchical headings.
- Icon-only buttons require precise `aria-label`; decorative SVG is `aria-hidden="true"`.
- Toasts, validation, and job progress use an appropriate polite `aria-live` region.
- Status cannot rely on color alone: pair color with text and/or a meaningful icon.
- Pointer targets are at least 24 × 24 px; mobile targets are at least 44 × 44 px.
- Mobile input font size is at least 16 px.
- NEVER disable browser zoom.
- Charts require accessible text summaries and color-blind-safe distinctions.
- All informative images have useful alt text; decorative images use `alt=""`.
- Set explicit image width/height or aspect ratio to prevent layout shift.
- Test the accessibility tree, not only the painted pixels.

## 8. Forms and controls

- Every input has a visible label or an unambiguous accessible label.
- Labels and checkbox/radio rows form one generous hit target with no dead zones.
- Use correct `name`, `type`, `inputmode`, and `autocomplete` values.
- Do not block paste or keystrokes; validate and explain instead.
- Keep submit enabled until a request actually begins. Then disable it, keep its action label, and add a spinner.
- Put validation next to the relevant field and focus the first invalid field after submit.
- Warn before leaving a form with unsaved changes.
- Action copy says what will happen: `Запустити збір`, not `Продовжити`.
- Keep terminology consistent from button to progress stage to success message.
- Use an ellipsis character for loading/follow-up actions: `Завантаження…`, not `Завантаження...`.

## 9. Sync modal and progress UX

- The preflight view names client, providers, source mode, exact ranges, and estimated units when known.
- Progress is structured by provider and stage; a streaming log is secondary and collapsed by default.
- Determinate progress uses a real completed/total model. Do not animate a fake percentage.
- Closing the modal MUST NOT cancel the job.
- Reopening or refreshing MUST restore state from the API/SSE event history.
- Cancellation is explicit and described as cooperative.
- Partial completion keeps successful providers and offers `Повторити невдалі`.
- Auth-required errors link to a concrete recovery instruction.
- Completion refreshes only affected cards and preserves filters/scroll.
- The modal/drawer uses `overscroll-behavior: contain` and does not produce background scroll.

## 10. Motion

- Motion serves cause/effect, state transition, or one deliberate brand moment.
- Prefer CSS, then Web Animations API; avoid a motion library unless it solves a measured need.
- Animate only compositor-friendly `transform` and `opacity` in normal UI flows.
- NEVER use `transition: all`.
- NEVER animate layout properties such as width, height, top, or left for routine interaction.
- Motion is interruptible and responds to user input.
- Honor `prefers-reduced-motion` with an equivalent low/no-motion state.
- Avoid perpetual ambient animations around live data; they obscure actual running status.

## 11. Responsive layout and content resilience

- Use intrinsic CSS Grid/Flexbox layout, not JavaScript measurements.
- Verify at 390, 768, 1024, 1440, and ultra-wide viewport widths.
- Test 200% browser zoom and long client/provider names.
- Prevent accidental horizontal page scrolling; do not hide real overflow bugs with a global band-aid.
- Flex/grid text children that may truncate need `min-width: 0`.
- Choose deliberate wrapping, truncation, line clamping, or breaking for every variable text field.
- Respect `env(safe-area-inset-*)` for full-bleed mobile UI.
- Set `color-scheme: dark` and a matching `theme-color`.
- Explicitly color native selects and form controls for Windows dark mode.

## 12. Vanilla JS and CSS constraints

- Use semantic HTML, custom CSS, and small ES modules. Do not introduce React/Vue/Svelte.
- Bootstrap is a behavior/layout/accessibility foundation, not the visual identity.
- Tabler Icons are the default interface icon source. Use one icon system consistently.
- Chart.js is reserved for detail history where a chart improves comparison.
- Vendor and pin production frontend dependencies; no runtime CDN dependency.
- No inline event handlers or JavaScript URLs.
- Keep rendering functions small and state-driven. Do not mix API fetching, business normalization, and HTML string construction in one function.
- Escape untrusted text. Prefer DOM APIs for dynamic user/client content.
- Preserve filters, selected period, tabs, and open client detail in the URL when that state should survive refresh/share/back-forward.
- Abort obsolete fetches and prevent stale responses from overwriting a newer selection.
- One control has one responsibility; avoid hidden multi-action click areas.

## 13. Performance requirements

- Set an explicit performance budget before adding a dependency.
- Self-host and subset fonts; preload only critical faces.
- Preload above-fold critical imagery; lazy-load below-fold client logos.
- Batch DOM reads and writes; never alternate layout reads with mutations in a loop.
- Use event delegation where repeated cards otherwise create many listeners.
- For large client lists, use `content-visibility: auto`; virtualize only after measurement shows it is needed.
- Avoid rendering hidden detail histories for every card.
- Profile under CPU/network throttling, not only on a development workstation.
- Eliminate layout shift caused by fonts, logos, skeletons, and async card updates.

## 14. Copy rules

- UI copy is English. Ukrainian is used only for docs, code comments, and chat replies to the user — never for rendered UI strings.
- Code identifiers remain English.
- Write from the operator's point of view with active, plain verbs.
- A label labels; helper text explains; a button acts. Do not make one string do all three jobs.
- Errors state what happened, what data remains valid, and the next recovery action.
- Avoid apologies, hype, and personality filler in operational states.
- Keep provider and metric names exactly consistent across cards, filters, drawers, jobs, and errors.
- Protect brand/provider names and technical identifiers with `translate="no"` when browser translation could corrupt them.

## 15. Visual verification loop

No frontend task is complete after code compilation alone.

1. Render with representative normal, empty, loading, partial, stale, and error fixtures.
2. Capture desktop and mobile screenshots.
3. Compare hierarchy, density, palette, typography, and signature against the fixed direction above and [The Yovth site](https://theyovth.com/).
4. Check for AI-slop patterns listed in §3 and remove any that appeared.
5. Check overflow, alignment, focus, hover, active, disabled, and reduced-motion states.
6. Run automated accessibility checks, then complete keyboard-only manual testing.
7. Repeat screenshots after fixes. Do not claim visual completion without inspecting the rendered result.

## 16. Release checklist

Before handing off any UI change, verify:

- [ ] The main task is obvious within 5 seconds.
- [ ] The view unmistakably uses The Yovth's design language.
- [ ] No generic AI-slop pattern from §3 remains.
- [ ] The change uses the existing token and component vocabulary.
- [ ] Normal, empty, loading, dense, partial, stale, and error states work.
- [ ] `null` is not rendered as zero and status meaning is not color-only.
- [ ] Keyboard order, visible focus, dialog focus return, and screen-reader names work.
- [ ] Dates/numbers are locale-aware and comparison numbers are tabular.
- [ ] Mobile, tablet, laptop, desktop, ultra-wide, and 200% zoom were inspected.
- [ ] Reduced motion works and there is no `transition: all`.
- [ ] No avoidable layout shift, accidental overflow, or stale async update remains.
- [ ] Before/after screenshots were visually reviewed.
- [ ] Automated tests and the relevant E2E flow pass.

## 17. Sources and project-specific adaptation

This contract is an original project-specific adaptation informed by:

- [Anthropic frontend-design skill](https://github.com/anthropics/skills/tree/main/skills/frontend-design) — intentional, subject-grounded design; anti-template critique; screenshot iteration. Apache-2.0 source.
- [Vercel Web Interface Guidelines](https://github.com/vercel-labs/web-interface-guidelines) — production interaction, accessibility, state, motion, layout, and performance checks. MIT source.
- [WAI-ARIA Authoring Practices Guide](https://www.w3.org/WAI/ARIA/apg/patterns/) — keyboard and accessible interaction patterns.
- [The Yovth](https://theyovth.com/) — project brand source of truth.

When a generic source preference conflicts with this file, the project-specific rule in this file wins. When a task-specific user requirement conflicts with this file, the user requirement wins unless it would create an accessibility, security, or data-integrity failure; surface that conflict explicitly.

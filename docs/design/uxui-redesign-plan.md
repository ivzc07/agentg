# Coach dashboard UX/UI redesign - brief, research, plan

Status: **implemented** on `feat/UXUI` (commits `2e87d4a..6ec648e`); see §Phase 5 verification at the end.
This document is phases 1-3 of the redesign effort, kept as the design record.
Research method: full read of `src/agentg/dashboard_web.py` plus four parallel code/design audits and four Mobbin research sweeps (26 captured references).

---

## Phase 1 - Project brief

### Product and jobs

agentg is a chat-based gym coaching agent: Members train and log everything through Telegram, and the product's one visual surface is the **coach-facing web dashboard** ([docs/spec-dashboard.md](../spec-dashboard.md)).
The dashboard has three jobs, in priority order: **who needs me** (a roster sorted by Gap with severity colors and safety flags), **write a Member's Routine** (the weekday-to-exercises editor), and **one Member's page** (Sessions, last weights, Routine, Notes, flags).
Supporting flows: Presets (shared routines), tenant Settings (invite links + gym name), and the magic-link login door.

### Stack and constraints

- Server-rendered HTML built with Python f-strings inside `src/agentg/dashboard_web.py` (1951 lines) on aiohttp; no template engine in practice, no build step, no SPA, three tiny vanilla JS snippets (search filter, copy buttons, typed-confirm gate).
- Styling is three inline `<style>` constants (`ROSTER_STYLE`, `MEMBER_STYLE`, `EDITOR_STYLE`) plus a bare `_page()` shell and three stray inline styles.
- i18n: per-browser EN/ES via `dashboard_i18n.py` strings; Exercise names, Workout names, and Members' verbatim words never translate.
- No design tokens, no CSS custom properties, no dark mode, no media queries, no focus styles, no transitions.
- Tests assert markup fragments heavily (`tests/` unit + behavioral), so class/copy churn shows up in pytest.

### Screen inventory

| Screen | Route | Built from |
|---|---|---|
| Roster - Table | `GET /?view=table` | `_table_page`, `_chrome`, `_seg`, `_roster_row`, `_lapsed_section` |
| Roster - Cards | `GET /?view=cards` | `_cards_page`, `_member_card` (4-week day grid), severity bands |
| Roster - Split | `GET /?view=split` | `_split_page` (20rem rail + pane), `_split_placeholder` |
| Member page | `GET /members/{id}` | `_member_content`: header facts, `_safety_banner`, `_routine_card`, `_sessions_card`, `_weights_card`, `_notes_card` |
| Routine editor | `GET/POST /members/{id}/routine` | `_routine_editor_page`, `_editor_day` (weekday select + name input + exercises textarea), `_ownership_chip` |
| Presets index | `GET/POST /presets` + apply/default/retire | `_presets_page` |
| Preset editor | `GET/POST /presets/{id}/routine` | same editor, master variant |
| Settings | `GET /settings` + 3 POSTs | `_settings_page` via bare `_page`, `_qr_svg`, `_copy_button`, `_regenerate_form` |
| Door pages | `GET/POST /login/{token}`, bounce | `_interstitial_page`, `_bounce_page` (Spanish by design) |
| Shared 404 | any dead Member URL | `_not_found` (deliberately bare, keep) |

### Current design system, as found

Full audit values are in the effort's research notes; the shape of it:

- **Colors**: five grays for muted text (`#666`, `#5a6472`, `#8a94a3`, `#555`, `#333`), three hairline grays (`#e3e7ec`, `#eee`, `#ddd`), two reds for the same error role (`#b3261e` and inline `#b00`), amber `#9a5b00`, green `#1f7a4d`.
- **Type**: `system-ui` stack, eight ad-hoc sizes from 9px to 22.4px, line-height never set, two different h1 sizes and two h2 treatments.
- **Space and shape**: mixed rem/px with no rule, six border radii (3px / 6px / 8px / 10px / 0.5rem / 1rem), exactly one shadow, zero motion.
- **Three and a half divergent page shells**: roster pages get the full chrome, the Member page drops all chrome but the back link, the editor and Presets pages reference classes (`.muted`, `.card`, `a.back`) that their `<style>` does not include so they render unstyled, and Settings uses the bare 32rem `_page` skeleton with UA-default controls.
- **Dead classes**: `.tag-new`, `.chip`, `a.edit` are emitted but defined nowhere, so "new" is indistinguishable from "snoozed" and the ownership chip is just another gray tag.
- **Dark mode**: none anywhere.

### Pain points (concrete, in the code)

Hierarchy and scanability:

1. Table severity is color-only on the right-aligned away text (`_roster_row`, `ROSTER_STYLE` L276-277); no glyph, weight, or bar distinguishes amber from red, and the row's job (urgency) reads last.
2. The Member header collapses every fact into one gray `·`-joined line and drops the severity color entirely (`_member_content` L779-784).
3. Status tags render inside the `<h1>` (L786), so screen readers announce them as the page name.
4. Cards: an empty "hot" band vanishes instead of reading as good news (L500-502), and a no-Routine Member files under "on track" against the spec's grey-new rule (L497).
5. The safety flag, the loudest fact in the product, is the smallest chip on a card (L466-475).

Layout and mobile:

6. Zero media queries; Split's fixed 20rem rail leaves ~55px of pane at 375px, the header chrome wraps into a jumble, and long names overflow rows horizontally (nowrap tags, no ellipsis).
7. The split rail scrolls with the document and never highlights the open Member.
8. The day grid is 15px squares, 9px labels, date on hover-title only, pure red/green with no legend and no shape difference - unreadable on touch and under color blindness.

Affordances and states:

9. No hover, no focus-visible, no styled buttons (every control is UA default), nothing marks rows as clickable beyond the name underline.
10. No empty state for a zero-member gym, no "no matches" state for search (the count in the header never updates), no success feedback on any write, no double-submit protection (double-clicking Apply messages every Member twice).
11. The typed-confirm Regenerate button is hard-coded `disabled`, so without JS the form can never submit, contradicting its own docstring.

Editor:

12. One spare day block per round-trip: building a 4-day plan takes four saves, and each save sends the Member a partial-plan Telegram message (L1033, L1494-1507).
13. Removing a day is a three-field manual wipe with a refusal if any field is missed; there is no per-day remove affordance.
14. A stale-save (409) re-renders the **fresh** Routine and destroys everything the Coach typed (L1483-1485) - the one rejection path that loses work.
15. The catalog is a comma-joined blob in a closed `<details>`; errors are one red line above the form naming no day or line; the EN/ES toggle sits under Save and wipes the form via GET.

Navigation:

16. The editor journey strips `?view` (enter from Split, exit to standalone Table context) while tick-off carefully preserves it.
17. The Presets page fakes the roster chrome with Table stamped `aria-current` and a dead search box; the current language is never indicated; the toggle's position wanders across four layouts.

Accessibility baseline:

18. `#8a94a3` fails AA at 3.1:1 and is used at the smallest sizes; the attendance grid is empty `<i>` elements invisible to screen readers; the seg control is ~26px tall and the EN/ES toggle ~17px - both under the 44px mobile target; search has no label.

---

## Phase 2 - Mobbin research

### What Mobbin has, and does not have

Stated plainly: **Mobbin has no coach-facing fitness web product.**
TrueCoach, Trainerize, Everfit, FitBudd, and Hevy Coach are absent; Future exists only as its member-facing side.
Mobbin also returned nothing for four specific patterns: a safety/medical marker on list rows, an attendance day-grid inside list cards (nearest: Cursor's contribution grid, `mobbin.com/screens/0cfb4fdd`), a stale-conflict "refused, reload" save pattern, and a QR-plus-invite-link settings page.
Those four are designed from adjacent references below and labeled as such.
Everything else came back strong: rosters and dense lists (web), person-record pages (web), workout editors (iOS), settings/danger-zone, empty states, and magic-link doors.

### References

**Primary anchor - Linear (web).**
Grouped issue lists with colored status glyphs leading each row, section headers that double as collapse affordances with counts ("In Progress 5"), right-aligned grayscale metadata, ~36px rows, and an honest "1 issue hidden by filters - Clear" notice (`mobbin.com/screens/610d34b6`, `212fda35`).
Why: the roster and Linear's list share the same job - a prioritized queue you scan by severity and drill into - and Linear's dark, dense, color-only-for-state restraint is the closest shipping web analog to the visual language this product already chose (below).
Not to copy: command palette, keyboard-first view state, drag reorder, the right properties panel - all SPA machinery.

**Secondary - Hevy (iOS).**
Set-table anatomy (SET / KG / REPS column headers, per-exercise cards, full-width low-emphasis "+ Add Set" row, Cancel/Update explicit save) and day-grouped exercise history (`mobbin.com/screens/ace0878c`, `7e32b889`).
The model for the training record (Sessions, last weights) and the editor's day cards.
Not to copy: sheets, drag reorder, rest timers, member-side logging state.

**Secondary - Wise (web).**
Banner anatomy for the safety flag: icon + bold one-line claim + one supporting sentence + a single action button inside the banner, full-width above the H1 (`mobbin.com/screens/ede78e65`); AutoSend's literal "I Acknowledge" button verb (`5c629e4e`).
Not to copy: client-state persistence; ours renders from the flag record and acknowledges via plain form POST.

Pattern references used at component level (each cited where applied in Phase 3):

- **Attio** (web) - recency named in the toolbar ("Sorted by last visit"), severity as a colored value inside a quiet cell, relative time strings (`mobbin.com/screens/d8e127ef`).
- **Notion** (web) - view tabs as small text links directly above the content; chip formula = muted background + saturated text of the same hue, identical across views (`9aed23d0`).
- **Apple Fitness (iOS)** - weekday-as-card plan editor: each day owns its items and its own add affordance, one sticky save (`fc377805`).
- **7shifts** (web) - weekday chips as styled checkboxes, add-row + per-row remove anatomy, character budget on notes (`74754b08`).
- **Substack / Link (web)** - server-side validation display: red-bordered field + specific message beneath, page-level error banner slot for save-level failures (`286c847a`, `0ef97473`).
- **User Interviews / Basecamp (web)** - settings as card-per-concern with one obvious copy action and consequence copy in full sentences under the link (`31f948c0`, `81d8ba87`).
- **Hume AI + Resend (web)** - regenerate consequence copy in three short lines + typed-confirm gate with the token shown inline (`a2c629ef`, `5a77cd07`).
- **Steep / Going (web)** - first-run empty state with one primary action; "0 results matching your filter" + Clear inside the results area (`b026462f`, `71eb64b3`).
- **Linear login / Descript (web)** - magic-link door: one centered column, names the acting identity, single primary Continue (`6283263c`, `fc5c2dba`).
- **Cursor** (web) - contribution-grid anatomy (weekday rows, legend) as the closest attendance-grid precedent (`0cfb4fdd`).

### The directional call

The visual direction does not come from Mobbin at all, and saying otherwise would be invented truth: the owner already chose it.
`docs/prototypes/coach-dashboard-v3-dark.html` - pure black, white as the accent, zero radius, mono uppercase eyebrows, mint/coral state colors - was parked with "the look is wanted, the timing is not" ([spec §Out of scope](../spec-dashboard.md)), and `feat/UXUI` is that later effort.
Linear is the primary *Mobbin* anchor because it is the closest shipping validation of that same grammar on the web, applied to the same list-scan-drill job; Hevy and Wise fill the two surfaces v3-dark left thin (training-record anatomy, alert-banner anatomy).
Everything v3-dark could not answer (validation display, settings, empty states, door pages) is filled by the pattern references above.

Three v3-dark frictions are resolved consciously rather than inherited:

1. v3-dark carries v2's rejected one-roster IA; the skin is re-applied to the **settled three-view IA** (Table / Cards / Split, segmented control), which stays exactly as specced.
2. v3-dark's mint/coral semantics meet the settled amber-at-1 / red-at-3 ramp by keeping the thresholds and re-tuning the hues for black (amber and coral below); mint stays "Session happened".
3. v3-dark's ownership confirm dialog predates the settled no-dialog chip (#86) and is not adopted; the chip + consequence line stays.

---

## Phase 3 - Design plan

### Direction in one paragraph

One dark editorial language across every page: pure black ground, two flat surfaces, hairline rules instead of card chrome, zero corner radius except pill chips, white as the loudest accent (the safety flag is a white block), a mono-uppercase eyebrow as the one typographic signature, and color reserved for training state (mint kept / coral missed / amber slipping / purple extra).
Same IA, same routes, same copy semantics, same EN/ES rules; this is a re-skin plus state/affordance/a11y completion, not a rewrite.

### 1. Design tokens

One `BASE_STYLE` block with CSS custom properties, included by every page (door pages included).
Dark is the product's one look, per the owner's parked-but-wanted direction; a light variant is an optional follow-up, deliberately not built now.

Color (all pass WCAG AA on their stated ground):

```css
--bg: #000;            /* page ground */
--surface: #131313;    /* cards: day cards, done flag card */
--surface-2: #1b1c1e;  /* tiles, inputs, empty grid squares */
--line: #2a2b2d;       /* hairline rules */
--line-2: #3a3a3c;     /* outlined chips, input borders */
--ink: #fff;           /* primary text, 21:1 */
--ink-2: #9a9a9a;      /* secondary text, 7.5:1 */
--ink-3: #757579;      /* faint meta >= 12px, 4.6:1 (v3's #5e5e60 failed AA and is retired to decorative use only) */
--mint: #6ef3a5;       /* Session happened, 13:1 */
--mint-tint: #0f2a1c;
--coral: #f58060;      /* red severity (3+ missed planned days), and missed day cells, 8.1:1 */
--coral-tint: #2b1712;
--amber: #f2b84b;      /* amber severity (1-2 missed planned days), 12.7:1 */
--amber-tint: #2a2110;
--purple: #8b7cf6;     /* extra / unscheduled, 5.9:1 */
--purple-tint: #201e33;
--danger-on-white: #b3261e;  /* only inside white flag blocks */
```

Type:

```css
--font: 15px/1.45 ui-sans-serif, system-ui, -apple-system, "Helvetica Neue", sans-serif;
--mono: ui-monospace, "SF Mono", "Roboto Mono", Menlo, Consolas, monospace;
/* scale (px): 11 eyebrow · 12 fine meta · 13 secondary · 15 body · 17 row/card titles · 20 section h2 · 22 page titles · 33 hero */
/* weights: 400 body · 600 titles/buttons · 700 hero+section */
/* headings letter-spacing -0.02em; eyebrows +0.14em uppercase 11px mono; chips 10px mono uppercase +0.12em */
```

Space, shape, elevation, motion:

```css
--gut: 16px;           /* spacing scale: 4 / 8 / 12 / 16 / 24 / 34 / 48 */
/* radius: 0 everywhere; 999px pills only (chips, filter pills) */
/* shadows: none - elevation is surface color + hairline */
--t-fast: 120ms ease-out;   /* background/outline hover-focus transitions only; no transforms, no keyframes */
```

Interaction tokens:

```css
:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
/* control heights: 44px minimum buttons/inputs, 55px primary CTA, 46px sticky nav, >=52px roster rows */
```

### 2. Component-level changes

Each change names its reference; "(gap)" marks patterns Mobbin had no example for, designed from the adjacent reference named.

- **Page shell (new, all pages)**: one document skeleton with `BASE_STYLE`, a sticky 46px top nav (gym name, seg control on roster pages, search, Presets / Settings / EN-ES with real active states) [v3-dark nav; Notion view-tab placement; fixes pains 17, 22, 23].
  The Member page keeps its spec-required switcher-hiding but regains the nav (gym name, Presets, Settings, language).
- **Roster row**: 52px initials tile, 17px name line with chips, 13px meta line where severity lives as colored text ("3 días planificados perdidos" in coral/amber) plus the away text, whole row a block-level link with hover surface and `aria-current` when open in Split [v3-dark row; Linear leading-state anatomy; Attio recency cell; fixes pains 1, 7, 9].
  Severity is never color-only: the coral/amber meta sentence carries the count in words.
- **Chips**: one chip system - mono uppercase 10px; `SAFETY FLAG` = white block (loudest thing in the system), `NEW` = outlined, `SNOOZED`/`LAPSED` = surface gray, ownership chip = outlined with named variant [v3-dark flagchip/newchip; defines the dead `.tag-new`/`.chip` classes; fixes pains 3, 5 partially].
- **Attendance day grid**: 18px cells, mint **filled** = Session, coral **hollow ring** = scheduled and missed, dashed = future - shape difference makes it colorblind-safe; weekday initials 11px `--ink-2`; a one-line legend under the grid; each cell gets `<span class="sr">` date text for screen readers [(gap) designed from v3-dark strip + Cursor contribution-grid anatomy; fixes pains 8, 18].
- **Safety flag banner**: white block above the columns - eyebrow `SAFETY FLAG`, the Member's words at 17px/600, date, and one black "Tick off" button inside the banner; acknowledged state decays to `--surface` with "seen by {coach} · {date}"; expired keeps the settled "expired, never seen" label [v3-dark flagcard; Wise banner anatomy; AutoSend acknowledge verb].
- **Buttons and forms**: primary = solid white on black 55px, secondary = outlined 44px, destructive-inside-white = `--danger-on-white`; inputs 44px on `--surface-2` with `--line-2` border and visible `<label>` elements everywhere (search included) [v3-dark buttons; fixes pains 9, 11 styling half, 18].
- **Error display**: page-level error banner slot (coral-tint surface, coral text) at the top of forms for save-level failures, retained field content [Link/Substack validation pattern; (gap) the stale-conflict wording keeps the existing STRINGS copy].
- **Empty states**: one `emptystate` component (22px title, one `--ink-2` sentence, at most one action) used by: zero-member roster (points at Settings' invite link), zero search matches ("0 de N" + clear guidance, rendered by the existing search JS), Split's pick-a-member pane (with live counts), empty bands [Steep first-run; Going filtered-zero; v3-dark emptyPane; fixes pain 10 empty-state half].
- **Segmented control and language toggle**: seg becomes 44px-tall pill tabs (white pill = active, unchanged `aria-current`); EN/ES becomes two 44px chips with the active language filled, fixed position in the nav on every page [Notion view tabs; fixes pains 17, 18 target sizes].

### 3. Screen-level changes

- **Table**: gray bordered `<ul>` rows become full-width v3 rows under a countbar ("N members · sorted by gap" - the sort finally named [Attio]); lapsed tail keeps `details/summary` styled as a quiet row; search-empty state added.
- **Cards**: bands keep the settled severity reading but all three headers always render, an empty band saying "0" reads as good news [Linear section counts; fixes pain 4a]; no-Routine Members move out of "on track" into a fourth quiet "Nuevos - sin rutina" group per the spec's grey-new rule [fixes pain 4b]; cards get tile + name + severity meta + the new 18px day grid + legend.
- **Split**: rail becomes independently scrollable (own `overflow-y`) with the open Member's row highlighted; below 900px the split collapses to rail-or-pane with a back row, so the view finally works on a phone [v3-dark frame; fixes pains 6, 7].
- **Member page**: hero (eyebrow `MEMBER`, 33px name, tags moved out of the `<h1>` into a chip row, meta line that keeps the severity color); white flag banner; the settled two-column body order is unchanged (Routine + Sessions left, weights + Notes right); Routine day-cards get the v3 identity/divider/footer anatomy with per-exercise "last logged" lines [Hevy exercise history]; Sessions become `details` rows (newest open) with set lines and coral verbatim quotes; pagination gains a `#sessions` anchor so paging stops teleporting to the page top.
- **Routine editor**: day blocks become day cards (eyebrow weekday, labeled fields, monospace exercises textarea kept) [Apple Fitness day-as-card; Hevy card anatomy]; help text and the `squat, 4, 8-10` format hint move **above** the blocks; the catalog `details` renders as a chip cloud instead of a comma blob; error banner at top; the ownership chip + consequence line stay exactly as specced; back link keeps `?view`.
- **Presets**: gets the real nav (no fake Table state, no dead search box); preset cards on `--surface` with the apply form's member checkboxes as a chip grid [7shifts weekday chips]; invalid `<form>`-in-`<p>` markup fixed.
- **Settings**: same shell as the rest of the product at last; card-per-concern (invite / coach link / name) [User Interviews]; QR constrained and labeled; `<code>` URLs wrap; regenerate keeps the typed confirm with the word shown inline as a chip and consequence copy in short lines [Resend token display; Hume consequence copy]; the JS-disabled submit gets a no-JS fallback (button enabled, server-side check remains the load-bearing gate - fixes pain 11); copy-button feedback reverts after 2s.
- **Door pages**: bounce and interstitial join the dark shell - one centered column, one 55px white button, Spanish as designed [Linear/Descript door anatomy].
- **Shared 404**: stays deliberately bare (spec-load-bearing indistinguishability); untouched.

Every screen ships its loading (server-rendered navigation - n/a beyond POST double-submit), empty, error, and long-content state: names truncate with ellipsis instead of overflowing, long Routines/catalogs scroll inside their blocks, and every form re-render preserves typed content where it already does today.

### 4. Migration order

Tokens first, then primitives, then composites, then screens; small commits, pytest after each.

1. **Tokens + shell** (1 commit): `BASE_STYLE` custom properties, the one document skeleton, focus-visible, type scale; all pages switched to it with minimal per-page CSS kept temporarily.
   *High churn*: many tests assert markup fragments; expect and fix string-assertion fallout here, nowhere else.
2. **Primitives** (1-2 commits): nav/chrome, row, chip system (defining `tag-new`/`chip`/`edit`), buttons/inputs/labels, banner, empty state, day-grid cells.
3. **Roster screens** (1 commit): Table, Cards (band fixes), Split (scroll + highlight + responsive collapse).
   *Risky*: the Split breakpoint is the one layout-behavior change; verify at 375/768/1280px.
4. **Member page** (1 commit): hero, flag banner, day cards, sessions, anchor pagination.
5. **Editor + Presets** (1 commit): day cards, error banner, catalog chips, Presets chrome/markup fixes.
6. **Settings + door pages** (1 commit): cards, QR, typed-confirm no-JS fallback, copy revert.
7. **Verify** (Phase 5): build, ruff, pytest, screenshots of every screen at mobile + desktop widths, self-review against the references.

### Flagged: real improvements that change behavior, so they need a separate yes

Per the task rules these are **not** in the cosmetic scope above; each is small but alters behavior or specced copy.

1. **Stale-save keeps the Coach's typed form** beside the fresh version instead of destroying it (pain 14) - contradicts the current "the fresh version replaces them" comment; strongly recommended, needs your yes.
2. **Editor renders a spare block per unused weekday** instead of one, so a 4-day plan is one save and one Member notification instead of four (pain 12) - parser already ignores blank blocks; approved 2026-07-31 with the branch.
3. **Retire preset gets a confirm** (currently one silent click next to Apply, while regenerating a link has a typed ceremony) - approved 2026-07-31 with the branch.
4. **POST double-submit protection** (disable-on-submit one-liner in the shared shell) - prevents double Apply messaging every Member twice; borderline cosmetic, included in scope unless you object.
5. **Editor journey preserves `?view`** (pain 16) - navigation consistency with tick-off's existing behavior; borderline, included unless you object.

### Out of scope, unchanged

The three-view switcher, severity thresholds, day-grid shape, ownership chip semantics, typed confirms, EN/ES rules, the bare 404, all routes, all handler logic, all STRINGS copy (except where a new state needs a new string, added to both languages), and everything spec-dashboard §Out of scope already parks.

---

## Phase 5 - Verification record (2026-07-31)

### What ran

- Full suite: **1127 passed, 2 skipped**; `mypy` clean on `dashboard_web.py` and `dashboard_i18n.py` (the file's eight pre-existing errors were fixed en route).
- Three test contracts were updated to the approved behavior changes: the editor link/redirect carrying `?view`, and the stale-save keeping the typed form (the new test also asserts the stamp re-arms on the fresh Routine).
- A seeded demo server (Iron Temple fixtures mirroring the prototype world) served the real app; every screen was screenshotted at desktop width and at 375px (same-origin 375px iframes, since the OS window would not shrink below ~500px - the media queries evaluate per-iframe viewport, so this is a faithful check).
- Live interactive checks: search filtering with accent folding, the zero-match state, the lapsed tail auto-expanding on a match and never being slammed shut; the stale-save flow end-to-end (409, typed work kept, fresh version shown, stamp re-armed); EN/ES toggle with the active language marked; the Spanish bounce page.
- An adversarial review workflow (four lenses, every claim independently refuted-or-confirmed) found **8 unique real defects**, all fixed in `6ec648e`: submit-guard vs cancelled confirm, bfcache re-arm, `[hidden]` losing to `.mcard{display:flex}`, the split rail's 47px sticky top vs a wrapping chrome, an invisible focus ring on the white banner, `--ink-3` at 4.05:1 on surfaces, two missing CSS rules, unlabeled nav landmarks.
Two review claims were refuted and not acted on (the editor chip in its h1 is the settled ownership-chip placement; the day grid's future/plain squares are decorative, not state-bearing - state is mint fill vs coral ring, which passes non-text contrast).

### Where the result matches the references

Linear's row anatomy and grouped sections with counts (roster rows, bands, lapsed tail), Attio's named sort ("Ordenado por días sin venir"), the v3-dark grammar throughout (black, white accent, zero radius, mono eyebrows, white safety block), Hevy/Apple Fitness day-card anatomy (routine cards and the editor's labeled day blocks), Wise's banner anatomy (flag banner: claim, date, one action inside the block), Resend/Hume's typed-confirm shape, Steep/Going empty states, Notion's chip consistency across views.

### Deliberate divergences

- Severity hues are amber `#f2b84b` / coral `#f58060` rather than v3's single coral, because the settled ramp has two steps; mint stays "Session happened".
- v3's ownership confirm dialog and single-roster IA were not adopted (both overruled by settled decisions).
- The row severity cue is a text sentence ("N días planificados sin sesión") rather than v3's strip-on-every-row - the Table stays denser and the words carry the count colorblind-safely.

### Left undone, knowingly

- Routine day-cards carry no per-exercise "last logged" lines (plan §3): needs per-Member exercise history in `member_page`, out of the store's scope this pass.
- Sessions stay flat rows, not `details` collapses - revisit with the htmx interaction cluster (ADR 0003).
- The zero-match search state is a fixed sentence, not "0 de N", and the header count never updates live - same cluster.
- No per-day remove affordance in the editor (pain 13) - the clear-the-block gesture stands for now.

- The Member hero's away text carries no severity color: `MemberPage` does not expose `missed_days`, and the store was out of scope. Follow-up: add it to the `member_page` query and color the fact.
- The Split placeholder shows no live counts (v3's emptyPane had them) - same reason, kept simple.
- No light theme: dark is the product's one look per the owner's call; a light variant is an optional follow-up.
- v3's adherence rings and month calendar belong to v2's rejected IA and were not built.
- True OS-window 375px screenshots were not capturable on this machine; the iframe technique above stands in.

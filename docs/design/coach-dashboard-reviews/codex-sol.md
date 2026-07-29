# Verdict

Ship one responsive roster, not three Coach-selectable views: a compact list on a phone that becomes a persistent master-detail rail on a tablet.
Keep Gap as the v1 sort key because it is the only honest Gym-wide signal available, but stop turning it into invented states such as "Slipping" and "On track."
Make the Member page a decision surface for rewriting a Routine: safety and active constraints first, current Routine second, recent performed Sets third.
The current prototypes are tidy desktop mockups, but they spend too much space explaining the data model and too little on the Coach's next glance and next tap.

# What is wrong now

1. **The three-view switch makes the Coach configure the tool before using it.**

   Table, Cards, and Split are not three jobs. They are three layout strategies for the same job. On a gym floor, a Coach should not have to remember which representation exposes which detail, or switch modes when moving from phone to tablet. The choice also destroys spatial memory: the same Member moves from a row, to a card in a band, to a rail item.

   Split is the useful interaction model on a tablet, but it is not a view. It is the responsive tablet form of the roster. Table is the useful scanning model, but it needs to collapse into a list on a phone. Cards do not earn a place.

2. **The card grid claims to show missed weekdays, but it cannot.**

   A blank square means at least three different things: no Workout was scheduled, a Workout was scheduled and missed, or a Session happened outside the displayed data. The grid shows Sessions only, so it cannot support the stated conclusion about which Routine weekday was missed.

   Twenty-eight 15 px squares are also poor floor-side information. They demand careful decoding, rely heavily on color, have targets far below comfortable touch size, and make nine Members much taller than the equivalent list. Four weeks is an arbitrary window: long enough to add visual noise, too short to explain a durable training change, and not tied to when the active Routine began.

3. **The Member page makes the Coach assemble the rewrite context by scanning four distant cards.**

   The current order is current Routine, several Sessions, last weight per Exercise, then Notes. The most consequential facts for a rewrite, the safety Note, injuries, constraints, and goal, are split between a banner and the bottom of the right column. On a phone, the Coach may scroll through the entire Routine and Session history before reaching them.

   The two-column grouping also creates a false distinction between "training" on the left and "context" on the right. A Coach deciding whether to keep Back squat needs its current prescription, recent performed Sets, and relevant injury Note together in the same reading sequence.

4. **"Last weight per Exercise" looks precise but is weak evidence.**

   A weight without reps, Set count, effort, or the previous comparable Session does not tell the Coach whether it was successful, abandoned, or a warm-up. It duplicates facts already present in Sessions while stripping away the context needed to interpret them.

   The interface must not imply a Weight suggestion. Show what the Member performed, with dates and reps, and let the Coach judge it.

5. **Gap is being asked to mean more than it knows.**

   Gap is a clean, derived fact and a defensible v1 ordering. It is not a verdict on whether a Member is "on track." A three-day Gap may be normal for a two-day Routine, while a two-day Gap may include a missed scheduled Workout. The red, amber, and green treatment turns elapsed time into a coaching judgment the system has not derived.

   The roster title "Who needs me" intensifies that overclaim. The first row is simply the Member with the longest Gap, not necessarily the Member requiring the most urgent coaching decision. The open safety Note must remain visible without changing that order, as required.

6. **The screen does not actually adapt to a phone or a modest tablet.**

   Neither HTML file contains responsive rules. The 320 px Split rail and the two-column Member layout remain in place at narrow widths. The top bar also carries the Gym identity, three view controls, language controls, and Coach identity. At floor-side widths this will squeeze, wrap, or overflow before the Coach reaches Member information.

7. **The visual hierarchy repeats facts and spends height on low-value summaries.**

   Gap appears in the Member subtitle, a stat, and a colored callout. The roster header spends prime space on totals such as "Training 9" and "Gap >= 7d 2" even though the rows immediately answer those questions. Repeated card chrome, uppercase headings, chips, footnotes, and prototype explanations make a small dataset feel busy.

8. **Several interactions are visually clickable but not robust controls.**

   Whole table rows and Split rail `div` elements are driven by click handlers rather than links or buttons. That weakens keyboard access, focus visibility, open-in-new-tab behavior, and assistive technology naming. The 15 px day cells encode state mainly by color. Tiny muted dates and language tags are likely to be hard to read under gym lighting.

9. **The EN/ES prototype exposes implementation rationale inside the product.**

   The footnote about what was not translated belongs in a product decision record, not on every roster and Member screen. A Coach should not have to read an explanation of `exercises.name` or aliases while checking Marta between Sets.

   `EN · textual` is also system language rather than Coach language. It identifies a mismatch without explaining that the words are preserved from the Member. Repeating it inside many short lines adds clutter.

# Your proposed design, screen by screen, concrete enough to build

## Roster

Build one roster component with one ordering: descending Gap. Do not expose a Table/Cards/Split control.

On a phone, it is a full-width list. On a tablet, the same list becomes a 300 to 340 px sticky rail and the selected Member opens beside it. At widths where the Member pane cannot remain at least about 600 px, selecting a Member navigates to a full-width Member page with a clear back control. This is responsive behavior, not a saved Coach preference.

Keep the top area to one compact sticky row:

- Gym name and the title "Members"
- total Member count in muted text
- a search field or search button, because finding a known Member is a floor-side task
- Coach menu, with language inside it

Do not show aggregate "training," stale, or safety totals. They are not actions and they consume the first viewport.

Each Member row should be a real link with a minimum 48 px height. Its scanning order should be:

1. Member name, left aligned and visually strongest
2. Gap, right aligned with tabular numerals, written as "12d" in English and "12 d" or the localized compact equivalent in Spanish
3. Last Session underneath: absolute short date and Workout name, for example "11 Jul · Pull"
4. Markers underneath only when present

Show an open safety marker and a "New" marker. Do not show acknowledged safety Notes in the roster. They no longer need floor-side attention and the Member page retains the history. Markers never alter the Gap order.

Use restrained emphasis for Gap. Bold the first few longest values or use one neutral accent, but do not use green to declare success. If thresholds are retained for visual grouping, label them literally, such as "7+ days," and treat them as sticky separators within the one list. Do not use judgmental bands.

Add one compact secondary fact only if testing shows it helps: "6 Sessions / 28d." It reports actual Sessions without pretending to know whether scheduled Workouts were missed. Do not add the 4-week day grid to the roster.

When no Member is selected in tablet layout, use the right pane for a quiet instruction, not stale counts: "Select a Member to review training." Preserve the selected row when the Coach returns from the Member pane.

## Member page

The Member page should have one DOM reading order that works on a phone. A tablet may place adjacent sections side by side, but must not change their semantic order.

### 1. Compact identity header

Show:

- back to Members on a phone
- Member name
- "41 Sessions · Member since Dec 2024"
- "Last Session 11 Jul · 12d Gap"

State Gap once. Do not repeat it as a standalone colored callout.

Keep the header sticky only if it remains under roughly 64 px. On a tablet rail, the Member name can remain at the top of the detail pane while the rail stays independently scrollable.

### 2. Safety and rewrite context

An open safety Note comes immediately below the identity header in a high-contrast banner. Show the Member's exact words, when it was captured, and an explicit button labeled "Acknowledge safety Note." Do not use "Tick off," which can sound like resolving the injury rather than recording that a Coach reviewed the Note.

Because this action sets `acknowledged_at`, require one short confirmation that explains the consequence: the Note remains in the Member record but no longer appears as open. Use a 44 px minimum target and return focus to the banner after completion.

Below the banner, show a compact "Coach context" section containing active Notes in this order:

1. safety
2. injury
3. constraint
4. goal
5. preference

Show the Note kind, exact Member words, and date. Keep retired Notes behind a disclosure labeled "Retired Notes (1)." Dimmed content in the main list is easy to mistake for current context and adds risk during a quick rewrite.

### 3. Current Routine and the write entry point

Place the active Routine next, because it is the object the Coach is about to replace. Its header should say who authored it and when it became active.

Use a primary button labeled "Write new Routine," not "Edit." A Coach-authored Routine replaces the active Routine while the prior Routine is kept and deactivated. The label should match that model and prevent an expectation of silent in-place mutation.

Keep the Routine compact:

- one row per scheduled weekday
- Workout name beside the weekday
- Exercise name followed by Sets and reps in aligned columns
- no card within card styling

On a phone, make each Workout row collapsible after the first so the Coach can compare weekdays without scrolling through every Exercise. Start all rows expanded when there are three or fewer Workouts, then test this with real Routine sizes.

The Routine-writing flow should open from this button with the current Routine copied as an editable starting point. Before saving, state that saving activates the new Coach-authored Routine and keeps the previous Routine in history. This remains one job, writing the Member's Routine, rather than introducing a separate settings surface.

### 4. Recent performance evidence

Replace "Last weight per Exercise" plus the long prose Session lines with one "Recent Sessions" section.

Show the latest three Sessions expanded and older Sessions behind "Show 3 more." Each Session starts with date, reported Workout name, and whether it contained Sets. Under it, use aligned Exercise rows:

- Exercise
- performed Sets as weight x reps
- any volunteered effort or comment

For an Exercise in the current Routine, optionally show the previous comparable performed Set directly underneath in muted text. This creates a useful comparison without inventing a Weight suggestion. Never collapse a Session with no Sets into "nothing"; keep the explicit "Session recorded, no Sets."

On a tablet, "Coach context" can occupy a narrow right column beside the current Routine and recent evidence. On a phone, the order must remain context, Routine, recent Sessions. The current prototype's Routine and Sessions left, weights and Notes right arrangement should not survive.

### 5. Remaining Notes and history

If all active Notes already appear in Coach context, do not repeat them later. Retired Notes and prior Routines can live behind disclosures at the bottom. Chat history and Compaction summaries remain absent.

## EN/ES behavior

Make language a per-Coach preference. Initialize it from the browser language on first use, then persist the Coach's choice. It must not be a Gym-wide setting because Coaches at the same Gym may prefer different languages. It must not change the Agent's language with Members.

Translate all system-owned interface text:

- navigation, buttons, headings, empty states, Note-kind labels, and accessible names
- weekdays and month names
- relative time and plural forms
- decimal and date formatting
- `html lang`, focus announcements, and confirmation copy

Do not translate Member-authored words in Notes or volunteered Set comments. Preserve them exactly. When their language differs from the Coach interface, attach one localized badge to the whole text block, such as `EN · original` in Spanish and `ES · original` in English. Give the badge an accessible label such as "Original Member text in English." Do not tag every fragment in a line.

Workout names are authored free text, so preserve them. A mismatched Workout name does not need a badge in every roster row. If clarification is needed, tag it once where the Routine is displayed.

For v1, preserve the Catalog Exercise name because it is the record's canonical display value. Do not scatter language badges beside every Exercise. Longer term, add localized display names to the Catalog rather than treating the current schema as a permanent UX rule. Aliases should remain matching data, not presentation data.

Format Spanish performed Sets so decimals and rep sequences cannot be confused. For example: `12,5 kg x 12 / 12 / 10`, not `12,5 kg x 12,12,10`.

Remove the translation footnote from both screens. Document these rules for the team and expose only the small original-language badges the Coach needs.

# What you would cut

- The Table, Cards, and Split segmented control
- The Cards view in full
- The 4-week day grid
- "Needs you now," "Slipping," and "On track" bands
- Green Gap styling that implies a Member is on track
- roster summary stats
- the repeated Gap callout on the Member page
- acknowledged safety markers on the roster
- the standalone "Last weight per Exercise" card
- repeated active Notes below the rewrite context
- retired Notes in the default scan
- language controls in the primary top bar
- the untranslated-content footnote on every screen
- developer-facing phrases such as "markers do not re-sort"
- non-semantic clickable rows and tiny color-only state cells

# What you disagree with in the brief above

I disagree that three switchable roster views should be a product feature. The useful parts of Table and Split describe responsive states of one interface, while Cards adds interpretation and scanning cost without adding a job.

I disagree that a Sessions-only day grid can show which Routine weekday is being missed. It would need a trustworthy historical schedule, an explicit distinction between scheduled Workouts and actual Sessions, and a clear treatment of deviations. Without those, the grid overstates what the data says.

I only partly accept Gap as the roster's spine. It is the best honest v1 sort key under the available model, and the roster should remain sorted by it. It should not be presented as a complete measure of need or converted into "on track" status.

I disagree that current storage constraints should settle the Exercise-language design permanently. Canonical Catalog names can remain untranslated in v1, but a multilingual product should eventually support localized display names without misusing aliases.

Finally, I disagree with placing the Routine editor entry point beside an under-contextualized Routine and calling it "Edit." The entry point belongs on the Member page, but only after safety and active Notes are visible, and it should say "Write new Routine" so the Coach understands that a new active Routine will replace the old one.

# Verdict

Build one roster: a touch-friendly, Gap-sorted queue with a compact “needs attention” reason on each Member row. Keep the Member page as a focused, single-column work surface with an immediately visible Routine editor action, recent Sessions, and safety Notes before historical detail. Drop the three-way view switch and the four-week card grid: they make a Coach choose a presentation when they need to choose a person.

# What is wrong now

1. The three roster views are three products competing for one job. Table is the only honest queue, cards are a decorative re-encoding of the same Gap order, and split permanently spends half the screen on a list after a Member has already been chosen. A Coach on a phone or tablet pays in taps, horizontal constraints, and lost place. A saved URL containing a view also makes the product state depend on a presentation choice that has no coaching meaning.

2. “Who needs me” is sorted by Gap, but the UI does not explain what action the Coach can take next. A 12-day Gap and a 1-day Gap are not equally urgent, and a safety Note is important without being allowed to reorder the roster. Keep the prescribed Gap ordering, but give each row a reason line: “12 days since last Session”, “new Member”, or “open safety Note”. This makes the sort legible without inventing a second ranking. Put the open safety count in the page header, not in a competing colored heat system.

3. The red/orange Gap heat and safety marker imply an urgency scale the product does not define. Gap is derived and useful as a queue spine, but color must not pretend to know why a Member has not trained or what the Coach should do. Use text first, restrained color second, and a distinct safety icon or label for the safety Note.

4. The four-week day grid is too small to answer the useful question on a card. It shows that a Session landed on a date, but not whether it was the expected Workout, whether it had Sets, or which weekday matters for the active Routine. Tiny squares are poor touch targets and poor evidence. On a gym floor, the Coach needs the last Session, the active Routine's next Workout, and a readable history of recent Sessions. The grid adds visual decoding work without changing the decision.

5. The roster header spends valuable space on “Training”, “Gap >= 7d”, and “Open flags” as dashboard statistics. Nine Members is a small queue, not an analytics surface. “Gap >= 7d” is a hidden policy threshold that may be useful as a count but does not deserve equal visual weight to the queue. The first viewport should contain more Members and their reasons.

6. The Member page repeats Gap in the header and as a large colored line, while the most consequential action, rewriting the Routine, is one button inside a lower card. That order is backwards for the Coach who opened Marta to change her Routine. The page should first establish identity, safety context, and the current Routine, then support an intentional edit. Gap remains visible, but does not occupy two separate blocks.

7. Two columns are reasonable on a wide monitor, but not as the information model. On a tablet they force narrow cards; on a phone they become a long, ambiguous sequence. Routine plus Sessions on the left and weights plus Notes on the right also separates a Routine from the Notes that may constrain its rewrite. Safety Note and active Notes should be in the decision path before the edit action.

8. The prototype shows the last weight per Exercise as a flat lookup list. It is useful during a rewrite only when paired with the relevant Exercise in each Workout. Detached weights make the Coach scan between columns and can be mistaken for prescribed values. Label them explicitly as “last logged Set” and show them inline beside each Routine Exercise, with date. This preserves the distinction from a Routine and from a Weight suggestion.

9. Sessions are a useful audit trail, but the current treatment makes six lifts and comments into a dense text block. Show a compact recent list first, one row per Session with date, Workout if known, Set count, and a short Member-provided comment. Expand a Session on tap. A Session with no Sets must remain explicit as “Session logged, no Sets”, since it still counts as a visit.

10. Retired Notes are dimmed but remain in the main scan. That is honest history, but it competes with active safety, injury, goal, and constraint Notes. Show active Notes by default and put retired Notes behind an explicit “Show retired Notes” disclosure. Do not remove the ability to inspect them.

11. The language prototype puts a lengthy explanatory footnote on every screen. That is documentation leaking into the work surface. A Coach should see a small “Original text” tag beside untranslated Member words, with a help tooltip or one-time explanation. Exercise names and free-text Workout names stay unchanged; their provenance should be clear without a paragraph at the bottom.

# Your proposed design, screen by screen, concrete enough to build

## Roster

Use one responsive list, always ordered by descending derived Gap. Do not offer Table, Cards, or Split as product controls. On wide screens the list can use columns; on phone and tablet it becomes stacked rows without changing the order or content.

The header is compact: Gym name, “Who needs me”, open safety Note count, and the Coach identity. Remove the three large stat tiles. A small filter control may filter “all”, “Gap 7+ days”, “new Members”, and “open safety Notes”, but filtering must never alter the underlying sort. Persist only the filter if product research proves it useful; the default is all Members.

Each row is a single large tap target, at least 48px tall, with this scan order:

- Member name and a small “new” label when applicable.
- “12 days since last Session” or “No Session yet”, plus the last Session date and Workout when known.
- A compact reason chip for an open safety Note, with a non-color icon. An acknowledged safety Note can show “safety Note acknowledged” only as secondary detail.
- A chevron and no separate tiny click target.

The Gap number is the primary numeric value, but pair it with words. For zero, say “Today”; for one, “1 day”; for an unknown last Session, say “No Session yet”, not an artificial Gap. Keep the roster sorted with Members lacking a Session according to the product’s explicit rule, and make that rule visible in the implementation contract before building. Gap remains derived, never stored.

Do not use band headings such as “Needs you now”, “Slipping”, and “On track”. They imply classifications beyond Gap and create false stopping points. A sorted queue lets the Coach scan from the top and stop when the current floor work is done.

For the wide tablet layout, use a readable four-column grid: Member, Gap and last Session, current Routine/next Workout if available, and markers. Keep the first two columns sticky only if the list becomes long. Never require horizontal scrolling to reach a safety Note. On a phone, hide the next Workout only when space requires it, but keep it available as a second line or detail disclosure.

Selecting a row opens the Member page. On tablet, retain a simple back affordance and restore scroll position. Do not use Split as a permanent pane: it reduces the Member page to a narrow reading column and makes accidental row changes likely while standing or carrying a device.

## Member page

Make the top of the page a compact identity bar: Member name, member-since, total Sessions, last Session, and Gap in plain text. Add a back link that returns to the roster at the same scroll position. Do not repeat Gap in a second banner.

If an unacknowledged safety Note exists, show it directly beneath the identity bar as a high-contrast but not alarmist callout: safety label, the original Note text, date, and “Acknowledge” (the prototype calls this “Tick off”). Acknowledge must be explicit and reversible if the domain supports it; if it is not reversible, show the consequence before committing. The callout must not move the Member in the roster.

Put an “Edit Routine” button in the page title row and repeat it as the clear primary action on the Routine card only if testing shows the title-row action is missed. The entry point belongs on the Member page because this is the one write operation, but it must be visible before history. Say “Edit Routine”, not merely “Edit”, so the action is unambiguous.

Show the active Routine next. Display its author status, active-since date, and Workouts grouped by weekday. Each Workout lists Exercises and prescription text. Beside each Exercise, optionally show “last logged Set: 60 kg x 8, 18 Jul” in subdued text. Never label that value as a target or Weight suggestion. Keep the Routine card readable when the Coach is about to replace it; do not bury the edit action in a dense grid.

Below Routine, show active Notes, with safety and injury first, then constraint, goal, and preference. Notes are durable facts the Member volunteered, so show their original text and date. A language tag should read “EN · original text” or “ES · original text” only when the text differs from the dashboard language. Use “original text” rather than “textual”, which describes the implementation rather than the Coach’s need.

Show recent Sessions after the decision context. Each Session row includes date, Workout if known, Set count, and whether it had a Member comment. Tapping expands the Sets and the original comment. Keep the latest three to five expanded or visible, with “Show more Sessions” for the remaining history. Do not expose chat history or Compaction summaries.

Put a collapsed “Last logged Set by Exercise” lookup below Sessions or inside the Routine card as a mobile-friendly disclosure. It is supporting evidence for a rewrite, not a second primary column. Notes and Session comments retain their source language and carry the same small language tag.

On a phone, use one column and sticky bottom action “Edit Routine” only while the page is scrolled past the title. On a tablet, use a two-column layout only after the identity and safety callout: left Routine and Sessions, right active Notes and the supporting lookup. The reading order in the DOM must remain identity, safety, Routine, Notes, Sessions so a narrow screen and assistive technology preserve the coaching decision path.

## Routine editor handoff

The review is for the two screens, but the handoff needs one contract: entering Edit Routine should carry Member identity, current Routine version, active Notes, and the last logged Set context into the editor. The page must not make a Coach remember whether a Routine is Agent-written or coach-authored. The editor should visibly confirm that this is a coach-authored Routine write and that replacing it leaves the old Routine deactivated, not deleted.

## EN/ES behavior

Make language a Coach preference, with browser language as the initial default if no preference exists. Do not put the switch in the Member work area or reset the selected Member when it changes. Localize dashboard labels, dates, weekdays, relative time, decimal marks, accessibility labels, and button text. Use locale-aware formatting rather than hand-built strings.

Do not translate Exercise names, Workout names, Notes, or Member-provided Set comments. Keep the exact source text and add a compact tag only when source language differs from the dashboard language: “EN · original text”. The tag must be adjacent to the text, not a footnote. “safety”, “injury”, and other Note kinds are controlled labels and should translate; the Note body does not.

Avoid comma ambiguity in Spanish. Render Sets as “12,5 kg x 12 / 12 / 10” or one Set per line, never “12,5 kg x 12,12,10”. Use the locale’s date format but keep an unambiguous day and month when relative time is not enough. The Coach’s dashboard language should not silently change the Agent’s chat language for Members; those are separate settings and outside these screens.

# What you would cut

Cut the Table/Cards/Split segmented control and the Split permanent rail.

Cut the four-week day grid, its weekday micro-headings, and the explanatory “bands are a reading of Gap” copy.

Cut the duplicate Gap callout, the three large roster stat tiles, and the “Read-only” prose under the list. A concise read-only affordance is enough.

Cut the full-screen language footnote. Replace it with adjacent source-language tags and accessible help text.

Cut the always-visible retired Notes from the default scan, and cut detached last-weight prominence. Keep both capabilities behind disclosure or in the Routine context.

# What you disagree with in the brief above

I disagree with treating three roster views as a settled product control. The closed scope requires one roster job, not three presentations, and the choice itself is a burden for a Coach who is standing up. I also disagree that a day grid is justified because a Routine pins Workouts to weekdays: a grid records calendar presence, while the Coach needs to know the next Workout, the last Session, and what was actually logged.

I disagree with Gap being presented as both the sole visual heat scale and the repeated centerpiece of the Member page. Gap is the correct stable sort spine for this first roster, but it needs plain-language context and a “No Session yet” state, not an implied urgency model. Finally, I disagree with explanatory language footnotes on every screen. Preserve source language rigorously, but teach it at the point of reading with a small original-text tag.

I do not disagree with the closed jobs or safety rules: the web remains read-only except for writing a Member’s coach-authored Routine and acknowledging the safety Note, no messaging or nudging is added, chat history and Compaction summaries stay hidden, and the safety Note never re-sorts the roster.

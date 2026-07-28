# Review: coach dashboard prototypes (#76)

UX/UI review of `coach-dashboard-screens.html` and `coach-dashboard-language.html`.
Planning only; nothing in the prototypes was changed.

## Verdict

Ship one roster, not three, and make its day grid schedule-aware.
Today the grid shows Sessions but not the Routine's pinned weekdays, so it literally cannot show "which day is being missed", which the prototype's own notes claim is its purpose.
Severity color should come from missed scheduled Workout days, with Gap kept as the decided sort key; raw Gap cries wolf every Monday for any Member who trains three days a week.
The Member page should merge last weights into the Routine lines, surface Notes before the fold, and state the Gap once instead of four times.
The split view is a responsive breakpoint, not a view; the switcher should not exist.

## What is wrong now

Ordered by what it costs the Coach on the floor.

### 1. The day grid cannot answer its own question

`coach-dashboard-screens.md` says the grid is by day rather than by week "on purpose: a Routine pins Workouts to weekdays, so the grid shows *which* day is being missed".
But the grid renders only three cell states: hit (green), future (dashed), and empty.
An empty Tuesday square is unreadable: for a Mon/Wed/Fri Member it means rest day, for a Tue/Thu Member it means a missed Workout.
The one fact that turns an empty square into a missed day, the Routine's pinned weekdays, is not drawn.
Marta's grid happens to read well only because her perfect Mon/Wed/Fri adherence followed by silence makes a visible cliff; for any Member with looser adherence the grid is decoration.
This is the roster's core promise, unkept.

### 2. Gap severity is schedule-blind

The sort by Gap is decided and fine; the color is not.
`heat()` hard-codes hot at 7 days and warm at 4 for everyone.
A Member whose Routine is Monday and Thursday shows Gap 4 every Monday morning while perfectly on plan, and lands amber in "Slipping".
A Coach who sees false amber every week stops believing the colors within a month, and then the colors protect no one.
The severity signal must be "scheduled Workout days inside the Gap with no Session", which the data model can derive today: the Routine pins weekdays, Sessions have dates.

### 3. Three roster views is a product decision exported to the Coach

The .md insists these are "three views the Coach switches between - not three candidates to pick one from".
That framing dodges the cost: three codepaths, three test surfaces, a segmented control occupying the best real estate in the top bar, and a Coach who now owns a configuration question the designers declined to answer.
All three show the same facts by the prototype's own admission, so the switcher buys nothing but the choice itself.
Worse, none of the three works where the Coach actually is:

- Split hard-codes a 320px rail and breaks on a phone outright.
- Table is hover-first (row highlight on `:hover`) and mouse-shaped.
- Cards scale badly: 28 squares plus labels per card is fine at 9 fixture Members and noise at a real gym's 80.

The card content earns its place; the cards *view* does not.
The insight (weekday-aligned attendance) belongs in the row of the one list, not in a separate layout.

### 4. No way to find a specific Member

Mid-session reality: Marta walks up and says her shoulder hurts; the Coach has fifteen seconds to pull her page.
There is no search, no name jump, and no filter.
At 9 fixture Members that is invisible; at 80 it is a scroll hunt.
The "Open flags 1" stat is dead text; it should be the tap that filters to flagged Members.

### 5. The Member page buries what a plan-writer needs and repeats what they do not

Above the fold the same fact is stated four times: "last Fri 11 Jul" in the sub line, "Gap 12 days" in the stat, "12 days with no session" in the gapcall bar, and "Fri 11 Jul · 12 days ago" as the first Session row.
Meanwhile the injury Note ("no deep lunges") sits bottom-right, and on a phone the columns stack so Notes render dead last, after every Session.
For someone about to rewrite a plan the order is exactly backwards: constraints are the one thing that must not be missed, and they are the least prominent thing on the page.
The "Last weight per Exercise" card duplicates numbers already visible in the Sessions list and floats free of the plan those weights serve.

### 6. Pure Gap sort has no floor, so the top of the roster becomes a graveyard

A Member who quit in March sits at Gap 140, permanently first, permanently red.
The Coach learns to scroll past the top of "who needs me", which is the one place they must not go numb.
The domain already has the answer: Check-in state includes lapsed.
Lapsed Members belong in a collapsed tail section, not at the head of the list.

### 7. The Edit button hides an irreversible ownership transfer

CONTEXT.md: a coach-authored Routine is one "the Agent never restructures".
So the moment a Coach saves an edit to Marta's Agent-written Routine, the Agent stops managing it, forever, for that Routine.
The prototype's Edit button carries none of this weight.
The Coach must see the consequence before committing, not discover it weeks later when the Agent has silently stopped adapting the plan.

### Minor

- "flag · ticked" chips on roster rows (Dani) are finished business shown as if it were pending; noise.
- The three stat tiles: "Training 9" is vanity, "Gap >= 7d" duplicates the band count, only open flags is load-bearing.
- ES: `12,5 kg × 12,12,10` overloads the comma, already flagged in the .md and real.
- Acknowledging a flag is a write on a page the brief calls read-only; it needs the same care as the Routine edit (show who ticked and when, allow untick).

## Proposed design

### The roster (one view)

One phone-first list, grouped, searchable.
The split layout survives as a breakpoint: at >= 900px the same list docks left and the Member page fills the right pane, exactly the current split view, but as responsive behavior with zero user choice.
The table and cards views are deleted.

Top of screen: the roster title, a search field, and one filter chip ("2 flags") that narrows to Members with open safety flags.
No segmented control, no stat tiles.

Band headers stay, since they made the Gap readable, but they group the single list rather than justifying a separate cards layout:
"Needs you now", "Slipping", "On track", then a collapsed "Lapsed (3)" tail for Members whose Check-in state is lapsed.
Bands remain a reading of the schedule-aware severity below, never a stored field.

Row anatomy, one line tall, thumb-sized (min 56px):

```
[flag dot] Marta Ruiz            [14-day strip]   12d
```

- Name, with a red dot before it only for an open safety flag.
- A 14-day day strip: two weeks, Mon-first, aligned to weekday like today's grid but half the window, small enough for a row.
- Cell states: filled green = Session; ringed outline = scheduled Workout day with no Session (missed); faint = unscheduled day; dashed = future.
  The ring is the fix for problem 1: a missed day is now drawn, not inferred.
- Gap as a compact number, colored by missed scheduled days (problem 2), not by raw thresholds.
- New Members get the "new" chip in place of a meaningless strip.

Sort stays strictly by Gap inside each band, as decided.

### The Member page

Single column on phone, same content docked right of the rail on wide screens.
Reading order rebuilt for the Coach's two moments: "what is going on with this person" and "I am about to rewrite the plan".

1. Header: name, member since, Session count, and the Gap stated exactly once, colored by the schedule-aware severity.
   The gapcall bar and the duplicate stat are gone.
2. Safety flag banner, as today, with Tick off; after ticking it shows "ticked by Ale · 23 Jul" and an undo.
3. Active Notes, compact, injury and constraint kinds sorted first.
   Five one-line Notes cost 100px and are the highest-value pixels on the page for a plan-writer.
   Retired Notes collapse behind a "1 retired" toggle instead of rendering dimmed inline.
4. The full 4-week day grid, one instance, larger than the roster strip, with the same scheduled-day rings.
   Four weeks is the right window here even though the roster row gets two; the Member page is where the longer pattern matters.
5. Routine, with the Edit button, and with last weights merged into its lines:

   ```
   Monday · Legs
     Back squat   4 × 6-8    last 70 kg · 7 Jul
     Leg press    3 × 10     last 120 kg · 7 Jul
   ```

   This deletes the separate weights card, halves the right column, and puts each weight next to the prescription it informs, which is the only reason a Coach wants it.
   Exercises logged outside the Routine still appear in their Sessions.
6. Sessions, as today (the dense "Deadlift 85 × 5,5,4" strings are good), each row additionally marked when the Session landed on an unscheduled day.

The Edit entry point stays on this page; it is the only write and this is where its context lives.
But it opens a dedicated editor, not an inline mode.

### The Routine editor (the write v1 must nail)

Full-screen on phone, right-pane takeover on wide.

- The editing surface mirrors the Routine shape: weekday picker, Workout name, Exercise rows with sets and reps.
- Exercise names autocomplete from the Catalog only, since Routines prescribe only Catalog names; free text is rejected at the field, not at save.
- Pinned alongside (bottom sheet on phone): the Member's active injury and constraint Notes, and last weight per Exercise with tap-to-insert.
  This is the moment those two datasets exist for; pinning them here is why the Member page could get leaner.
- Before the first edit of an Agent-written Routine, one interstitial line with a confirm:
  "This makes the Routine coach-authored. The Agent will follow it but will never restructure it again."
- Save creates the new active Routine and keeps the old one deactivated, matching the model; the editor says so in the save button's subtext ("Replaces the current Routine; the old one is kept").

### EN/ES

The three untranslated categories are correct and the reasoning in the .md is sound; keep all three.
Sharpen the framing: the primitive being marked is provenance (the Member's own words versus system text), not language.
The `verbatim` left-border should therefore appear on every Member quote in both languages, as it already does; the language tag appears only on mismatch, as it already does.
Keep that, and shorten the tag to just `EN` or `ES`; "textual" next to a quote border restates the border.

Changes:

- Fix the comma collision in both languages, not just ES: reps become `5-5-4` everywhere, so `12,5 kg × 12-12-10` reads cleanly and EN/ES stay parallel.
- The language is a Coach preference stored on the coach-flagged Member, defaulting from `Accept-Language` on first visit.
  It is explicitly not a Gym column: the Gym's chat language belongs to each Member's conversation with the Agent, and the dashboard's reading language belongs to the reader.
  This also answers the .md's third open question: no, the Agent's chat language does not follow this switch, by design, and the settings copy should say so.
- Steal from the ES copy: "Sin venir" (has not come in) is plainer than "Gap".
  The EN column header could be "Away" with the same meaning; "Gap" stays the domain word in code and docs, but the Coach-facing label does not have to be jargon.
- The footnote explaining non-translation is good pedagogy for the prototype and wrong for the product; it becomes a one-time tooltip on the language switch, not a permanent banner on every screen.

## What I would cut

- The view switcher and two of the three roster views; split becomes a breakpoint, cards becomes the row strip.
- The gapcall bar and the Gap stat tile on the Member page; one Gap statement, in the header.
- The roster stat tiles "Training" and "Gap >= 7d"; open flags survives as a filter chip.
- The standalone "Last weight per Exercise" card; merged into Routine lines and the editor.
- "flag · ticked" chips on roster rows; acknowledged flags show only on the Member page.
- Dimmed inline retired Notes; collapsed behind a count instead.
- The permanent translation footnote.

## What I disagree with in the brief

- "Roster sorted by Gap": I keep the sort but reject raw Gap as the severity signal.
  Sorting and alarming are different jobs; the brief conflates them, and schedule-blind alarm thresholds will train the Coach to ignore the roster's colors.
- "Read-only roster and Member page": Tick off is a write, so v1 has two writes, not one.
  Underselling it invites shipping an acknowledgment with no actor, no timestamp display, and no undo, which for a safety record is not acceptable.
- Pure Gap ordering with no lapse handling contradicts the roster's stated job.
  "Who needs me" and "who left months ago" are different questions; the Check-in lapsed state already exists and should fold those Members into a collapsed tail, even though that softens the decided "sorted by Gap" purity.
- "No messaging a Member from the web" is right, but the decision should not block a hand-off.
  The Coach's next act after "who needs me" is to talk to the person; a plain `t.me` deep link on the Member page opens the existing Telegram conversation without the web app ever composing or sending anything.
  If even that is out of scope for v1, fine, but it should be recorded as deferred, not forbidden by implication.

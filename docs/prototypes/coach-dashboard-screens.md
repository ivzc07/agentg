# PROTOTYPE — Coach dashboard: roster and Member screens

> **Throwaway UI for [ticket #76](https://github.com/ivzc07/agentg/issues/76).** The question it answers: *what information belongs on the roster and the Member page, and how does a Coach move between them?*
> Open `coach-dashboard-screens.html`. React to it — cross things out, rewrite lines. The verdict is the deliverable; both files get deleted once #76 closes.

The roster is **three views the Coach switches between** (segmented control in the top bar) — not three candidates to pick one from.
There is exactly **one Member page**, shown under all three.

## Run it

```
python -m http.server -d docs/prototypes 8777
```

Then open <http://localhost:8777/coach-dashboard-screens.html>.
`?view=table|cards|split&screen=list|member` is bookmarkable. The top-bar control sets the view (a product control); the dark bar at the bottom only jumps between screens (a prototype crutch).

## Verdict

**This prototype is the one, chosen by the owner.** Three roster views the Coach switches between - Table, Cards, Split - over one shared Member page.
The Cards day grid is per **day**, not per week, because a Routine pins Workouts to weekdays and the weekly version hid which day was being missed.

Two later prototypes were built and **parked, not adopted**:

- [`coach-dashboard-v2.html`](coach-dashboard-v2.html) - a rebuild driven by three independent design reviews, kept at [`docs/design/coach-dashboard-reviews/`](../design/coach-dashboard-reviews/). It collapses the three views into one responsive roster and makes severity schedule-aware. Rejected for now: the owner wants the view switcher.
- [`coach-dashboard-v3-dark.html`](coach-dashboard-v3-dark.html) - the same information in a black, zero-radius, monospace-eyebrow visual language. **Kept for later**: the look is wanted, the timing is not.

The reviews themselves live in [`docs/design/coach-dashboard-reviews/`](../design/coach-dashboard-reviews/), outside this throwaway directory; their substantive findings are also recorded as fog on the map, so nothing is lost when these files are deleted.

## Fixtures

**Iron Temple**, 9 Members, kg. Today is Wed 23 Jul.
Gaps run 0 to 12 days. **Marta Ruiz** (gap 12) carries an open safety flag; **Dani Osei** (gap 5) carries one already ticked off; **Tom Beckett** and **Ana Vidal** are new.
Every variant drills into Marta: 41 Sessions, an Agent-written Routine (Mon legs / Wed push / Fri pull), last weight per Exercise, 5 active Notes and 1 retired.

All three views show the *same* facts about the *same* Members. Only the shape and the movement differ.

## The Member page (one, shared)

Name, member-since, session count, last Session and Gap in the header; the Gap called out again as a coloured line; then the safety flag if there is one, with a **Tick off** button.
Below that, two columns: **Routine** (with the **Edit** button — the one write v1 must nail) and **Sessions** on the left; **last weight per Exercise** and **Notes** on the right. Retired Notes are dimmed, not hidden.

## Roster view — Table

Dense sortable list: Member, Gap, last Session, markers. Click a row and the whole page becomes the Member; a breadcrumb walks back.

## Roster view — Cards

The same Members as cards in three bands — *Needs you now* / *Slipping* / *On track* — each carrying a **4-week day grid**: 7 columns Mon–Sun, one square per day, green where a Session landed, dashed where the day is still in the future.
By day rather than by week on purpose: a Routine pins Workouts to weekdays, so the grid shows *which* day is being missed, not just how many.
The bands are a reading of Gap, not a new field.

## Roster view — Split

The roster never leaves: a narrow permanent left rail, the Member page filling the right pane. No back button, because nothing was left.

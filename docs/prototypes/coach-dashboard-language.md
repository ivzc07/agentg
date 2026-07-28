# PROTOTYPE — Coach dashboard in two languages

> **Throwaway UI for [ticket #76](https://github.com/ivzc07/agentg/issues/76), second pass.** The question it answers: *what actually changes when the Coach flips EN/ES?*
> Companion to [`coach-dashboard-screens.md`](coach-dashboard-screens.md) — same three roster views, same single Member page, plus a language switch. React to it; the verdict is the deliverable and both files get deleted once #76 closes.

## Run it

```
python -m http.server -d docs/prototypes 8777
```

Then open <http://localhost:8777/coach-dashboard-language.html>.
`?lang=en|es&view=table|cards|split&screen=list|member` is bookmarkable. **EN / ES** sits at the right of the top bar, next to the Coach's name.

## Verdict

_Pending — filled in when the owner reacts._

## What the switch changes

Every label, column heading, band name, marker, button and empty state. Also the things that are easy to forget:

- **Weekdays and months** - `lunes / miércoles / viernes`, `12 jun`, `mié 23 jul`, and the `lu ma mi ju vi sá do` headings on the Cards day grid.
- **Relative time** - "12 days" becomes "12 días", "3 days ago" becomes "hace 3 días".
- **The decimal mark** - `12.5 kg` becomes `12,5 kg`.
- **`<html lang>`**, so a screen reader says it right.

## What the switch deliberately does *not* change

This is the part worth arguing about. Three kinds of text stay put, and the dashboard says so in a footnote on every screen:

1. **Exercise names.** `exercises.name` is unique and product-wide - "Back squat" *is* the row, not a translation of it. A Spanish name means either a second name column per language or `aliases` doing a job it was not built for (it exists to match what a Member typed, not to display).
2. **What the Member said.** Notes and set comments are the Member's own words, written by the Agent in whatever language they chat in. Translating them puts words in their mouth. Left alone, a Spanish dashboard shows English quotes - so untranslated text carries a small `EN · textual` tag.
3. **Workout names.** "Legs", "Push", "Pull" are free text on the `workouts` row, typed by whoever wrote the Routine.

The fixture mixes languages on purpose: Marta's goal and constraint Notes are in Spanish, her injury and preference Notes in English.

## Open questions this surfaced

- **Whose setting is the language?** A Coach preference, a Gym column next to `timezone` and `weight_unit`, or just the browser's `Accept-Language`? Nothing stores it today.
- **Reps read badly in Spanish.** `12,5 kg × 12,12,10` uses a comma for both the decimal mark and the rep separator. Needs a different separator (`12-12-10`) or a different decimal convention.
- **Does the Agent's chat language follow this switch?** It is the same Gym; a Coach reading Spanish while the Agent writes English to Members is defensible, but it should be a decision, not an accident.

# Coach dashboard: three independent UX reviews

Three models were given the same brief and the same prototype, in isolation, and asked to propose a better design for the coach-facing web dashboard's two screens: the Gym-wide roster and one Member's page.
They did not see each other's answers.

| Report | Model |
| --- | --- |
| [`fable.md`](fable.md) | Claude Fable 5 |
| [`codex-sol.md`](codex-sol.md) | GPT-5.6 Sol, via Codex CLI |
| [`codex-luna.md`](codex-luna.md) | GPT-5.6 Luna, via Codex CLI |

What they reviewed was the prototype in `docs/prototypes/coach-dashboard-screens.html` and `coach-dashboard-language.html`, resolved on [#76](https://github.com/ivzc07/agentg/issues/76).
Those prototypes are throwaway and will be deleted; these reports are kept because their findings outlive them.

## What all three agreed on

**Kill the three-view switcher.** Table, Cards and Split are three layout strategies for one job, not three jobs. All three independently argued for a single responsive roster where the split rail is a breakpoint rather than a Coach-visible choice. The owner overruled this and kept the switcher, so it stands - but the argument is on the record.

**The day grid cannot answer its own question.** An empty square means "rest day" for a Mon/Wed/Fri Member and "missed Workout" for a Tue/Thu Member. Without drawing the Routine's pinned weekdays, the grid cannot show which day is being missed, which is the stated reason it exists.

**The Member page buries what a plan-writer needs.** Notes - injuries and constraints above all - are the one thing that must not be missed when rewriting a Routine, and they sit last on a phone. Last weight per Exercise belongs next to the prescription it informs, not in a detached lookup card.

**Cut the chrome.** The Gap is stated three or four times above the fold; the roster stat tiles restate what the rows already say; the translation footnote is a design-decision record leaking onto every screen.

## What only one of them caught

- **Fable:** Gap colour is schedule-blind, so a Member on a two-day Routine shows amber every week while perfectly on plan - and a Coach who sees false amber weekly stops believing the colours. Also: saving an edit to an Agent-written Routine is an irreversible ownership transfer, and the Edit button carries none of that weight.
- **Sol:** neither prototype contains any responsive rule at all, so the 320px rail and two-column Member layout survive intact at phone widths. Also: rows are click-handled `div`s rather than links or buttons, which costs keyboard access and assistive-technology naming.
- **Luna:** "No Session yet" is a real state that a derived Gap cannot express, and the roster needs an explicit rule for where those Members sort.

## Where the findings went

The substantive ones are recorded as fog on the map, [#70](https://github.com/ivzc07/agentg/issues/70), under **Not yet specified**, so they surface as tickets when the frontier reaches them.
`docs/prototypes/coach-dashboard-v2.html` is a working prototype of the consolidated recommendation, built and parked unadopted.

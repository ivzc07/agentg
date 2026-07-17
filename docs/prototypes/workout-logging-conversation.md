# PROTOTYPE — Workout-logging conversation

> **Throwaway script for [ticket #5](https://github.com/ivzc07/agentg/issues/5).** The question it answers: *how does a member log a workout mid-session with the least possible typing?* React to it — pick a variant, cross things out, rewrite lines. The verdict is the deliverable; this file gets absorbed or deleted once #5 closes.

## Verdict

**Variant A — "Just type it" (free text only) — chosen by the owner.** No inline buttons for set logging; the agent parses terse lines ("60 8,8,7", "same as last time"). Variants B and C are rejected. The scripted defaults stand: the agent *proposes* last-time numbers but never assumes them logged, and the tone (light emoji, specific encouragement, "no guilt" gap opener) is accepted as written.

## Fixtures

- **Dani**, member at Iron Temple (gym). Routine: Mon **legs**, Wed **push**, Fri **pull**.
- Today is **Wednesday**. Last session: **Monday, 2 days ago — legs**.
- Last *push* session (a week ago): bench 60 kg 8/8/7 · overhead press 40 kg 8/7/6 · dips (bodyweight) 10/10/8.
- `[ … ]` = Telegram inline button (a tap, zero typing). Everything else the member types.

All three variants play the same visit. The differences are only in **how sets get confirmed**.

---

## Variant A — "Just type it" (free text only)

**Dani:** I'm here

**Agent:** 💪 Welcome back, Dani! Last time was 2 days ago — legs. Today's Wednesday, so: **push day**.

1. Bench press — last time 60 kg · 8/8/7
2. Overhead press — last time 40 kg · 8/7/6
3. Dips — last time 10/10/8

Log sets however is fastest for you — "60 8,8,7" works, so does "same as last time".

**Dani:** bench 60 8,8,8

**Agent:** Bench 60 kg ×8 ×8 ×8 ✅ — all three at 8, that's the 8/8/7 from last week beaten. Overhead press next.

**Dani:** same as last time

**Agent:** Overhead press 40 kg ×8 ×7 ×6 ✅ (copied from last push day). Dips to go.

**Dani:** how do i do dips again?

**Agent:** *(sends demo GIF)* Elbows tucked, lean slightly forward, full lockout at the top.

**Dani:** dips 10,10,9

**Agent:** Dips ×10 ×10 ×9 ✅ — one rep up on the last set.

**Dani:** actually bench was 62.5 not 60

**Agent:** Fixed — bench **62.5 kg** ×8 ×8 ×8. Even better: that's +2.5 kg over last week. 📈

**Dani:** done

**Agent:** 🏁 Push day logged: 9 sets.
- Bench 62.5 kg 8/8/8 — **up 2.5 kg**
- OHP 40 kg 8/7/6 — held
- Dips 10/10/9 — +1 rep

Solid session. See you Friday for pull?

*Typing count: 6 short messages. Fastest for members who don't mind typing; everything hinges on the agent parsing terse lines correctly.*

---

## Variant B — "Tap through" (buttons first, one exercise at a time)

**Dani:** I'm here

**Agent:** 💪 Welcome back! 2 days since legs. Push day today — 3 exercises, I'll walk you through.

**1/3 Bench press** — last time 60 kg · 8/8/7. Plan: 60 kg 3×8.
`[ ✓ did 3×8 ]` `[ heavier ]` `[ lighter ]` `[ type it ]` `[ skip ]`

**Dani:** *(taps `[ heavier ]`)*

**Agent:** Bench at **62.5 kg** then. When you're done:
`[ ✓ 3×8 at 62.5 ]` `[ fell short ]` `[ type it ]`

**Dani:** *(taps `[ ✓ 3×8 at 62.5 ]`)*

**Agent:** Bench 62.5 kg ×8 ×8 ×8 ✅ — +2.5 kg on last week!

**2/3 Overhead press** — last time 40 kg · 8/7/6. Plan: 40 kg 3×8.
`[ ✓ did 3×8 ]` `[ fell short ]` `[ type it ]` `[ skip ]`

**Dani:** *(taps `[ fell short ]`)*

**Agent:** No problem — which was it?
`[ 8/8/7 ]` `[ 8/7/6 like last time ]` `[ type it ]`

**Dani:** *(taps `[ 8/7/6 like last time ]`)*

**Agent:** OHP 40 kg ×8 ×7 ×6 ✅.

**3/3 Dips** — last time 10/10/8.
`[ ✓ did 10/10/8 ]` `[ did more ]` `[ type it ]` `[ skip ]`

**Dani:** *(taps `[ did more ]` → agent offers `[ 10/10/9 ]` `[ 10/10/10 ]` `[ type it ]` → taps `[ 10/10/9 ]`)*

**Agent:** Dips ×10 ×10 ×9 ✅ — that's the whole workout!
`[ 🏁 finish ]` `[ add an exercise ]`

**Dani:** *(taps `[ 🏁 finish ]`)*

**Agent:** 🏁 Push day logged: 9 sets. Bench **up 2.5 kg**, OHP held, dips +1 rep. See you Friday!

*Typing count: 1 message ("I'm here"). Corrections after the fact still need typing — buttons can't reach arbitrary numbers. More messages and taps overall; the chat gets busy.*

---

## Variant C — "Confirm or say otherwise" (one checklist, assume last time)

**Dani:** I'm here

**Agent:** 💪 Welcome back — 2 days since legs. Push day. **I'll assume same-as-last-time unless you say otherwise:**

1. Bench press 60 kg 3×8 — `[ ✓ ]`
2. Overhead press 40 kg 8/7/6 — `[ ✓ ]`
3. Dips 10/10/8 — `[ ✓ ]`

Tap ✓ as you finish each one, or type what changed.

**Dani:** bench 62.5 8,8,8

**Agent:** Bench 62.5 kg ×8 ×8 ×8 ✅ — +2.5 kg over last week! Two to go.

**Dani:** *(taps `[ ✓ ]` on overhead press)*

**Agent:** OHP 40 kg ×8 ×7 ×6 ✅ (as last time).

**Dani:** *(taps `[ ✓ ]` on dips)*

**Agent:** Dips ×10 ×10 ×8 ✅ — workout complete! `[ 🏁 finish ]` `[ not done yet ]`

**Dani:** wait dips last set was 9

**Agent:** Fixed — dips ×10 ×10 ×9. `[ 🏁 finish ]`

**Dani:** *(taps `[ 🏁 finish ]`)*

**Agent:** 🏁 9 sets logged. Bench **up 2.5 kg** — first bump in three weeks. See you Friday for pull?

*Typing count: 2 short messages, 3 taps. Typing only where reality deviated. The risk: a lazy ✓ silently logs numbers that aren't true.*

---

## Shared scene — returning after a real gap

*(Any variant. 9 days since the last session.)*

**Dani:** I'm here

**Agent:** Dani! 👋 Been **9 days** — last time was legs, on the 8th. No guilt, the bar missed you. Today's Wednesday → push. After a break like that, want the usual weights or ~10% lighter to ease back in?
`[ usual ]` `[ lighter ]`

**Dani:** *(taps `[ lighter ]`)*

**Agent:** Smart. Adjusted: bench 55 kg, OHP 35 kg, dips as usual. *(…continues in the chosen variant's style)*

---

## What this surfaced — feed back to the domain model

1. **Weight units.** Every line assumes kg. Unit (kg/lb) needs a home — per-gym default, possibly per-member override. `Set.weight` is meaningless without it.
2. **Bodyweight sets.** Dips have reps but no weight — `Set.weight` must be nullable (and weighted dips need it back later).
3. **Session lifecycle.** Logging happens into an *open* session; "done"/`[ 🏁 finish ]` closes it. The model has no open/closed state — and needs an auto-close fallback for members who never say done (e.g. end of day).
4. **Corrections = editing Sets.** "that was 62.5 not 60" edits already-logged rows in the current session. Sets must be updatable, and the agent needs a tool scoped to "the sets I just logged".
5. **Proposal source confirmed.** "Same as last time" means the last *Session's* sets for that exercise — not the Routine's prescription. Matches the memory design's `get_last_sets`.
6. **Per-visit targets are ephemeral.** Variants B/C propose a target ("62.5 kg 3×8") before it's performed. That target lives only in chat — it is *not* a Workout field. Flag if you'd rather store it.
7. **Gap deload offer.** "~10% lighter after 9 days" is a rule a coach should be able to change — belongs with routine rules, not hardcoded.
8. **Skip/substitute is fine.** `[ skip ]` and "add an exercise" lean on the settled decision that a Session need not match the scheduled Workout.

## How to react

1. Which variant — A, B, C, or a blend (e.g. C's checklist with B's gap-deload buttons)?
2. Is "assume same as last time" the right default proposal, or too presumptuous?
3. Tone check: emoji level, encouragement, the "no guilt" gap opener — more, less, different?

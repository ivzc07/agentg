# What the current data model can answer about a Member's training

Research for [#74](https://github.com/ivzc07/agentg/issues/74), on the map [Coach web dashboard](https://github.com/ivzc07/agentg/issues/70).
Planning only: nothing here is a build instruction.

Read against the three screens [#71](https://github.com/ivzc07/agentg/issues/71) put in scope: the Gym-wide "who needs me" roster, the Member page, and the Routine writer.

## The short version

Almost everything the dashboard wants to *show* is already recorded.
What is missing is not data but **queries**: every existing read is shaped for one Member in one chat turn, and the roster needs set-based reads across a Gym.

Three real holes in the data itself, in order of how much they hurt:

1. A Session is not linked to the Workout it was meant to be, so **adherence is inferred, never known**.
2. A safety flag is not a first-class thing - it is a note of kind `other` whose text starts with the literal string `Safety flag: `.
3. No write anywhere records **who** made it or **when it was last changed**, so a dashboard write and a chat write are indistinguishable after the fact.

## What is already there

Every domain row carries `gym_id` (`src/agentg/models.py:1`), so scoping a read surface to one tenant is cheap and safe throughout.
Weights are converted to the Gym's unit on the way in (`src/agentg/training.py:540`), so a stored weight is always in `Gym.weight_unit` and needs no conversion on read.

| The dashboard wants | Where it lives | State |
|---|---|---|
| Roster of a Gym's Members | `Member.gym_id`, `Member.is_coach` | rows yes, **no query** |
| Days since last visit ("Gap") | `TrainingStore.newest_session_date` | per-Member only, **needs a set-based version** |
| Session list with contents | `sessions` + `sets` | rows yes, **no query** (`_session_exercises` is private) |
| Weight and reps per set | `Set.weight`, `Set.reps` | yes |
| Volunteered RPE and per-set note | `Set.rpe`, `Set.note` | yes, sparse by design |
| Last working weight for an Exercise | `TrainingStore.last_sets` | per Exercise only |
| Weight over time for an Exercise | `TrainingStore.exercise_history` | **returns no date**, top set only |
| Notes, incl. injuries and goals | `NotesStore.active` | active only, **retired need a query** |
| Active Routine, workouts, exercises | `RoutineStore.active_routine` | yes |
| Exercise catalog for a picker | `TrainingStore.catalog_names` | yes |
| Demo clip per Exercise | `Exercise.demo_slug`, `DemoOverride` | yes |
| Next-weight suggestion | `advice.suggest_for_today` | yes, but **only today's Workout** |
| Check-in state (off / snoozed / lapsed) | `Member.checkin_state` | yes |

## Screen 1: the roster

**Nothing lists a Gym's Members.**
`LinkingStore` can find a Member by id (`member_in_gym:143`) or by exact name (`members_by_name:127`), and the latter already selects every Member of the Gym and filters in Python - so the raw select exists, it is just not exposed.

**Gap needs a set-based query.**
`newest_session_date` (`training.py:176`) is the right definition - it counts today's open Session as activity, which is what the check-in sweep uses.
But it is one query per Member.
A roster sorted by Gap wants one `GROUP BY member_id` over `sessions`; the `ix_sessions_member_started` index already covers it.
Note the sibling `latest_session_info` (`training.py:154`) *excludes* the open Session and answers a different question - it is for the chat opener, not the roster.

**A gym switch leaves a ghost Member.**
`link_member` (`linking_store.py:87`) creates a *new* Member row and re-points the channel, leaving the old row behind with its history and no channel.
A naive `WHERE gym_id = ?` roster would list that ghost: a name with a frozen Gap that the Coach can never reach.
`CheckinStore.sweep_rows` (`checkin_store.py:42`) already dodges this by joining `member_channels`; the roster should do the same, or decide deliberately to show departed Members.

**Safety flags are only findable by text prefix.**
`flag_to_coach_action` (`coaching.py:98`) writes `MemberNote(kind="other", text=f"Safety flag: {summary}")`.
There is no flag kind, no `handled` state, no record of whether the Member consented to the ping, and no timestamp for a Coach having seen it.
Marking flagged Members on the roster therefore means matching that literal prefix - which works, and is brittle the moment anyone touches the wording.
Injuries are separate and cleaner: `kind = "injury"`.

**Both existing per-Member reads ignore `gym_id`.**
`newest_session_date`, `active_routine`, `NotesStore.active` and friends filter on `member_id` alone.
That is safe today because a Member belongs to one Gym, but the dashboard - unlike the chat runtime - takes a Member id from a URL, so it must do its own scoping.

## Screen 2: the Member page

**Session history has no query.**
The building block exists and is private: `_session_exercises` (`training.py:491`) collapses a Session's Sets into one line per Exercise with the top weight.
Public reads only ever return the single *previous* Session.
A paginated Session list is a new query over `sessions` + `sets`, well served by `ix_sessions_member_started`.

**Session duration is derivable but weak.**
`started_at` and `closed_at` are both there, but a Session abandoned without "done" is auto-closed at its last activity three hours later (`training.py:436`), and logging without opening creates a Session stamped at the first set (`training.py:452`).
Duration is real for a Session the Member closed and fiction otherwise.

**A weight chart needs a new query.**
`exercise_history` (`training.py:359`) returns top weight and top-set reps per closed Session, most-recent-first - but **carries no date**, drops the open Session, and hides warm-ups and back-off sets.
Plotting weight over time means reading `sets` directly (it has `created_at`, `weight`, `reps`, `rpe`, `note`) or extending that method to carry the Session date.
Watch the index: `Set.session_id` is indexed, **`Set.exercise_id` is not**, so a per-Exercise history across a Member scans.

**PRs, volume and 1RM are not stored** - all derivable from `sets`, none recorded.

**Retired notes are a small open question.**
[#72](https://github.com/ivzc07/agentg/issues/72) says the Coach sees *all* Notes.
`NotesStore.active` returns only unretired ones; retired rows survive with `retired_at` set (`notes.py:45`) and need their own query.
Whether "all" includes them is a product call, not a data problem.

**Adherence is the real gap.**
Nothing links a Session to a Workout.
`Session` has no `workout_id` and no name, so "did they do Monday's Legs?" can only be inferred by comparing the Session's weekday and exercise list to the Routine.
Worse, the Routine may have changed since: superseded `Routine` rows survive deactivated with their Workouts intact, so the Routine active on a past date can be reconstructed as *the most recent Routine created on or before it* - approximate, and only because nothing is ever deleted.
`checkin.py:81` already does a crude version of this for the missed-pinned-day nudge.

## Screen 3: the Routine writer

Reading and writing both work today: `active_routine` (`routines.py:194`) returns the full structure, `save_routine(..., coach_authored=True)` (`routines.py:111`) writes it and deactivates the old one, `catalog_names` feeds an exercise picker.
Two absences:

- **No `Routine.created_by`.** The row records *that* a Coach wrote it, never *which* Coach - a blank spot for any Gym with more than one.
- **No history query.** Superseded Routines are all still there (`is_active = False`); nothing lists them, so "what did this Member have before?" is unanswerable without a new read.

## Not recorded anywhere

- **Bodyweight, measurements, photos.** Nothing.
- **Contact detail.** A Member is a `name` (from the channel display name) and a channel id. No email, no phone.
- **An actor on any write.** No audit trail, no `updated_at` on any table. Once the dashboard writes, nothing can say a change came from the web.
- **Rules doc history.** `set_rules_doc` overwrites in place. (Rules-doc editing is v2 per #71, but the same overwrite already applies to the chat path.)
- **A "Coach last looked" marker.** Needed the moment the roster wants "new since you were here".
- **Membership lifecycle.** No soft-delete or archived state; forget-me hard-deletes everything (`forget.py:31`). A Member who quit is just one with a long Gap.

## One cross-cutting wart

All day boundaries are computed in UTC.
`Gym.timezone` exists and the check-in sweep honours it (`checkin_sweep.py:58`), but `TrainingStore.today()` and `RoutineStore._today()` both use the process clock in UTC, each carrying a comment deferring gym-local days to a later ticket.
A dashboard showing "3 days ago" or "today's Workout" inherits that, and a Gym far from UTC will see it flip at the wrong hour.

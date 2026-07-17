"""Pure check-in decision logic (spec §Proactive check-ins).

Given a Member's check-in state, last Session, Routine pinned days, and the
gym-local time, decide whether to nudge, wind down, or stay quiet — warmly and
sparingly. This module is pure and exhaustively tested; the sweep wires it to
the DB and the channel.
"""

from datetime import UTC, date, datetime

import pytest

from agentg.checkin import (
    GIVE_UP_NUDGES,
    SEND_HOUR,
    CheckinData,
    decide_checkin,
)

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)  # date.weekday() values


def at(d: date, hour: int = SEND_HOUR) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=UTC)


def data(**overrides) -> CheckinData:
    base = dict(
        state="on",
        snoozed_until=None,
        last_nudge_on=None,
        nudges_this_week=0,
        ignored_nudges=0,
        last_session_date=None,
        signup_date=date(2026, 7, 1),
        pinned_weekdays=frozenset(),
        missed_workout=None,
        todays_workout=None,
    )
    base.update(overrides)
    return CheckinData(**base)


# --- the send window ---


def test_nothing_fires_before_9am():
    # 2026-07-16 is a Thursday; a fallback member 10 days idle
    d = data(last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16), hour=8), d).action == "none"


def test_nothing_fires_in_the_evening_quiet_hours():
    d = data(last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16), hour=21), d).action == "none"
    assert decide_checkin(at(date(2026, 7, 16), hour=23), d).action == "none"


def test_the_nudge_fires_at_9am_local():
    d = data(last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16), hour=SEND_HOUR), d).action == "nudge"


# --- fallback members (no routine): flat 3-day gap ---


def test_a_fallback_member_is_nudged_once_the_gap_reaches_three_days():
    # last session Monday 13th; Thursday 16th is 3 days later
    d = data(last_session_date=date(2026, 7, 13))
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "nudge"


def test_a_fallback_member_inside_the_gap_is_left_alone():
    d = data(last_session_date=date(2026, 7, 15))  # 1 day ago
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"


def test_a_fallback_member_with_no_sessions_uses_the_signup_date():
    d = data(last_session_date=None, signup_date=date(2026, 7, 10))
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "nudge"  # 6 days since signup
    fresh = data(last_session_date=None, signup_date=date(2026, 7, 15))
    assert decide_checkin(at(date(2026, 7, 16)), fresh).action == "none"


# --- routine members: missed a pinned day → nudge on the next pinned day ---


def test_a_missed_pinned_day_nudges_on_the_next_pinned_day():
    # Routine pins Mon & Wed. Last session two Fridays back; today is Wednesday.
    d = data(
        pinned_weekdays=frozenset({MON, WED}),
        last_session_date=date(2026, 7, 10),  # Fri before
        todays_workout="Push",
        missed_workout="Legs",
    )
    decision = decide_checkin(at(date(2026, 7, 15)), d)  # Wed 15th
    assert decision.action == "nudge"
    assert decision.nudge_type == "missed_pinned_day"


def test_no_nudge_on_a_pinned_day_when_the_previous_one_was_trained():
    # pinned Mon & Wed; trained Monday 13th; today Wednesday 15th
    d = data(pinned_weekdays=frozenset({MON, WED}), last_session_date=date(2026, 7, 13))
    assert decide_checkin(at(date(2026, 7, 15)), d).action == "none"


def test_no_nudge_on_a_rest_day_even_if_a_day_was_missed():
    # pinned Mon & Wed; missed both; today is Thursday (a rest day)
    d = data(pinned_weekdays=frozenset({MON, WED}), last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"  # Thu


# --- state gates ---


def test_off_members_are_never_nudged():
    d = data(state="off", last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"


def test_lapsed_members_are_never_nudged():
    d = data(state="lapsed", last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"


def test_a_snoozed_member_is_quiet_until_the_snooze_date():
    d = data(state="snoozed", snoozed_until=date(2026, 7, 28), last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"


def test_a_snooze_that_has_passed_lets_nudges_resume():
    d = data(state="snoozed", snoozed_until=date(2026, 7, 14), last_session_date=date(2026, 7, 6))
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "nudge"


# --- frequency cap: 2/week, never consecutive days ---


def test_no_nudge_on_the_day_after_a_nudge():
    d = data(last_session_date=date(2026, 7, 6), last_nudge_on=date(2026, 7, 15))  # yesterday
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"


def test_not_nudged_twice_on_the_same_day():
    d = data(last_session_date=date(2026, 7, 6), last_nudge_on=date(2026, 7, 16))  # today
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"


def test_the_weekly_cap_is_two():
    # already nudged twice this ISO week (Mon 13th – Sun 19th); today Fri 17th
    d = data(
        last_session_date=date(2026, 7, 6),
        nudges_this_week=2,
        last_nudge_on=date(2026, 7, 15),  # Wed, two days ago (not consecutive)
    )
    assert decide_checkin(at(date(2026, 7, 17)), d).action == "none"


def test_the_weekly_count_resets_in_a_new_calendar_week():
    # two nudges last week (last_nudge Sat 11th ISO week 28); today Thu 16th (week 29)
    d = data(
        last_session_date=date(2026, 6, 30),
        nudges_this_week=2,
        last_nudge_on=date(2026, 7, 11),
    )
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "nudge"


# --- give-up / wind-down ---


def test_after_enough_ignored_nudges_it_winds_down():
    d = data(last_session_date=date(2026, 6, 20), ignored_nudges=GIVE_UP_NUDGES)
    decision = decide_checkin(at(date(2026, 7, 16)), d)
    assert decision.action == "winddown"


def test_the_winddown_only_happens_when_a_nudge_would_otherwise_fire():
    # ignored count is high, but they're inside the gap → stay quiet, no winddown yet
    d = data(last_session_date=date(2026, 7, 15), ignored_nudges=GIVE_UP_NUDGES)
    assert decide_checkin(at(date(2026, 7, 16)), d).action == "none"


# --- tone of the canonical copy ---


def test_the_gap_nudge_copy_is_warm_and_guilt_free():
    d = data(last_session_date=date(2026, 7, 13))
    message = decide_checkin(at(date(2026, 7, 16)), d).message
    assert message and "?" in message  # an invitation, not a scolding
    assert not any(word in message.lower() for word in ("lazy", "should", "fail", "guilt"))


def test_the_pinned_nudge_names_the_workout_warmly():
    d = data(
        pinned_weekdays=frozenset({MON, WED}),
        last_session_date=date(2026, 7, 10),
        todays_workout="Push",
        missed_workout="Legs",
    )
    message = decide_checkin(at(date(2026, 7, 15)), d).message
    assert message and "Legs" in message and "?" in message


def test_the_winddown_copy_leaves_the_door_open():
    d = data(last_session_date=date(2026, 6, 20), ignored_nudges=GIVE_UP_NUDGES)
    message = decide_checkin(at(date(2026, 7, 16)), d).message
    assert message and "whenever" in message.lower()


@pytest.mark.parametrize("hour", [8, 21, 22, 0, 7])
def test_quiet_hours_are_absolute(hour):
    d = data(last_session_date=date(2026, 6, 1), ignored_nudges=GIVE_UP_NUDGES)  # very overdue
    assert decide_checkin(at(date(2026, 7, 16), hour=hour), d).action == "none"

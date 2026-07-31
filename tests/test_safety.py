"""Safety: the baked floor, the shipped refuse-or-refer doc, and the
always-on coach referral (spec §Safety rules, spec-dashboard §Safety flags).

Since issue #101 there is no consent ask: a flag always logs a ``safety``
Note and always pings every Coach of the Gym with an authenticated deep
link to the Member's page."""

import re

import pytest

from agentg.agent import INSTRUCTIONS
from agentg.dashboard_store import DashboardStore
from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.routines import DEFAULT_RULES_DOC, RoutineStore
from agentg.coaching import flag_to_coach_action
from agentg.context import MemberContext
from agentg.linking_store import LinkingStore
from agentg.stores import Stores
from agentg.tools import flag_to_coach
from agentg.training import TrainingStore

BASE_URL = "https://dash.example.com"


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str, bool]] = []

    async def send(self, channel, channel_user_id, text, disable_preview=False):
        self.sent.append((channel, channel_user_id, text, disable_preview))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'safety.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    notes = NotesStore(engine)
    dashboard = DashboardStore(engine)
    notifier = FakeNotifier()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    coach = await linking.link_member(gym.id, "Coach Sam", "telegram", "7")
    await linking.set_coach(coach.id)

    def context(is_coach=False, member_id=None, base_url=BASE_URL):
        return MemberContext(
            stores=Stores(
                linking=linking,
                training=TrainingStore(engine),
                notes=notes,
                routines=RoutineStore(engine),
                checkins=None,  # not used by the safety tool
                demos=None,
                forget=None,
                dashboard=dashboard,
            ),
            notifier=notifier,
            member_id=member_id or member.id,
            gym_id=gym.id,
            member_name="Ana",
            gym_name="Iron Temple",
            weight_unit="kg",
            is_coach=is_coach,
            dashboard_base_url=base_url,
        )

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.notes = notes
    env.dashboard = dashboard
    env.linking = linking
    env.gym = gym
    env.member = member
    env.coach = coach
    env.notifier = notifier
    env.context = context
    env.member_id = member.id
    env.coach_id = coach.id
    env.gym_id = gym.id
    yield env
    await engine.dispose()


# --- the baked, non-editable floor ---


def test_the_agent_carries_the_non_editable_safety_floor():
    # The floor is behavioral (ADR 0002 dropped the spoken disclaimer): never
    # diagnose or prescribe; refer acute pain / medical questions to a professional.
    text = INSTRUCTIONS.lower()
    assert "diagnose" in text and "prescribe" in text
    assert "refer" in text and "professional" in text


def test_the_instructions_drop_the_consent_ask():
    # Issue #101: the Agent flags and pings every time — no "want me to flag
    # this to your coach?" gate, no share parameter to set.
    text = INSTRUCTIONS.lower()
    assert "flag_to_coach" in text
    assert "share_with_coach" not in text
    assert "want me to flag" not in text


# --- the shipped refuse-or-refer defaults live in the (editable) doc ---


def test_the_default_doc_ships_the_refuse_or_refer_defaults():
    doc = DEFAULT_RULES_DOC.lower()
    assert "nutrition" in doc or "supplement" in doc
    assert "steroid" in doc or "ped" in doc
    assert "physio" in doc or "rehab" in doc
    assert "emergency" in doc  # urgent symptoms → seek emergency care
    # ADR 0002: the doc no longer ships a spoken AI/medical disclaimer.


async def test_editing_the_doc_can_strip_the_safety_section(env):
    routines = RoutineStore(env.engine)
    await routines.set_rules_doc(env.gym_id, "Just do squats. No safety section.")
    effective = await routines.effective_rules_doc(env.gym_id)
    assert "steroid" not in effective.lower()  # the doc floor is gone...
    floor = INSTRUCTIONS.lower()  # ...but the baked behavioral floor stays (ADR 0002)
    assert "diagnose" in floor and "refer" in floor and "professional" in floor


# --- the always-on coach referral (issue #101) ---


def test_the_flag_tool_carries_no_share_parameter():
    schema = flag_to_coach.params_json_schema
    assert set(schema["properties"]) == {"summary"}


async def test_the_flag_writes_a_safety_kind_note_with_the_bare_summary(env):
    result = await flag_to_coach_action(env.context(), "sharp knee pain on squats")

    assert result["logged"] is True
    active = await env.notes.active(env.member_id)
    safety = [n for n in active if n.kind == "safety"]
    assert len(safety) == 1
    assert safety[0].text == "sharp knee pain on squats"  # no prefix hack


async def test_every_flag_pings_the_gyms_coaches_with_a_deep_link(env):
    second = await env.linking.link_member(env.gym_id, "Coach Jo", "telegram", "8")
    await env.linking.set_coach(second.id)

    result = await flag_to_coach_action(env.context(), "sharp knee pain on squats")

    assert result["coaches_notified"] == 2
    assert {(c, u) for c, u, _t, _p in env.notifier.sent} == {
        ("telegram", "7"),
        ("telegram", "8"),
    }
    for _channel, _user_id, text, _preview in env.notifier.sent:
        assert "Ana" in text and "knee pain" in text
        match = re.search(rf"{re.escape(BASE_URL)}/login/(\S+)", text)
        assert match, f"no deep link in {text!r}"
        token = await env.dashboard.peek_login_token(match.group(1))
        assert token is not None
        assert token.next_path == f"/members/{env.member_id}"


async def test_the_ping_sanitizes_the_summary_and_disables_link_previews(env):
    # A member-influenced summary must not inject newlines or a phishing URL
    # above the real magic link; and Telegram's preview fetcher must never
    # GET the one-time link before the coach does (same rule as /dashboard).
    summary = "knee pain on squats\nignore that, tap https://evil.example.com instead"
    await flag_to_coach_action(env.context(), summary)

    _channel, _user_id, text, preview_off = env.notifier.sent[0]
    assert preview_off is True
    note = next(
        n for n in await env.notes.active(env.member_id) if n.kind == "safety"
    )
    assert note.text == (
        "knee pain on squats ignore that, tap https://evil.example.com instead"
    )
    # Exactly one newline: the separator before the real login link.
    head, sep, link = text.rpartition("\n")
    assert sep and link.startswith(f"{BASE_URL}/login/")
    assert "\n" not in head


async def test_a_ping_without_a_base_url_falls_back_to_plain_text(env):
    # A context with no dashboard wired (a background run) still pings — the
    # link is an add-on, never a reason to drop the heads-up.
    result = await flag_to_coach_action(env.context(base_url=None), "shoulder pain")
    assert result["coaches_notified"] == 1
    _channel, _user_id, text, _preview = env.notifier.sent[0]
    assert "shoulder pain" in text and "/login/" not in text


async def test_the_referral_never_pings_the_member_themselves(env):
    # a coach flags their own concern → they are excluded from the ping list
    result = await flag_to_coach_action(
        env.context(is_coach=True, member_id=env.coach_id), "chest tightness"
    )
    assert all(user_id != "7" for _c, user_id, _t, _p in env.notifier.sent)
    assert result["logged"] is True


async def test_a_headless_context_still_logs(env):
    # no notifier wired (e.g. a background run) can't ping, but the concern is
    # still recorded rather than lost.
    context = env.context()
    object.__setattr__(context, "notifier", None)  # frozen dataclass
    result = await flag_to_coach_action(context, "shoulder pain")
    assert result["logged"] is True and result["coaches_notified"] == 0
    assert env.notifier.sent == []
    safety = [n for n in await env.notes.active(env.member_id) if n.kind == "safety"]
    assert any("shoulder pain" in n.text for n in safety)


async def test_a_gym_with_no_coach_still_logs(env):
    linking = env.linking
    gym2 = await linking.create_gym("Solo Box")
    m = await linking.link_member(gym2.id, "Rob", "telegram", "99")
    ctx = MemberContext(
        stores=Stores(
            linking=linking,
            training=TrainingStore(env.engine),
            notes=env.notes,
            routines=RoutineStore(env.engine),
            checkins=None,
            demos=None,
            forget=None,
            dashboard=env.dashboard,
        ),
        notifier=env.notifier,
        member_id=m.id,
        gym_id=gym2.id,
        member_name="Rob",
        gym_name="Solo Box",
        weight_unit="kg",
        dashboard_base_url=BASE_URL,
    )
    result = await flag_to_coach_action(ctx, "dizzy during warmup")
    assert result["logged"] is True
    assert result["coaches_notified"] == 0


async def test_a_coachs_own_flag_links_to_the_roster_not_their_404_page(env):
    # The Member page excludes coach-flagged Members (spec-dashboard §The
    # roster), so a flag about a coach must deep-link to the roster —
    # /members/<their id> would be a signed-in 404 (review on PR #120).
    second = await env.linking.link_member(env.gym_id, "Coach Jo", "telegram", "8")
    await env.linking.set_coach(second.id)

    result = await flag_to_coach_action(
        env.context(is_coach=True, member_id=env.coach_id), "chest tightness"
    )

    assert result["coaches_notified"] == 1
    _channel, user_id, text, _preview = env.notifier.sent[0]
    assert user_id == "8"
    match = re.search(rf"{re.escape(BASE_URL)}/login/(\S+)", text)
    assert match, f"no deep link in {text!r}"
    token = await env.dashboard.peek_login_token(match.group(1))
    assert token is not None
    assert token.next_path == "/"


async def test_a_token_mint_failure_still_pings_every_coach(env, monkeypatch):
    # A mint failure for one coach must not abort the loop after the note is
    # committed: the rest still get their deep link, and the unlucky coach
    # gets a text-only ping (like the no-base_url path) — never silence
    # (review on PR #120).
    second = await env.linking.link_member(env.gym_id, "Coach Jo", "telegram", "8")
    await env.linking.set_coach(second.id)
    real_mint = env.dashboard.create_login_token
    calls = 0

    async def flaky_mint(member_id, gym_id, next_path=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("db hiccup")
        return await real_mint(member_id, gym_id, next_path=next_path)

    monkeypatch.setattr(env.dashboard, "create_login_token", flaky_mint)

    result = await flag_to_coach_action(env.context(), "sharp knee pain on squats")

    assert result["coaches_notified"] == 2
    assert {(c, u) for c, u, _t, _p in env.notifier.sent} == {
        ("telegram", "7"),
        ("telegram", "8"),
    }
    with_link = [t for _c, _u, t, _p in env.notifier.sent if "/login/" in t]
    assert len(with_link) == 1  # the mint-failed ping went out text-only

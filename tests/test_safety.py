"""Safety: the baked floor, the shipped refuse-or-refer doc, and the
consent-gated coach referral (spec §Safety rules)."""

import pytest

from agentg.agent import INSTRUCTIONS
from agentg.db import create_engine
from agentg.forget import ForgetStore
from agentg.notes import NotesStore
from agentg.routines import DEFAULT_RULES_DOC, RoutineStore
from agentg.coaching import flag_to_coach_action
from agentg.context import MemberContext
from agentg.store import LinkingStore
from agentg.stores import Stores
from agentg.training import TrainingStore


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, channel, channel_user_id, text):
        self.sent.append((channel, channel_user_id, text))


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'safety.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    notes = NotesStore(engine)
    notifier = FakeNotifier()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")
    coach = await linking.link_member(gym.id, "Coach Sam", "telegram", "7")
    await linking.set_coach(coach.id)

    def context(is_coach=False, member_id=None):
        return MemberContext(
            stores=Stores(
                linking=linking,
                training=TrainingStore(engine),
                notes=notes,
                routines=RoutineStore(engine),
                checkins=None,  # not used by the safety tool
                demos=None,
                forget=None,
            ),
            notifier=notifier,
            member_id=member_id or member.id,
            gym_id=gym.id,
            member_name="Ana",
            gym_name="Iron Temple",
            weight_unit="kg",
            is_coach=is_coach,
        )

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.notes = notes
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


# --- consent-gated coach referral ---


async def test_declining_still_logs_the_concern_but_pings_no_one(env):
    result = await flag_to_coach_action(env.context(), "sharp knee pain on squats", share=False)

    assert result["logged"] is True
    assert result["coaches_notified"] == 0
    assert env.notifier.sent == []
    active = await env.notes.active(env.member_id)
    assert any("knee pain" in n.text for n in active)


async def test_consent_pings_the_gyms_coach_and_logs(env):
    result = await flag_to_coach_action(env.context(), "sharp knee pain on squats", share=True)

    assert result["logged"] is True
    assert result["coaches_notified"] == 1
    assert len(env.notifier.sent) == 1
    channel, user_id, text = env.notifier.sent[0]
    assert (channel, user_id) == ("telegram", "7")  # the coach's chat
    assert "Ana" in text and "knee pain" in text
    assert any("knee pain" in n.text for n in await env.notes.active(env.member_id))


async def test_the_referral_never_pings_the_member_themselves(env):
    # a coach flags their own concern → they are excluded from the ping list
    result = await flag_to_coach_action(
        env.context(is_coach=True, member_id=env.coach_id), "chest tightness", share=True
    )
    assert all(user_id != "7" for _c, user_id, _t in env.notifier.sent)
    assert result["logged"] is True


async def test_consent_still_logs_when_no_channel_notifier_is_wired(env):
    # a headless context (no notifier, e.g. a background run) can't ping, but
    # the concern is still recorded rather than lost.
    context = env.context()
    object.__setattr__(context, "notifier", None)  # frozen dataclass
    result = await flag_to_coach_action(context, "shoulder pain", share=True)
    assert result["logged"] is True and result["coaches_notified"] == 0
    assert env.notifier.sent == []
    assert any("shoulder pain" in n.text for n in await env.notes.active(env.member_id))


async def test_consent_with_no_coach_set_up_still_logs(env):
    # a gym with no coach: consent given, but nobody to ping
    linking = LinkingStore(env.engine)
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
        ),
        notifier=env.notifier,
        member_id=m.id,
        gym_id=gym2.id,
        member_name="Rob",
        gym_name="Solo Box",
        weight_unit="kg",
    )
    result = await flag_to_coach_action(ctx, "dizzy during warmup", share=True)
    assert result["logged"] is True
    assert result["coaches_notified"] == 0

"""Safety: the baked floor, the shipped refuse-or-refer doc, and the
always-on coach referral (spec §Safety rules, spec-dashboard §Safety flags).

Since issue #101 there is no consent ask: a flag always logs a ``safety``
Note and always pings every Coach of the Gym with an authenticated deep
link to the Member's page."""

import asyncio
import re
import time
from types import SimpleNamespace

import pytest

import agentg.runtime as runtime_module
from agentg.agent import INSTRUCTIONS
from agentg.dashboard import DashboardDoor
from agentg.dashboard_store import DashboardStore
from agentg.db import create_engine
from agentg.notes import NotesStore
from agentg.routines import DEFAULT_RULES_DOC, RoutineStore
from agentg.coaching import flag_to_coach_action
from agentg.context import MemberContext
from agentg.linking import Linking
from agentg.linking_store import LinkingStore
from agentg.messages import IncomingMessage
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from agentg.tools import flag_to_coach
from agentg.training import TrainingStore
from conftest import unused_phraser

BASE_URL = "https://dash.example.com"


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str, bool, bool]] = []

    async def send(
        self, channel, channel_user_id, text, disable_preview=False, protect_content=False
    ):
        self.sent.append((channel, channel_user_id, text, disable_preview, protect_content))


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

    ctx = env.context()
    result = await flag_to_coach_action(ctx, "sharp knee pain on squats")

    assert result["coaches_to_notify"] == 2
    await _flush_pings(ctx)
    by_coach: dict[str, list[tuple[str, bool, bool]]] = {}
    for _channel, user_id, text, preview, protect in env.notifier.sent:
        by_coach.setdefault(user_id, []).append((text, preview, protect))
    assert set(by_coach) == {"7", "8"}
    for messages in by_coach.values():
        # Two messages: the heads-up, then the one-time link on its own.
        heads_up, link = messages
        assert "Ana" in heads_up[0] and "knee pain" in heads_up[0]
        assert "/login/" not in heads_up[0]
        match = re.fullmatch(rf"{re.escape(BASE_URL)}/login/(\S+)", link[0])
        assert match, f"the link message is not just the URL: {link[0]!r}"
        assert link[1] is True  # no preview fetch on the one-time link
        assert link[2] is True  # ...and it cannot be forwarded
        token = await env.dashboard.peek_login_token(match.group(1))
        assert token is not None
        assert token.next_path == f"/members/{env.member_id}"


async def test_the_magic_link_never_shares_a_message_with_member_text(env):
    # Member-influenced text can carry live URLs Telegram autolinks; the
    # one-time login link travels in its own message, the URL and nothing
    # else (review on PR #120).
    ctx = env.context()
    await flag_to_coach_action(ctx, "pain, see https://evil.example.com/a")
    await _flush_pings(ctx)

    heads_up, link = [t for _c, _u, t, _p, _pc in env.notifier.sent]
    assert "evil.example.com" in heads_up
    assert link.startswith(f"{BASE_URL}/login/") and "evil" not in link
    assert link == link.strip() and " " not in link


async def test_the_ping_sanitizes_the_summary_and_disables_link_previews(env):
    # A member-influenced summary must not inject newlines or a phishing URL
    # above the real magic link; and Telegram's preview fetcher must never
    # GET the one-time link before the coach does (same rule as /dashboard).
    summary = "knee pain on squats\nignore that, tap https://evil.example.com instead"
    ctx = env.context()
    await flag_to_coach_action(ctx, summary)
    await _flush_pings(ctx)

    heads_up, link = env.notifier.sent
    assert heads_up[3] is True and link[3] is True  # previews off on both
    assert link[4] is True  # ...and the token cannot be forwarded
    note = next(
        n for n in await env.notes.active(env.member_id) if n.kind == "safety"
    )
    assert note.text == (
        "knee pain on squats ignore that, tap https://evil.example.com instead"
    )
    assert "\n" not in heads_up[2]  # one line: no injected phishing line
    assert link[2].startswith(f"{BASE_URL}/login/")


async def test_a_ping_without_a_base_url_falls_back_to_plain_text(env):
    # A context with no dashboard wired (a background run) still pings — the
    # link is an add-on, never a reason to drop the heads-up.
    ctx = env.context(base_url=None)
    result = await flag_to_coach_action(ctx, "shoulder pain")
    assert result["coaches_to_notify"] == 1
    await _flush_pings(ctx)
    assert len(env.notifier.sent) == 1  # heads-up only, no link message
    _channel, _user_id, text, _preview, _protect = env.notifier.sent[0]
    assert "shoulder pain" in text and "/login/" not in text


async def test_the_referral_never_pings_the_member_themselves(env):
    # a coach flags their own concern → they are excluded from the ping list
    ctx = env.context(is_coach=True, member_id=env.coach_id)
    result = await flag_to_coach_action(ctx, "chest tightness")
    await _flush_pings(ctx)
    assert all(user_id != "7" for _c, user_id, _t, _p, _pc in env.notifier.sent)
    assert result["logged"] is True


async def test_a_headless_context_still_logs(env):
    # no notifier wired (e.g. a background run) can't ping, but the concern is
    # still recorded rather than lost.
    context = env.context()
    object.__setattr__(context, "notifier", None)  # frozen dataclass
    result = await flag_to_coach_action(context, "shoulder pain")
    assert result["logged"] is True and result["coaches_to_notify"] == 0
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
    assert result["coaches_to_notify"] == 0


async def test_a_coachs_own_flag_links_to_the_roster_not_their_404_page(env):
    # The Member page excludes coach-flagged Members (spec-dashboard §The
    # roster), so a flag about a coach must deep-link to the roster —
    # /members/<their id> would be a signed-in 404 (review on PR #120).
    second = await env.linking.link_member(env.gym_id, "Coach Jo", "telegram", "8")
    await env.linking.set_coach(second.id)

    ctx = env.context(is_coach=True, member_id=env.coach_id)
    result = await flag_to_coach_action(ctx, "chest tightness")

    assert result["coaches_to_notify"] == 1
    await _flush_pings(ctx)
    _channel, user_id, text, _preview, _protect = env.notifier.sent[-1]  # the link message
    assert user_id == "8"
    match = re.fullmatch(rf"{re.escape(BASE_URL)}/login/(\S+)", text)
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

    ctx = env.context()
    result = await flag_to_coach_action(ctx, "sharp knee pain on squats")

    # Pings are deferred — flush them before checking.
    assert result["coaches_to_notify"] == 2
    await _flush_pings(ctx)
    heads = [m for m in env.notifier.sent if "/login/" not in m[2]]
    links = [m for m in env.notifier.sent if "/login/" in m[2]]
    assert {(m[0], m[1]) for m in heads} == {("telegram", "7"), ("telegram", "8")}
    assert len(links) == 1  # the mint-failed ping went out text-only


async def _flush_pings(ctx):
    """Await every deferred coach ping on the context so tests can inspect
    the notifier output without going through the after_send path."""
    if ctx.coach_pings:
        await asyncio.gather(*[p() for p in ctx.coach_pings])


class TimedFakeNotifier:
    """A FakeNotifier that sleeps briefly per send and tracks the peak number
    of concurrent in-flight sends — a structural proof of concurrency that
    does not depend on wall-clock thresholds."""

    def __init__(self, delay: float = 0.05, barrier: asyncio.Barrier | None = None):
        self.sent: list[tuple[float, float, str, str, str, bool, bool]] = []
        self._delay = delay
        self._barrier = barrier
        self._lock = asyncio.Lock()
        self._in_flight = 0
        self.max_concurrent = 0

    async def send(
        self, channel, channel_user_id, text, disable_preview=False, protect_content=False
    ):
        # Wait at the barrier so every concurrent _ping_one task enters
        # its first send simultaneously, making the concurrency proof
        # structural rather than wall-clock-dependent (P3 #5153518002).
        if self._barrier is not None:
            await self._barrier.wait()
        async with self._lock:
            self._in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self._in_flight)
        start = time.monotonic()
        try:
            await asyncio.sleep(self._delay)
        finally:
            async with self._lock:
                self._in_flight -= 1
        async with self._lock:
            self.sent.append((start, time.monotonic(), channel, channel_user_id, text, disable_preview, protect_content))


# ---------------------------------------------------------------------------
# Issue #172 — deferred coach pings (Member's reply first, coaches after)
# ---------------------------------------------------------------------------


async def test_member_reply_is_delivered_before_coach_notifications(env):
    """The safety note is logged synchronously, but coach pings are deferred
    so the Member's reply can be delivered first (AC #1, #2)."""
    ctx = env.context()
    result = await flag_to_coach_action(ctx, "sharp knee pain on squats")

    # AC #2: the note is logged synchronously and the tool truthfully reports it.
    assert result["logged"] is True
    assert result["coaches_to_notify"] == 1
    active = await env.notes.active(env.member_id)
    safety = [n for n in active if n.kind == "safety"]
    assert len(safety) == 1

    # AC #1: pings are NOT sent yet — they are deferred past the reply.
    assert env.notifier.sent == []

    # Flush and verify the pings actually happen.
    await _flush_pings(ctx)
    assert len(env.notifier.sent) == 2  # heads-up + magic link


async def test_every_coach_still_notified_with_magic_link(env):
    """After flushing deferred pings, every Coach receives a heads-up and a
    magic link in its own protected message (AC #3)."""
    second = await env.linking.link_member(env.gym_id, "Coach Jo", "telegram", "8")
    await env.linking.set_coach(second.id)

    ctx = env.context()
    result = await flag_to_coach_action(ctx, "sharp knee pain on squats")
    assert result["coaches_to_notify"] == 2
    assert env.notifier.sent == []  # deferred, not sent yet

    await _flush_pings(ctx)

    # Each of 2 coaches gets 2 messages (heads-up + link).
    by_coach: dict[str, list[tuple[str, bool, bool]]] = {}
    for _channel, user_id, text, preview, protect in env.notifier.sent:
        by_coach.setdefault(user_id, []).append((text, preview, protect))
    assert set(by_coach) == {"7", "8"}
    for messages in by_coach.values():
        assert len(messages) == 2
        heads_up, link = messages
        assert "Ana" in heads_up[0] and "knee pain" in heads_up[0]
        assert "/login/" not in heads_up[0]
        match = re.fullmatch(rf"{re.escape(BASE_URL)}/login/(\S+)", link[0])
        assert match, f"the link message is not just the URL: {link[0]!r}"
        assert link[1] is True  # no preview fetch on the one-time link
        assert link[2] is True  # ...and it cannot be forwarded


async def test_failure_notifying_one_coach_does_not_prevent_others(env):
    """A failure notifying one Coach does not prevent the others and does
    not affect the Member (AC #4)."""
    second = await env.linking.link_member(env.gym_id, "Coach Jo", "telegram", "8")
    await env.linking.set_coach(second.id)

    # Use a notifier that fails for coach 7 but succeeds for coach 8.
    failing_notifier = FailingForOneNotifier("7")
    ctx = env.context()
    object.__setattr__(ctx, "notifier", failing_notifier)

    result = await flag_to_coach_action(ctx, "sharp knee pain on squats")
    # The note is still logged regardless. The Member is unaffected.
    assert result["logged"] is True
    assert result["coaches_to_notify"] == 2

    await _flush_pings(ctx)

    # Coach 7's heads-up failed; Coach 8 still got everything.
    by_coach: dict[str, list[str]] = {}
    for _channel, user_id, text, _preview, _protect in failing_notifier.sent:
        by_coach.setdefault(user_id, []).append(text)
    assert "7" not in by_coach  # the failing coach got nothing
    assert "8" in by_coach
    assert len(by_coach["8"]) == 2  # heads-up + link


async def test_coach_pings_run_concurrently(env):
    """Coach pings go out concurrently rather than one after another (AC #5)."""
    second = await env.linking.link_member(env.gym_id, "Coach Jo", "telegram", "8")
    await env.linking.set_coach(second.id)
    third = await env.linking.link_member(env.gym_id, "Coach Max", "telegram", "9")
    await env.linking.set_coach(third.id)

    # A Barrier gates every send so the concurrency proof is structural —
    # all three _ping_one tasks enter their first send at the same time,
    # regardless of how long create_login_token takes (P3 #5153518002).
    barrier = asyncio.Barrier(3)
    timed = TimedFakeNotifier(delay=0.05, barrier=barrier)
    ctx = env.context()
    object.__setattr__(ctx, "notifier", timed)

    await flag_to_coach_action(ctx, "sharp knee pain")
    await _flush_pings(ctx)

    # All sends should overlap if truly concurrent — with 3 coaches
    # (3 concurrent _ping_one calls), at least 3 sends should be in-flight
    # at the same time.  Each _ping_one does two sequential sends, so the
    # first send of every coach overlaps before any second send starts.
    assert timed.max_concurrent >= 3, (
        f"max concurrent sends was {timed.max_concurrent}, "
        "expected >= 3 for concurrent coach pings"
    )
    assert len(timed.sent) == 6  # 3 coaches × (heads-up + link)


class FailingForOneNotifier:
    """A FakeNotifier that raises for a specific channel_user_id."""

    def __init__(self, failing_id: str):
        self.sent: list[tuple[str, str, str, bool, bool]] = []
        self._failing_id = failing_id

    async def send(
        self, channel, channel_user_id, text, disable_preview=False, protect_content=False
    ):
        if channel_user_id == self._failing_id:
            raise RuntimeError("simulated send failure")
        self.sent.append((channel, channel_user_id, text, disable_preview, protect_content))


# ---------------------------------------------------------------------------
# Runtime-level test — pings are deferred past the reply (P2 #5153517044)
# ---------------------------------------------------------------------------


@pytest.fixture
async def runtime_env(tmp_path):
    """A minimal AgentRuntime with a linked member, two coaches, and a
    FakeNotifier so we can observe the ping ordering at the handle_message
    level."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'rt_safety.db'}")
    stores = Stores.from_engine(engine)
    dashboard = DashboardStore(engine)
    notifier = FakeNotifier()
    runtime = AgentRuntime(
        agent=object(),  # replaced per test via monkeypatch
        # These tests drive the blocking path (they monkeypatch Runner.run),
        # so streaming is off -- the #176 convention across the test suite.
        stream_replies=False,
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=None,
        notifier=notifier,
        dashboard=DashboardDoor(
            dashboard, BASE_URL
        ),
    )
    await runtime.ensure_schema()
    gym = await stores.linking.create_gym("Iron Temple")
    member = await stores.linking.link_member(gym.id, "Dani", "telegram", "42")
    coach = await stores.linking.link_member(gym.id, "Coach Sam", "telegram", "7")
    await stores.linking.set_coach(coach.id)
    coach2 = await stores.linking.link_member(gym.id, "Coach Jo", "telegram", "8")
    await stores.linking.set_coach(coach2.id)

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.runtime = runtime
    env.notifier = notifier
    env.stores = stores
    env.gym_id = gym.id
    env.member_id = member.id
    yield env
    await engine.dispose()


async def test_coach_pings_are_deferred_after_the_member_reply_at_runtime_level(
    runtime_env, monkeypatch
):
    """The reply from handle_message must have notifier.sent == [] before
    after_send() and non-empty only after (P2 #5153517044)."""

    async def fake_run(agent, text, *, session, context=None, run_config=None):
        # Simulate the Agent calling flag_to_coach, which populates
        # context.coach_pings.
        await flag_to_coach_action(context, "sharp knee pain")
        return SimpleNamespace(final_output="I've logged this and will notify your coaches.")

    monkeypatch.setattr(runtime_module.Runner, "run", fake_run)

    reply = await runtime_env.runtime.handle_message(
        IncomingMessage(
            channel="telegram", channel_user_id="42", text="my knee hurts"
        )
    )

    # The reply text is there, but pings are not sent yet.
    assert "coaches" in str(reply)
    assert runtime_env.notifier.sent == [], (
        "coach pings must be deferred past the reply — "
        "notifier.sent should be empty before after_send()"
    )
    # The after_send callable is wired on the Reply.
    assert reply.after_send is not None

    # Now the channel delivers the reply text and calls after_send.
    await reply.after_send()

    # Each of 2 coaches gets 2 messages (heads-up + magic link).
    assert len(runtime_env.notifier.sent) == 4, (
        f"expected 4 messages (2 coaches × 2) after after_send, "
        f"got {len(runtime_env.notifier.sent)}"
    )

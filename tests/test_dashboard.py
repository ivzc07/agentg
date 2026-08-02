"""The dashboard door: login tokens, the /dashboard command, session cookies.

Store-level and chat-side behavior; the HTTP flow lives in
test_dashboard_web.py.
"""

import hashlib
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import agentg.runtime as runtime_module
from agentg.dashboard import DashboardDoor, is_dashboard_command
from agentg.dashboard_store import TOKEN_TTL, DashboardStore
from agentg.dashboard_web import SESSION_TTL, sign_session, verify_session
from agentg.db import create_engine
from agentg.linking import Linking
from agentg.linking_store import LinkingStore
from agentg.messages import IncomingMessage, Reply
from agentg.models import DashboardLoginToken
from agentg.runtime import AgentRuntime
from agentg.stores import Stores
from conftest import FakeClock, unused_phraser

SECRET = "test-secret"


@pytest.fixture
async def engine(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'domain.db'}")
    yield engine
    await engine.dispose()


@pytest.fixture
async def stores(engine):
    stores = Stores.from_engine(engine)
    await stores.linking.ensure_schema()
    return stores


async def make_coach(linking: LinkingStore, name="Ana", user_id="42", is_coach=True):
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, name, "telegram", user_id)
    if is_coach:
        await linking.set_coach(member.id, True)
    linked = await linking.identity_for("telegram", user_id)
    assert linked is not None
    return linked


# --- login tokens ---------------------------------------------------------


async def test_issued_token_redeems_exactly_once(stores):
    linked = await make_coach(stores.linking)
    raw = await stores.dashboard.create_login_token(linked.member.id, linked.gym.id)

    token = await stores.dashboard.redeem_login_token(raw)
    assert token is not None
    assert token.member_id == linked.member.id and token.gym_id == linked.gym.id
    assert token.used_at is not None

    assert await stores.dashboard.redeem_login_token(raw) is None  # single-use
    assert await stores.dashboard.peek_login_token(raw) is None


async def test_only_the_token_hash_is_stored(stores):
    linked = await make_coach(stores.linking)
    raw = await stores.dashboard.create_login_token(linked.member.id, linked.gym.id)

    async with stores.dashboard._sessions() as db:
        rows = (await db.scalars(select(DashboardLoginToken))).all()
    assert len(rows) == 1
    assert rows[0].token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in rows[0].token_hash


async def test_unknown_and_empty_tokens_do_not_redeem(stores):
    assert await stores.dashboard.redeem_login_token("no-such-token") is None
    assert await stores.dashboard.peek_login_token("no-such-token") is None
    assert await stores.dashboard.redeem_login_token("") is None


async def test_expired_tokens_do_not_redeem(engine):
    clock = FakeClock()
    linking = LinkingStore(engine)
    store = DashboardStore(engine, clock=clock)
    await linking.ensure_schema()
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Ana", "telegram", "42")

    raw = await store.create_login_token(member.id, gym.id)
    clock.advance(TOKEN_TTL + timedelta(seconds=1))

    assert await store.peek_login_token(raw) is None
    assert await store.redeem_login_token(raw) is None


async def test_peek_does_not_spend_the_token(stores):
    linked = await make_coach(stores.linking)
    raw = await stores.dashboard.create_login_token(linked.member.id, linked.gym.id)

    assert await stores.dashboard.peek_login_token(raw) is not None
    assert await stores.dashboard.redeem_login_token(raw) is not None


# --- per-request coach check ----------------------------------------------


async def test_coach_identity_requires_the_flag_and_the_gym(stores):
    linked = await make_coach(stores.linking)
    assert await stores.dashboard.coach_identity(linked.member.id, linked.gym.id) is not None

    await stores.linking.set_coach(linked.member.id, False)  # demoted
    assert await stores.dashboard.coach_identity(linked.member.id, linked.gym.id) is None


async def test_coach_identity_rejects_cross_gym_and_unknown_members(stores):
    linked = await make_coach(stores.linking)
    other_gym = await stores.linking.create_gym("Steel Yard")
    assert await stores.dashboard.coach_identity(linked.member.id, other_gym.id) is None
    assert await stores.dashboard.coach_identity(99999, linked.gym.id) is None


# --- session cookies -------------------------------------------------------


def test_session_cookie_round_trips():
    clock = FakeClock()
    value = sign_session(7, 3, SECRET, clock())
    assert verify_session(value, SECRET, clock()) == (7, 3)


def test_session_cookie_survives_until_the_horizon():
    clock = FakeClock()
    value = sign_session(7, 3, SECRET, clock())
    clock.advance(SESSION_TTL - timedelta(seconds=1))
    assert verify_session(value, SECRET, clock()) == (7, 3)
    clock.advance(timedelta(seconds=2))
    assert verify_session(value, SECRET, clock()) is None


@pytest.mark.parametrize(
    "value",
    [
        "7:3:9999999999:deadbeef",  # bad signature
        "garbage",
        "7:3:not-a-timestamp:" + "0" * 64,
        "",
    ],
)
def test_tampered_or_malformed_cookies_do_not_verify(value):
    assert verify_session(value, SECRET, FakeClock()()) is None


def test_a_cookie_signed_with_another_secret_does_not_verify():
    value = sign_session(7, 3, "other-secret", FakeClock()())
    assert verify_session(value, SECRET, FakeClock()()) is None


# --- the /dashboard command ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/dashboard", True),
        ("/dashboard@GymCoachBot", True),
        ("  /dashboard please  ", True),
        ("/dashboards", False),
        ("dashboard", False),
        ("/start dashboard", False),
    ])
def test_dashboard_command_parsing(text, expected):
    assert is_dashboard_command(text) is expected


async def test_a_coach_gets_a_magic_link_with_no_preview(stores):
    linked = await make_coach(stores.linking)
    door = DashboardDoor(stores.dashboard, "https://dash.example.com")

    reply = await door.handle(linked)

    assert reply.disable_preview is True
    assert "Iron Temple" in reply
    url = reply.splitlines()[-1]
    assert url.startswith("https://dash.example.com/login/")
    raw = url.rsplit("/login/", 1)[1]
    assert await stores.dashboard.peek_login_token(raw) is not None


async def test_a_non_coach_is_refused_and_no_token_is_issued(stores):
    linked = await make_coach(stores.linking, is_coach=False)
    door = DashboardDoor(stores.dashboard, "https://dash.example.com")

    reply = await door.handle(linked)

    assert reply.disable_preview is False
    assert "coach" in reply and "/login/" not in reply
    async with stores.dashboard._sessions() as db:
        count = await db.scalar(select(func.count(DashboardLoginToken.id)))
    assert count == 0


async def test_a_group_chat_gets_no_link_and_no_token(stores):
    linked = await make_coach(stores.linking)  # even a coach
    door = DashboardDoor(stores.dashboard, "https://dash.example.com")

    reply = await door.handle(linked, is_group=True)

    assert "/login/" not in reply
    assert "privado" in reply or "directo" in reply  # pointed at the DM
    async with stores.dashboard._sessions() as db:
        count = await db.scalar(select(func.count(DashboardLoginToken.id)))
    assert count == 0


async def test_runtime_routes_the_command_to_the_door_not_the_agent(stores, engine, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(runtime_module.Runner, "run", run)
    runtime = AgentRuntime(
        agent=object(),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, unused_phraser),
        summarizer=AsyncMock(),
        dashboard=DashboardDoor(stores.dashboard, "https://dash.example.com"),
        stream_replies=False,
    )
    await make_coach(stores.linking)

    reply = await runtime.handle_message(
        IncomingMessage(channel="telegram", channel_user_id="42", text="/dashboard")
    )

    assert isinstance(reply, Reply)
    assert "/login/" in reply
    run.assert_not_awaited()  # the Agent never sees the command


async def test_the_factory_wires_the_injected_clock_into_the_dashboard_store(engine):
    """FLAG_EXPIRY is compared against the DashboardStore's clock — a Stores
    built with a fake clock must age flags by fake time, not wall time
    (review on PR #120). The fake clock starts far in the future so a
    wall-clock store can never accidentally agree."""
    from datetime import UTC, datetime

    clock = FakeClock(start=datetime(2099, 1, 1, tzinfo=UTC))
    stores = Stores.from_engine(engine, clock=clock)
    await stores.linking.ensure_schema()
    gym = await stores.linking.create_gym("Iron Temple")
    member = await stores.linking.link_member(gym.id, "Ana", "telegram", "42")
    await stores.notes.remember_safety(member.id, gym.id, "sharp knee pain")

    rows, _ = await stores.dashboard.roster(gym.id)
    assert rows[0].has_safety_flag  # fresh flag marks

    clock.advance(timedelta(days=31))
    rows, _ = await stores.dashboard.roster(gym.id)
    assert not rows[0].has_safety_flag  # 30 days on the fake clock: cleared

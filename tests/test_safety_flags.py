"""Safety flags on the dashboard (issue #101, spec-dashboard §Safety flags).

The Agent's flag travels end to end: a ``safety`` Note marks the roster row
(never re-sorting, injuries never marking), the Member page carries a banner
with a Tick off action that records who and when without retiring the Note,
and an unacknowledged flag ages off the roster 30 days after creation while
the page keeps it labelled "expired, never seen".
"""

from datetime import timedelta

import pytest

from agentg.dashboard_web import SESSION_COOKIE, sign_session
from agentg.notes import NotesStore


@pytest.fixture
async def env(roster_env):
    return roster_env


def _notes(env) -> NotesStore:
    return NotesStore(env.engine, clock=env.clock)


async def _flag(env, member, text="sharp knee pain on squats", days_ago=0):
    """A safety Note as the Agent's flag writes it, optionally backdated."""
    if days_ago:
        now = env.clock.now
        env.clock.now = now - timedelta(days=days_ago)
        try:
            return await _notes(env).remember_safety(member.id, env.gym.id, text)
        finally:
            env.clock.now = now
    return await _notes(env).remember_safety(member.id, env.gym.id, text)


def _cookie(env):
    return {SESSION_COOKIE: sign_session(env.coach.id, env.gym.id, "test-secret", env.clock())}


async def _member_page(env, member_id: int):
    response = await env.client.get(f"/members/{member_id}", cookies=_cookie(env))
    assert response.status == 200
    return await response.text()


# --- the roster marker ---


async def test_a_flag_marks_the_row_without_resorting(env):
    ana = await env.add_member("Ana")
    beto = await env.add_member("Beto")
    cora = await env.add_member("Cora")
    await env.train(ana, days_ago=1)
    await env.train(beto, days_ago=5)
    await env.train(cora, days_ago=3)
    await _flag(env, cora)

    rows, _ = await env.store.roster(env.gym.id)

    assert [row.name for row in rows] == ["Beto", "Cora", "Ana"]  # Gap order holds
    flagged = {row.name: row.has_safety_flag for row in rows}
    assert flagged == {"Beto": False, "Cora": True, "Ana": False}


async def test_an_injury_note_never_marks_the_roster(env):
    member = await env.add_member("Ana")
    await _notes(env).remember(member.id, env.gym.id, "injury", "bad knee since March")

    row = await env.roster_row(member)
    assert not row.has_safety_flag


async def test_a_lapsed_row_keeps_its_flag_marker(env):
    member = await env.add_member("Perdido")
    await env.train(member, days_ago=10)
    await env.checkins.lapse(member.id)
    await _flag(env, member)

    _, lapsed = await env.store.roster(env.gym.id)
    assert [row.name for row in lapsed] == ["Perdido"]
    assert lapsed[0].has_safety_flag


async def test_a_ticked_flag_clears_the_marker(env):
    member = await env.add_member("Ana")
    note = await _flag(env, member)
    await env.store.acknowledge_flag(env.gym.id, member.id, note.id, env.coach.id)

    row = await env.roster_row(member)
    assert not row.has_safety_flag


async def test_a_30_day_old_flag_drops_off_the_roster(env):
    fresh = await env.add_member("Fresh")
    stale = await env.add_member("Stale")
    await _flag(env, fresh, days_ago=29)
    await _flag(env, stale, days_ago=31)

    rows, _ = await env.store.roster(env.gym.id)
    flagged = {row.name: row.has_safety_flag for row in rows}
    assert flagged == {"Fresh": True, "Stale": False}


async def test_the_page_renders_the_marker_on_flagged_rows(env):
    member = await env.add_member("Ana")
    await _flag(env, member)

    html = await env.page()
    assert "tag-flag" in html


# --- the Member page banner and the tick-off ---


async def test_the_member_page_shows_the_open_flag_with_a_tick_off_action(env):
    member = await env.add_member("Ana")
    note = await _flag(env, member)

    html = await _member_page(env, member.id)
    assert "sharp knee pain on squats" in html
    assert f"/members/{member.id}/flags/{note.id}/tick-off" in html


async def test_tick_off_records_who_and_when_and_keeps_the_note_live(env):
    member = await env.add_member("Ana")
    note = await _flag(env, member)

    response = await env.client.post(
        f"/members/{member.id}/flags/{note.id}/tick-off", cookies=_cookie(env)
    )
    assert response.status == 200

    stored = await _notes(env).active(member.id)
    safety = next(n for n in stored if n.kind == "safety")
    assert safety.acknowledged_at == env.clock.now  # who and when...
    assert safety.acknowledged_by_member_id == env.coach.id
    assert safety.retired_at is None  # ...but acknowledging is not retiring:
    assert "knee pain" in safety.text  # the Note stays live for the Agent

    row = await env.roster_row(member)  # the roster marker is silenced
    assert not row.has_safety_flag

    html = await _member_page(env, member.id)  # the page shows who saw it
    assert "Coach Ana" in html


async def test_an_expired_unacknowledged_flag_reads_expired_never_seen(env):
    member = await env.add_member("Ana")
    await _flag(env, member, days_ago=31)

    html = await _member_page(env, member.id)
    assert "caducada, nunca vista" in html


async def test_tick_off_of_a_foreign_gyms_flag_is_the_shared_404(env):
    member = await env.add_member("Ana")
    outsider_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(outsider_gym.id, "Rex", "telegram", "902")
    foreign = await _notes(env).remember_safety(outsider.id, outsider_gym.id, "dizzy")

    response = await env.client.post(
        f"/members/{member.id}/flags/{foreign.id}/tick-off",
        cookies=_cookie(env),
        allow_redirects=False,
    )
    assert response.status == 404
    note = await _notes(env).active(outsider.id)
    assert note[0].acknowledged_at is None


async def test_tick_off_of_another_members_flag_is_the_shared_404(env):
    """Same gym, wrong Member: the member_id predicate is what stops a Coach
    stamping one Member's flag through another's page."""
    ana = await env.add_member("Ana")
    bea = await env.add_member("Bea")
    beas_flag = await _flag(env, bea)

    response = await env.client.post(
        f"/members/{ana.id}/flags/{beas_flag.id}/tick-off",
        cookies=_cookie(env),
        allow_redirects=False,
    )
    assert response.status == 404
    note = next(n for n in await _notes(env).active(bea.id) if n.kind == "safety")
    assert note.acknowledged_at is None and note.acknowledged_by_member_id is None


async def test_tick_off_requires_a_coach_session(env):
    member = await env.add_member("Ana")
    note = await _flag(env, member)

    response = await env.client.post(
        f"/members/{member.id}/flags/{note.id}/tick-off", allow_redirects=False
    )
    assert response.status == 200  # the bounce page, not a tick
    assert "/dashboard" in await response.text()
    stored = await _notes(env).active(member.id)
    assert stored[0].acknowledged_at is None


# --- the deep link ---


async def test_the_flag_deep_link_lands_signed_in_on_the_members_page(env):
    member = await env.add_member("Ana")
    await _flag(env, member)
    raw = await env.store.create_login_token(
        env.coach.id, env.gym.id, next_path=f"/members/{member.id}"
    )

    response = await env.client.post(f"/login/{raw}", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == f"/members/{member.id}"

    cookies = {SESSION_COOKIE: response.cookies[SESSION_COOKIE].value}
    page = await env.client.get(f"/members/{member.id}", cookies=cookies)
    assert page.status == 200
    html = await page.text()
    assert "Ana" in html and "sharp knee pain on squats" in html


async def test_a_magic_link_without_a_next_path_lands_on_the_roster(env):
    raw = await env.store.create_login_token(env.coach.id, env.gym.id)
    response = await env.client.post(f"/login/{raw}", allow_redirects=False)
    assert response.status == 302
    assert response.headers["Location"] == "/"


async def test_tick_off_rejects_a_non_safety_note(env):
    member = await env.add_member("Ana")
    injury = await _notes(env).remember(member.id, env.gym.id, "injury", "bad knee")

    response = await env.client.post(
        f"/members/{member.id}/flags/{injury.id}/tick-off",
        cookies=_cookie(env),
        allow_redirects=False,
    )

    assert response.status == 404
    note = (await _notes(env).active(member.id))[0]
    assert note.acknowledged_at is None and note.acknowledged_by_member_id is None


async def test_tick_off_rejects_a_retired_flag(env):
    member = await env.add_member("Ana")
    note = await _flag(env, member)
    await _notes(env).retire(member.id, note.id)

    response = await env.client.post(
        f"/members/{member.id}/flags/{note.id}/tick-off",
        cookies=_cookie(env),
        allow_redirects=False,
    )

    assert response.status == 404


async def test_tick_off_rejects_an_unknown_note_id(env):
    member = await env.add_member("Ana")

    response = await env.client.post(
        f"/members/{member.id}/flags/999999/tick-off",
        cookies=_cookie(env),
        allow_redirects=False,
    )

    assert response.status == 404


async def test_concurrent_ticks_keep_the_first_stamp(env):
    """A concurrent tick after the first must be the idempotent no-op, even
    at the same clock instant: the ack is one atomic UPDATE guarded on
    ``acknowledged_at IS NULL``, so last-writer-wins is impossible (review
    on PR #120)."""
    import asyncio

    member = await env.add_member("Ana")
    coach2 = await env.linking.link_member(env.gym.id, "Coach Bea", "telegram", "2")
    await env.linking.set_coach(coach2.id)
    note = await _flag(env, member)
    await env.store.acknowledge_flag(env.gym.id, member.id, note.id, env.coach.id)

    # Same instant, two more writers — neither may move the stamp.
    await asyncio.gather(
        env.store.acknowledge_flag(env.gym.id, member.id, note.id, coach2.id),
        env.store.acknowledge_flag(env.gym.id, member.id, note.id, coach2.id),
    )

    stored = next(n for n in await _notes(env).active(member.id) if n.kind == "safety")
    assert stored.acknowledged_by_member_id == env.coach.id
    assert stored.acknowledged_at == env.clock.now


async def test_a_second_tick_keeps_the_first_coaches_stamp(env):
    member = await env.add_member("Ana")
    coach2 = await env.linking.link_member(env.gym.id, "Coach Bea", "telegram", "2")
    await env.linking.set_coach(coach2.id)
    note = await _flag(env, member)
    await env.store.acknowledge_flag(env.gym.id, member.id, note.id, env.coach.id)

    env.clock.advance(timedelta(days=1))
    again = await env.store.acknowledge_flag(env.gym.id, member.id, note.id, coach2.id)

    assert again is not None  # idempotent, not a 404
    stored = next(n for n in await _notes(env).active(member.id) if n.kind == "safety")
    assert stored.acknowledged_by_member_id == env.coach.id
    assert stored.acknowledged_at == env.clock.now - timedelta(days=1)


async def test_tick_off_with_foreign_consistent_ids_is_the_shared_404(env):
    """The URL ids are consistent with the foreign row, so only the gym_id
    predicate can produce this 404 — cross-tenant stamping stays impossible
    (review on PR #120)."""
    outsider_gym = await env.linking.create_gym("Other Gym")
    outsider = await env.linking.link_member(outsider_gym.id, "Rex", "telegram", "902")
    foreign = await _notes(env).remember_safety(outsider.id, outsider_gym.id, "dizzy")

    response = await env.client.post(
        f"/members/{outsider.id}/flags/{foreign.id}/tick-off",
        cookies=_cookie(env),  # our coach, their ids
        allow_redirects=False,
    )

    assert response.status == 404
    note = next(n for n in await _notes(env).active(outsider.id) if n.kind == "safety")
    assert note.acknowledged_at is None and note.acknowledged_by_member_id is None


async def test_an_acknowledged_flag_never_reads_expired(env):
    """Acknowledged wins over expired: once a Coach ticked the flag, aging
    past FLAG_EXPIRY must keep showing who and when — never "caducada,
    nunca vista" (review on PR #120)."""
    member = await env.add_member("Ana")
    note = await _flag(env, member)
    await env.store.acknowledge_flag(env.gym.id, member.id, note.id, env.coach.id)

    env.clock.advance(timedelta(days=31))

    html = await _member_page(env, member.id)
    assert "Vista por Coach Ana" in html
    assert "caducada, nunca vista" not in html


async def test_the_tick_off_form_keeps_the_view_it_was_opened_from(env):
    """A Coach in Split (or Cards) must not bounce to Table after ticking
    off: the form action carries the view, and the POST redirects back to
    it (review on PR #120)."""
    member = await env.add_member("Ana")
    note = await _flag(env, member)

    html = await (
        await env.client.get(f"/members/{member.id}?view=split", cookies=_cookie(env))
    ).text()
    assert f"/members/{member.id}/flags/{note.id}/tick-off?view=split" in html

    response = await env.client.post(
        f"/members/{member.id}/flags/{note.id}/tick-off?view=split",
        cookies=_cookie(env),
        allow_redirects=False,
    )
    assert response.status == 302
    assert response.headers["Location"] == f"/members/{member.id}?view=split"

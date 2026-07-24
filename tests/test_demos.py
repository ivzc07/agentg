"""DemoStore + serve_demo: resolution, the file_id cache, gym overrides."""

import json

import pytest

from agentg.db import create_engine
from agentg.demo_ingest import DemoManifestEntry, ingest_demo_manifest, load_manifest
from agentg.demo_media import SentAnimation, serve_demo
from agentg.demos import DemoStore
from agentg.linking_store import LinkingStore
from agentg.training import TrainingStore


class FakeSender:
    """Records sends and mints a file_id on the first (upload) send only."""

    def __init__(self, namespace="bot-1"):
        self._ns = namespace
        self.calls: list[tuple[str, str, str, str | None]] = []
        self._counter = 0

    @property
    def cache_namespace(self):
        return self._ns

    async def send_animation(self, channel, channel_user_id, slug, cached_file_id):
        self.calls.append((channel, channel_user_id, slug, cached_file_id))
        if cached_file_id is not None:
            return SentAnimation(file_id=cached_file_id)  # resend, no new upload
        self._counter += 1
        return SentAnimation(file_id=f"fileid-{slug}-{self._counter}", file_unique_id="u1")

    @property
    def uploads(self):  # sends where nothing was cached → a real upload
        return [c for c in self.calls if c[3] is None]


@pytest.fixture
async def env(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'demos.db'}")
    linking = LinkingStore(engine)
    await linking.ensure_schema()
    training = TrainingStore(engine)
    await training.ensure_seeded()
    demos = DemoStore(engine)
    gym = await linking.create_gym("Iron Temple")
    member = await linking.link_member(gym.id, "Dani", "telegram", "42")

    class Env:
        pass

    env = Env()
    env.engine = engine
    env.linking = linking
    env.demos = demos
    env.gym_id = gym.id
    env.member_channel = ("telegram", "42")
    yield env
    await engine.dispose()


async def serve(env, sender, exercise):
    return await serve_demo(env.demos, sender, exercise, env.gym_id, *env.member_channel)


# --- resolution + availability ---


async def test_no_demo_when_the_exercise_has_none(env):
    sender = FakeSender()
    assert await serve(env, sender, "bench press") == "no_demo"
    assert sender.calls == []


async def test_a_default_demo_is_resolved_and_sent(env):
    await env.demos.set_default_demo("bench press", "bench-press.mp4")
    ref = await env.demos.resolve("bench press", env.gym_id)
    assert ref is not None and ref.slug == "bench-press.mp4" and ref.gym_id is None


async def test_an_alias_resolves_to_the_default_demo(env):
    await env.demos.set_default_demo("bench press", "bench-press.mp4")
    ref = await env.demos.resolve("bench", env.gym_id)  # "bench" is a seeded alias
    assert ref is not None and ref.slug == "bench-press.mp4"


# --- the file_id cache: upload once, resend after ---


async def test_first_send_uploads_then_later_sends_reuse_the_file_id(env):
    await env.demos.set_default_demo("bench press", "bench-press.mp4")
    sender = FakeSender()

    assert await serve(env, sender, "bench press") == "sent"
    assert len(sender.uploads) == 1  # first send uploaded

    assert await serve(env, sender, "bench press") == "sent"
    assert len(sender.uploads) == 1  # second send reused the cached file_id
    assert sender.calls[1][3] == "fileid-bench-press.mp4-1"  # resent by id


async def test_the_cached_file_id_is_namespaced_per_bot(env):
    await env.demos.set_default_demo("bench press", "bench-press.mp4")
    await serve(env, FakeSender(namespace="bot-1"), "bench press")
    # a different bot has its own cache → must upload again
    other = FakeSender(namespace="bot-2")
    await serve(env, other, "bench press")
    assert len(other.uploads) == 1


async def test_changing_the_default_media_invalidates_the_cache(env):
    await env.demos.set_default_demo("bench press", "old.mp4")
    sender = FakeSender()
    await serve(env, sender, "bench press")  # caches old.mp4's file_id

    await env.demos.set_default_demo("bench press", "new.mp4")  # media changed
    await serve(env, sender, "bench press")
    assert sender.uploads[-1][2] == "new.mp4"  # re-uploaded the new media
    assert len(sender.uploads) == 2


async def test_a_failed_send_is_not_cached(env):
    await env.demos.set_default_demo("bench press", "bench-press.mp4")

    class FailingSender(FakeSender):
        async def send_animation(self, *a):
            return None

    assert await serve(env, FailingSender(), "bench press") == "send_failed"
    assert await env.demos.cached_file_id(
        (await env.demos.resolve("bench press", env.gym_id)).exercise_id, None, "bot-1"
    ) is None


# --- gym overrides win ---


async def test_a_gym_override_is_served_instead_of_the_default(env):
    await env.demos.set_default_demo("squat", "squat-default.mp4")
    await env.demos.set_override(env.gym_id, "squat", "coach-squat.mp4")

    ref = await env.demos.resolve("squat", env.gym_id)
    assert ref is not None
    assert ref.slug == "coach-squat.mp4" and ref.gym_id == env.gym_id


async def test_another_gym_still_gets_the_default(env):
    await env.demos.set_default_demo("squat", "squat-default.mp4")
    await env.demos.set_override(env.gym_id, "squat", "coach-squat.mp4")
    other_gym = await env.linking.create_gym("Steel Yard")

    ref = await env.demos.resolve("squat", other_gym.id)
    assert ref is not None and ref.slug == "squat-default.mp4" and ref.gym_id is None


async def test_override_and_default_caches_are_independent(env):
    await env.demos.set_default_demo("squat", "squat-default.mp4")
    await env.demos.set_override(env.gym_id, "squat", "coach-squat.mp4")
    sender = FakeSender()

    await serve(env, sender, "squat")  # gym override, uploads coach-squat
    other_gym = await env.linking.create_gym("Steel Yard")
    await serve_demo(env.demos, sender, "squat", other_gym.id, "telegram", "9")  # default

    uploaded = {c[2] for c in sender.uploads}
    assert uploaded == {"coach-squat.mp4", "squat-default.mp4"}


# --- ingest populates the catalog and wires demos ---


async def test_ingest_populates_and_wires_the_catalog(env, tmp_path):
    manifest = [
        DemoManifestEntry("goblet squat", "goblet-squat.mp4"),
        DemoManifestEntry("bench press", "bench-press.mp4"),  # an existing seed exercise
    ]
    count = await ingest_demo_manifest(env.demos, manifest)
    assert count == 2

    # a brand-new exercise was created and wired
    ref = await env.demos.resolve("goblet squat", env.gym_id)
    assert ref is not None and ref.slug == "goblet-squat.mp4"
    # the existing seed exercise got its demo without duplicating
    assert (await env.demos.resolve("bench press", env.gym_id)).slug == "bench-press.mp4"


def test_load_manifest_reads_name_slug_json(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps([{"name": "Deadlift", "slug": "deadlift.mp4"}]), encoding="utf-8")
    entries = load_manifest(path)
    assert entries == [DemoManifestEntry("Deadlift", "deadlift.mp4")]

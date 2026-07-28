"""Tests for the demo-media build script (docs/demo-media.md step 2)."""

from __future__ import annotations

import json
from pathlib import Path

from agentg.demo_ingest import load_manifest
from agentg.scripts.build_demo_media import (
    SEED_DEMO_PICKS,
    DatasetRecord,
    PlanItem,
    build_plan,
    index_videos,
    load_records,
    main,
    slug_for,
    slugify,
    transcode_pending,
    write_manifest,
)
from agentg.training import SEED_EXERCISES


def _records(*pairs: tuple[str, str]) -> list[DatasetRecord]:
    return [DatasetRecord(id=rid, name=name) for rid, name in pairs]


def _videos(tmp_path: Path, *ids: str) -> dict[str, Path]:
    videos = tmp_path / "videos"
    videos.mkdir(exist_ok=True)
    found = {}
    for rid in ids:
        gif = videos / f"{rid}-abc1234.gif"
        gif.write_bytes(b"GIF89a")
        found[rid] = gif
    return found


def test_slugify():
    assert slugify("3/4 sit-up") == "3-4-sit-up"
    assert slugify("Cable Lat Pulldown (Full Range)") == "cable-lat-pulldown-full-range"


def test_slug_is_anchored_on_dataset_id():
    record = DatasetRecord(id="0025", name="barbell bench press")
    assert slug_for(record) == "0025-barbell-bench-press.mp4"


def test_seed_picks_cover_exactly_the_seed_catalog():
    assert set(SEED_DEMO_PICKS) == set(SEED_EXERCISES)


def test_seed_records_wire_onto_seed_names_not_dataset_names():
    records = _records(("0025", "barbell bench press"), ("9999", "zebra raise"))
    plan = build_plan(records, {})
    bench = next(i for i in plan.items if i.record_id == "0025")
    assert bench.manifest_name == "bench press"  # the seed row, not a near-duplicate
    assert not any(i.manifest_name == "barbell bench press" for i in plan.items)
    assert plan.seed_wiring["bench press"] == "barbell bench press (0025)"
    # every other seed pick is absent from this tiny fixture, hence unresolved
    assert plan.seed_wiring["squat"] is None


def test_seed_alias_collisions_are_not_emitted():
    # the dataset's "chin-up" is a seeded alias of pull-up; emitting it would
    # resolve onto the pull-up row at ingest and clobber its demo
    records = _records(("0652", "pull-up"), ("1326", "chin-up"))
    plan = build_plan(records, {})
    names = [i.manifest_name for i in plan.items]
    assert "chin-up" not in names
    assert any(s.name == "chin-up" for s in plan.skipped)


def test_duplicate_dataset_names_emit_once_first_wins():
    records = _records(("1000", "lever chest press"), ("2000", "lever chest press"))
    plan = build_plan(records, {"1000": Path("a.gif"), "2000": Path("b.gif")})
    matches = [i for i in plan.items if i.manifest_name == "lever chest press"]
    assert [i.record_id for i in matches] == ["1000"]
    assert any(s.record_id == "2000" and "duplicate" in s.reason for s in plan.skipped)


def test_records_without_a_gif_are_skipped_not_planned():
    records = _records(("0001", "3/4 sit-up"))
    plan = build_plan(records, videos={})
    assert plan.items == []
    assert plan.skipped[0].reason == "no GIF in dataset videos/"


async def test_transcode_skips_existing_and_renames_tmp_into_place(tmp_path):
    videos = _videos(tmp_path, "0001", "0002", "0003")
    items = [
        PlanItem("0001", "3/4 sit-up", "0001-3-4-sit-up.mp4"),
        PlanItem("0002", "crunch", "0002-crunch.mp4"),
        PlanItem("0003", "plank", "0003-plank.mp4"),
    ]
    (tmp_path / "0001-3-4-sit-up.mp4").write_bytes(b"already done")
    (tmp_path / "0003-plank.mp4").write_bytes(b"")  # empty = corrupt, redo it
    ran: list[str] = []

    async def fake_runner(src: Path, dst: Path) -> None:
        ran.append(src.name)
        dst.write_bytes(b"mp4")

    done, skipped, failures = await transcode_pending(
        items, videos, tmp_path, jobs=2, runner=fake_runner
    )
    assert (done, skipped, failures) == (2, 1, [])
    assert sorted(ran) == ["0002-abc1234.gif", "0003-abc1234.gif"]
    assert (tmp_path / "0002-crunch.mp4").read_bytes() == b"mp4"
    assert not list(tmp_path.glob("*.tmp.mp4"))


async def test_transcode_reports_failures_without_aborting_the_run(tmp_path):
    videos = _videos(tmp_path, "0001", "0002")
    items = [
        PlanItem("0001", "3/4 sit-up", "0001-3-4-sit-up.mp4"),
        PlanItem("0002", "crunch", "0002-crunch.mp4"),
    ]

    async def flaky_runner(src: Path, dst: Path) -> None:
        if src.name.startswith("0001"):
            raise RuntimeError("ffmpeg exploded")
        dst.write_bytes(b"mp4")

    done, _, failures = await transcode_pending(items, videos, tmp_path, jobs=1, runner=flaky_runner)
    assert done == 1  # the good file still completed
    assert len(failures) == 1 and "0001-3-4-sit-up.mp4" in failures[0]
    assert not (tmp_path / "0001-3-4-sit-up.mp4").exists()
    assert (tmp_path / "0002-crunch.mp4").exists()


def test_manifest_round_trips_through_the_ingest_loader(tmp_path):
    items = [PlanItem("0025", "bench press", "0025-barbell-bench-press.mp4")]
    path = write_manifest(items, tmp_path)
    entries = load_manifest(path)
    assert [(e.name, e.slug) for e in entries] == [("bench press", "0025-barbell-bench-press.mp4")]


def test_main_end_to_end_with_a_stub_dataset(tmp_path, monkeypatch):
    dataset = tmp_path / "dataset"
    (dataset / "data").mkdir(parents=True)
    (dataset / "videos").mkdir()
    records = [{"id": rid, "name": name} for rid, name in [("0652", "Pull-Up"), ("1326", "chin-up")]]
    (dataset / "data" / "exercises.json").write_text(json.dumps(records), encoding="utf-8")
    for rid in ("0652", "1326"):
        (dataset / "videos" / f"{rid}-xyz.gif").write_bytes(b"GIF89a")
    out = tmp_path / "out"

    async def fake_runner(src: Path, dst: Path) -> None:
        dst.write_bytes(b"mp4")

    monkeypatch.setattr("agentg.scripts.build_demo_media._ffmpeg", fake_runner)
    main([str(dataset), str(out)])

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == [{"name": "pull-up", "slug": "0652-pull-up.mp4"}]
    assert (out / "0652-pull-up.mp4").exists()


def test_index_videos_and_load_records(tmp_path):
    dataset = tmp_path / "dataset"
    (dataset / "data").mkdir(parents=True)
    (dataset / "videos").mkdir()
    (dataset / "data" / "exercises.json").write_text(
        json.dumps([{"id": "0001", "name": "  3/4   Sit-Up "}]), encoding="utf-8"
    )
    (dataset / "videos" / "0001-2gPfomN.gif").write_bytes(b"GIF89a")
    assert load_records(dataset) == [DatasetRecord(id="0001", name="3/4 sit-up")]
    assert index_videos(dataset)["0001"].name == "0001-2gPfomN.gif"

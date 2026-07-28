"""Build the demo-media artifacts from the exercises dataset (docs/demo-media.md).

Ops pipeline, run once per catalog refresh on whatever box holds the dataset:

    python -m agentg.scripts.build_demo_media <dataset_dir> <output_dir> [--jobs N]

Reads ``data/exercises.json`` and ``videos/<id>-*.gif`` from the dataset,
transcodes every GIF to a soundless H.264 MP4 (the runbook's ffmpeg recipe),
and writes the MP4s plus a ``manifest.json`` into the output directory — the
app's media root. Ingest with ``python -m agentg.scripts.ingest_demos``.

Re-running is resumable: MP4s that already exist are skipped, and slugs are
derived from the dataset's stable ``id`` so a re-run never renames a file
(renaming a slug would drop the cached Telegram file_id on next ingest).

Nothing here is committed to git — the media is © Gymvisual, licensed for app
use, not redistribution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from agentg.catalog import normalize_exercise_name
from agentg.training import SEED_EXERCISES

# The seed catalog predates the dataset and names its staples differently
# ("bench press" vs the dataset's "barbell bench press"). Each seed Exercise
# is wired to the dataset record that best shows the movement, so ingest
# resolves onto the existing row instead of creating a near-duplicate beside
# it. Keys must stay exactly the seed names; values are dataset names
# (normalized). Judgment calls are noted in the ops report.
SEED_DEMO_PICKS: dict[str, str] = {
    "bench press": "barbell bench press",
    "overhead press": "barbell seated overhead press",
    "squat": "barbell full squat",
    "deadlift": "barbell deadlift",
    "dips": "chest dip",
    "pull-up": "pull-up",
    "barbell row": "barbell bent over row",
    "lat pulldown": "cable lat pulldown full range of motion",
    "lunge": "dumbbell lunge",
    "biceps curl": "barbell curl",
}

assert set(SEED_DEMO_PICKS) == set(SEED_EXERCISES), "SEED_DEMO_PICKS drifted from SEED_EXERCISES"

# Every seed name and alias, normalized: a dataset record under one of these
# names would resolve onto the seed row at ingest, so it must not be emitted
# under its own name (e.g. the dataset's "chin-up" is a seed alias of pull-up).
_RESERVED_NAMES: set[str] = {
    normalize_exercise_name(name)
    for seed, aliases in SEED_EXERCISES.items()
    for name in (seed, *aliases)
}

FFMPEG_ARGS = [
    "-an",
    "-vf", "scale=720:-2",
    "-c:v", "libx264",
    "-pix_fmt", "yuv420p",
    "-crf", "27",
    "-movflags", "+faststart",
]


@dataclass(frozen=True)
class DatasetRecord:
    id: str
    name: str  # normalized


@dataclass(frozen=True)
class PlanItem:
    """One MP4 to transcode plus its manifest entry."""

    record_id: str
    manifest_name: str  # what ingest will look up / create in the catalog
    slug: str


@dataclass(frozen=True)
class Skipped:
    record_id: str
    name: str
    reason: str


@dataclass(frozen=True)
class Plan:
    items: list[PlanItem]
    seed_wiring: dict[str, str | None]  # seed name -> dataset record it wires to
    skipped: list[Skipped]


def slugify(name: str) -> str:
    """Lowercase, hyphen-separated filename fragment ("3/4 Sit-Up" -> "3-4-sit-up")."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def slug_for(record: DatasetRecord) -> str:
    """The MP4 filename under the media root. Anchored on the dataset id, which
    is the one part of the dataset that does not drift between re-runs."""
    return f"{record.id}-{slugify(record.name)}.mp4"


def load_records(dataset_dir: Path) -> list[DatasetRecord]:
    raw = json.loads((dataset_dir / "data" / "exercises.json").read_text(encoding="utf-8"))
    return [DatasetRecord(id=item["id"], name=normalize_exercise_name(item["name"])) for item in raw]


def index_videos(dataset_dir: Path) -> dict[str, Path]:
    """Map record id -> GIF path. Stems look like ``0001-2gPfomN``."""
    videos: dict[str, Path] = {}
    for gif in (dataset_dir / "videos").glob("*.gif"):
        videos[gif.stem.split("-", 1)[0]] = gif
    return videos


def build_plan(records: list[DatasetRecord], videos: dict[str, Path]) -> Plan:
    """Decide the manifest: seed staples wired onto their seed rows, every other
    dataset exercise under its own (normalized) name, nothing double-emitted."""
    by_name: dict[str, DatasetRecord] = {}
    skipped: list[Skipped] = []
    for record in records:  # dataset order; first record wins a duplicated name
        if record.name in by_name:
            skipped.append(Skipped(record.id, record.name, "duplicate name in dataset"))
        else:
            by_name[record.name] = record

    items: list[PlanItem] = []
    seed_wiring: dict[str, str | None] = {}
    consumed: dict[str, str] = {}  # record id -> seed name it stands in for
    for seed in SEED_EXERCISES:
        pick = SEED_DEMO_PICKS[seed]
        picked = by_name.get(pick)
        if picked is None:
            seed_wiring[seed] = None
            continue
        seed_wiring[seed] = f"{picked.name} ({picked.id})"
        consumed[picked.id] = seed
        items.append(PlanItem(picked.id, seed, slug_for(picked)))

    for record in records:
        if record.id in consumed:
            skipped.append(Skipped(record.id, record.name, f"wired onto seed '{consumed[record.id]}'"))
        elif record.name in _RESERVED_NAMES:
            skipped.append(Skipped(record.id, record.name, "name resolves onto a seed catalog row"))
        elif by_name.get(record.name) is not record:
            continue  # already reported as a duplicate above
        elif record.id not in videos:
            skipped.append(Skipped(record.id, record.name, "no GIF in dataset videos/"))
        else:
            items.append(PlanItem(record.id, record.name, slug_for(record)))
    return Plan(items, seed_wiring, skipped)


async def _ffmpeg(src: Path, dst: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src), *FFMPEG_ARGS, str(dst),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {src.name}: {stderr.decode(errors='replace').strip()}")


Runner = Callable[[Path, Path], Awaitable[None]]


async def transcode_pending(
    items: list[PlanItem],
    videos: dict[str, Path],
    out_dir: Path,
    jobs: int,
    runner: Runner | None = None,
) -> tuple[int, int, list[str]]:
    """Transcode every planned MP4 that doesn't exist yet. Returns
    (done, skipped, failures) — a failed GIF is reported, not fatal: the run
    finishes the rest and is resumable, so a re-run retries only the failures.

    Writes go to a temp file renamed into place, so an interrupted run never
    leaves a half-written MP4 that a later run would mistake for done."""
    runner = runner or _ffmpeg
    out_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(jobs)
    done = 0
    skipped = 0
    failures: list[str] = []

    async def one(item: PlanItem) -> None:
        nonlocal done, skipped
        dst = out_dir / item.slug
        if dst.exists() and dst.stat().st_size > 0:
            skipped += 1
            return
        async with semaphore:
            # keeps the .mp4 extension so ffmpeg can infer the muxer format
            tmp = dst.with_name(dst.stem + ".tmp.mp4")
            try:
                await runner(videos[item.record_id], tmp)
            except Exception as exc:
                failures.append(f"{item.slug}: {exc}")
                return
            os.replace(tmp, dst)
            done += 1

    async with asyncio.TaskGroup() as tg:
        for item in items:
            tg.create_task(one(item))
    return done, skipped, failures


def write_manifest(items: list[PlanItem], out_dir: Path) -> Path:
    manifest = out_dir / "manifest.json"
    payload = [{"name": item.manifest_name, "slug": item.slug} for item in items]
    manifest.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dataset_dir", type=Path, help="clone of hasaneyldrm/exercises-dataset")
    parser.add_argument("output_dir", type=Path, help="media root for MP4s + manifest.json")
    parser.add_argument("--jobs", type=int, default=min(8, os.cpu_count() or 8),
                        help="parallel ffmpeg processes")
    args = parser.parse_args(argv)

    records = load_records(args.dataset_dir)
    videos = index_videos(args.dataset_dir)
    plan = build_plan(records, videos)

    missing = [seed for seed, wiring in plan.seed_wiring.items() if wiring is None]
    if missing:
        print(f"WARNING: no dataset match for seed exercises: {', '.join(missing)}", file=sys.stderr)

    done, skipped, failures = asyncio.run(
        transcode_pending(plan.items, videos, args.output_dir, args.jobs)
    )
    manifest = write_manifest(plan.items, args.output_dir)

    print(f"transcoded {done} MP4s ({skipped} already present), manifest: {manifest}")
    print(f"manifest entries: {len(plan.items)}; dataset records skipped: {len(plan.skipped)}")
    print("seed wiring:")
    for seed, wiring in plan.seed_wiring.items():
        print(f"  {seed} -> {wiring or 'NO MATCH'}")
    if failures:
        print(f"FAILED transcodes ({len(failures)}):", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

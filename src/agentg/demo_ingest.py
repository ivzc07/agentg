"""Populate the Exercise catalog with demos from the dataset manifest.

The dataset (per docs/research/exercise-demo-media.md) ships a JSON index of
exercises; ops transcodes each GIF to a soundless MP4 (see docs/demo-media.md)
and produces a manifest mapping Exercise name → MP4 slug. This wires those
slugs onto the catalog, creating Exercises the seed set doesn't already have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from agentg.demos import DemoStore


@dataclass(frozen=True)
class DemoManifestEntry:
    name: str
    slug: str


def load_manifest(path: str | Path) -> list[DemoManifestEntry]:
    """Read a manifest: a JSON list of ``{"name": ..., "slug": ...}`` objects."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [DemoManifestEntry(name=item["name"], slug=item["slug"]) for item in raw]


async def ingest_demo_manifest(demos: DemoStore, entries: list[DemoManifestEntry]) -> int:
    """Wire each manifest entry's demo onto the catalog. Returns the count."""
    for entry in entries:
        await demos.set_default_demo(entry.name, entry.slug)
    return len(entries)

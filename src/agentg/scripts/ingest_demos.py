"""Ingest a demo manifest into the Exercise catalog (see docs/demo-media.md).

Usage: ``python -m agentg.scripts.ingest_demos <manifest.json>``
"""

from __future__ import annotations

import asyncio
import sys

from agentg.config import Settings
from agentg.db import create_engine
from agentg.demo_ingest import ingest_demo_manifest, load_manifest
from agentg.demos import DemoStore
from agentg.store import LinkingStore


async def _run(manifest_path: str) -> int:
    settings = Settings.from_env()
    engine = create_engine(settings.database_url)
    await LinkingStore(engine).ensure_schema()  # exercises table must exist
    demos = DemoStore(engine)
    count = await ingest_demo_manifest(demos, load_manifest(manifest_path))
    await engine.dispose()
    return count


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python -m agentg.scripts.ingest_demos <manifest.json>", file=sys.stderr)
        raise SystemExit(2)
    count = asyncio.run(_run(sys.argv[1]))
    print(f"ingested {count} demo entries")


if __name__ == "__main__":
    main()

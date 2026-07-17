"""Process entrypoint: settings -> engine -> stores -> Agent -> polling + sweep."""

import asyncio
import logging
from datetime import UTC, datetime

from agents import set_tracing_disabled
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agentg.agent import build_agent
from agentg.channels.telegram import (
    TelegramDemoSender,
    TelegramNotifier,
    bot_id,
    build_bot,
    run_polling,
)
from agentg.checkin_store import CheckinStore
from agentg.checkin_sweep import run_sweep
from agentg.compaction import build_summarizer
from agentg.config import Settings
from agentg.db import create_engine
from agentg.demos import DemoStore
from agentg.notes import NotesStore
from agentg.onboarding import Onboarding
from agentg.routines import RoutineStore
from agentg.runtime import AgentRuntime
from agentg.store import LinkingStore
from agentg.training import TrainingStore

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = Settings.from_env()
    # Tracing exports to the OpenAI platform; we may not be running OpenAI models.
    set_tracing_disabled(True)
    engine = create_engine(settings.database_url)
    store = LinkingStore(engine)
    training = TrainingStore(engine)
    routines = RoutineStore(engine)
    checkins = CheckinStore(engine)
    demos = DemoStore(engine)

    bot = build_bot(settings.telegram_bot_token)
    demo_sender = TelegramDemoSender(
        bot, settings.demo_media_root, bot_id(settings.telegram_bot_token)
    )
    runtime = AgentRuntime(
        agent=build_agent(settings),
        engine=engine,
        store=store,
        onboarding=Onboarding(store),
        training=training,
        notes=NotesStore(engine),
        routines=routines,
        checkins=checkins,
        demos=demos,
        summarizer=build_summarizer(settings),
        demo_sender=demo_sender,
    )
    await runtime.ensure_schema()

    notifier = TelegramNotifier(bot)

    # In-process proactive check-in sweep. Runs on the hour; the decision layer
    # only fires each Member at 09:00 in their gym's timezone.
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def sweep() -> None:
        try:
            sent = await run_sweep(datetime.now(UTC), checkins, training, routines, notifier)
            if sent:
                logger.info("check-in sweep sent %d nudges", sent)
        except Exception:
            logger.exception("check-in sweep failed")

    scheduler.add_job(sweep, "cron", minute=0, id="checkin-sweep")
    scheduler.start()

    await run_polling(bot, runtime.handle_message)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()

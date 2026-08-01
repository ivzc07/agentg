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
from agentg.checkin_sweep import run_sweep
from agentg.compaction import build_summarizer
from agentg.config import Settings
from agentg.dashboard import DashboardDoor
from agentg.dashboard_web import build_app, start_server
from agentg.db import create_engine
from agentg.linking import Linking, build_phraser
from agentg.runtime import AgentRuntime
from agentg.stores import Stores

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = Settings.from_env()
    # Tracing exports to the OpenAI platform; we may not be running OpenAI models.
    set_tracing_disabled(True)
    engine = create_engine(settings.database_url)
    stores = Stores.from_engine(engine)

    bot = build_bot(settings.telegram_bot_token)
    notifier = TelegramNotifier(bot)
    demo_sender = TelegramDemoSender(
        bot, settings.demo_media_root, bot_id(settings.telegram_bot_token)
    )
    runtime = AgentRuntime(
        agent=build_agent(settings),
        engine=engine,
        stores=stores,
        linking=Linking(stores.linking, build_phraser(settings)),
        summarizer=build_summarizer(settings),
        demo_sender=demo_sender,
        notifier=notifier,
        dashboard=DashboardDoor(stores.dashboard, settings.dashboard_base_url),
    )
    await runtime.ensure_schema()

    # The dashboard's HTTP server shares this event loop with the long
    # poller (spec-dashboard §Stack). The bot username builds the t.me
    # invite links the Settings screen shows.
    bot_username = (await bot.get_me()).username or ""
    web_runner = await start_server(
        build_app(
            stores.dashboard,
            stores.linking,
            session_secret=settings.dashboard_session_secret
            or settings.telegram_bot_token,
            bot_username=bot_username,
            secure_cookies=settings.dashboard_base_url.startswith("https://"),
            notifier=notifier,
            spa_enabled=settings.dashboard_spa_enabled,
        ),
        host="0.0.0.0",
        port=settings.dashboard_port,
    )

    # In-process proactive check-in sweep. Runs on the hour; the decision layer
    # only fires each Member at 09:00 in their gym's timezone.
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def sweep() -> None:
        try:
            sent = await run_sweep(
                datetime.now(UTC), stores.checkins, stores.training, stores.routines, notifier
            )
            if sent:
                logger.info("check-in sweep sent %d nudges", sent)
        except Exception:
            logger.exception("check-in sweep failed")

    scheduler.add_job(sweep, "cron", minute=0, id="checkin-sweep")
    scheduler.start()

    try:
        await run_polling(bot, runtime.handle_message)
    finally:
        await web_runner.cleanup()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()

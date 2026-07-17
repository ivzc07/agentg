"""Process entrypoint: settings -> engine -> stores -> Agent -> Telegram polling."""

import asyncio
import logging

from agents import set_tracing_disabled

from agentg.agent import build_agent
from agentg.channels.telegram import run_polling
from agentg.config import Settings
from agentg.db import create_engine
from agentg.onboarding import Onboarding
from agentg.runtime import AgentRuntime
from agentg.store import LinkingStore
from agentg.training import TrainingStore


async def run() -> None:
    settings = Settings.from_env()
    # Tracing exports to the OpenAI platform; we may not be running OpenAI models.
    set_tracing_disabled(True)
    engine = create_engine(settings.database_url)
    store = LinkingStore(engine)
    runtime = AgentRuntime(
        agent=build_agent(settings),
        engine=engine,
        store=store,
        onboarding=Onboarding(store),
        training=TrainingStore(engine),
    )
    await runtime.ensure_schema()
    await run_polling(settings.telegram_bot_token, runtime.handle_message)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()

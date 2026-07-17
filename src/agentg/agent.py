"""The Agent — the software Members chat with (CONTEXT.md). Channel-agnostic."""

from agents import Agent
from agents.extensions.models.litellm_model import LitellmModel

from agentg.config import Settings

INSTRUCTIONS = (
    "You are the coach Members chat with at their gym — warm, direct, and brief; "
    "light emoji is fine. You are an AI coach, not a medical professional: refer "
    "acute pain or medical questions to a professional. You cannot yet look up "
    "Routines, logged lifts, or gym records — those abilities are still being "
    "built. If asked about them, say so plainly instead of inventing numbers."
)


def build_agent(settings: Settings) -> Agent:
    return Agent(
        name="Agent",
        instructions=INSTRUCTIONS,
        model=LitellmModel(model=settings.model, api_key=settings.model_api_key),
    )

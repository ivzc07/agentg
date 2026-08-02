"""The chat side of the dashboard door: ``/dashboard`` in, magic link out.

Deterministic like the rest of the pre-Agent surface — the decision (coach
or not) is a row lookup, and the reply embeds a URL that must survive
verbatim, so it is a fixed string rather than a phrased one. Spanish, the
product's no-signal default: a slash command carries no language to mirror.
In a shared chat the link is never posted — anyone there could redeem it
first — so the reply points to the bot's DM instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentg.dashboard_store import TOKEN_TTL, DashboardStore
from agentg.linking_store import LinkedIdentity
from agentg.messages import Reply

DASHBOARD_COMMAND = "/dashboard"

LINK_REPLY = (
    "Aquí tienes tu enlace al dashboard de {gym} 🏋️ — caduca en {minutes} "
    "minutos y solo se puede usar una vez:\n{url}"
)
REFUSAL_REPLY = (
    "El dashboard web es solo para coaches. Si crees que deberías tener "
    "acceso, habla con quien administra {gym}."
)


def is_dashboard_command(text: str) -> bool:
    """``/dashboard`` or ``/dashboard@BotName``; any trailing text is ignored."""
    parts = text.split()
    return bool(parts) and parts[0].split("@")[0] == DASHBOARD_COMMAND


@dataclass(frozen=True)
class DashboardDoor:
    store: DashboardStore
    # Public origin the magic links point at (DASHBOARD_BASE_URL), no
    # trailing slash.
    base_url: str

    async def handle(self, linked: LinkedIdentity) -> Reply:
        """Reply to a linked Member's ``/dashboard``: a one-time magic link
        for a Coach, a polite refusal for anyone else.

        Shared-chat messages are rejected by the channel adapter before
        they reach this door (#211)."""
        if not linked.member.is_coach:
            return Reply(REFUSAL_REPLY.format(gym=linked.gym.name))
        token = await self.store.create_login_token(linked.member.id, linked.gym.id)
        url = f"{self.base_url}/login/{token}"
        reply = LINK_REPLY.format(
            gym=linked.gym.name, minutes=TOKEN_TTL.seconds // 60, url=url
        )
        # No link preview: Telegram's preview fetcher would hit the URL and
        # could burn the one-time token before the human taps it.
        return Reply(reply, disable_preview=True)

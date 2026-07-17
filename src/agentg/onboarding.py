"""The linking conversation: Invite codes in, deterministic replies out.

Linking creates rows and switches tenancy, so it deliberately does not run
through the LLM — it is a small state machine with fixed wording per
docs/spec.md §Onboarding & gym linking. The Agent only ever speaks for
linked Members. Pending steps live in memory: a restart mid-onboarding just
means the person taps the Gym's invite link again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentg.messages import IncomingMessage
from agentg.store import Gym, LinkedIdentity, LinkingStore

DEAD_END = (
    "Hey! 👋 I'm a coach that works with partner gyms, so I can only get you "
    "set up through your gym. Grab your gym's invite link or QR code — the "
    "front desk or your coach has it — tap it, and we'll get going."
)
NAME_CONFIRM = (
    "Welcome to {gym}! 🎉 I've got your name as {name} from your profile — "
    "should I use that? Reply yes, or tell me what you'd like to be called."
)
NAME_ASK = "Welcome to {gym}! 🎉 What should I call you?"
WELCOME = (
    "You're all set, {name} — welcome to {gym}! 💪 "
    "Tell me when you're at the gym and we'll take it from there."
)
SAME_GYM = "You're already set up at {gym}, {name} — no need to link again. What's on for today?"
LINK_INACTIVE = (
    "That invite link doesn't seem to be active. No worries — you're still set up at {gym}."
)
SWITCH_CONFIRM = (
    "That's an invite for {new_gym}. Switching means a fresh start there — "
    "your training history stays with {old_gym}. Want to switch? (yes / no)"
)
SWITCHED = (
    "Done — welcome to {new_gym}, {name}! 💪 "
    "Fresh start from here; your history stayed with your old gym."
)
SWITCH_CANCELLED = "No problem — you're still with {gym}. 👍"

AFFIRMATIVES = {"yes", "y", "yep", "yeah", "yup", "ok", "okay", "sure", "si", "sí", "correct"}
MAX_NAME_LENGTH = 100


def _is_affirmative(text: str) -> bool:
    return text.strip().lower().strip("!. ") in AFFIRMATIVES


def _clean_name(text: str) -> str:
    return " ".join(text.split())[:MAX_NAME_LENGTH]


@dataclass
class _AwaitingName:
    gym_id: int
    gym_name: str
    prefilled: str


@dataclass
class _AwaitingSwitch:
    gym_id: int
    gym_name: str


_Pending = _AwaitingName | _AwaitingSwitch
_Identity = tuple[str, str]


@dataclass
class Onboarding:
    store: LinkingStore
    _pending: dict[_Identity, _Pending] = field(default_factory=dict)

    async def handle(self, msg: IncomingMessage, linked: LinkedIdentity | None) -> str | None:
        """Reply to anything linking-related; ``None`` means normal Agent chat."""
        identity = (msg.channel, msg.channel_user_id)

        if msg.link_code is not None:  # a deep-link tap always restarts the flow
            self._pending.pop(identity, None)
            return await self._handle_code(identity, msg, linked, msg.link_code)

        pending = self._pending.get(identity)
        if isinstance(pending, _AwaitingName):
            return await self._confirm_name(identity, msg, pending)
        if isinstance(pending, _AwaitingSwitch):
            return await self._confirm_switch(identity, msg, linked, pending)

        # A typed Invite code links too; any other unlinked text dead-ends.
        gym = await self.store.gym_by_invite_code(msg.text)
        if gym is not None:
            return self._start_link(identity, msg, linked, gym)
        if linked is None:
            return DEAD_END
        return None

    async def _handle_code(
        self, identity: _Identity, msg: IncomingMessage, linked: LinkedIdentity | None, code: str
    ) -> str:
        gym = await self.store.gym_by_invite_code(code) if code else None
        if gym is not None:
            return self._start_link(identity, msg, linked, gym)
        if linked is not None:
            if not code:  # a bare /start from a linked Member
                return SAME_GYM.format(gym=linked.gym.name, name=linked.member.name)
            return LINK_INACTIVE.format(gym=linked.gym.name)
        return DEAD_END

    def _start_link(
        self, identity: _Identity, msg: IncomingMessage, linked: LinkedIdentity | None, gym: Gym
    ) -> str:
        if linked is not None:
            if linked.gym.id == gym.id:
                return SAME_GYM.format(gym=gym.name, name=linked.member.name)
            self._pending[identity] = _AwaitingSwitch(gym_id=gym.id, gym_name=gym.name)
            return SWITCH_CONFIRM.format(new_gym=gym.name, old_gym=linked.gym.name)

        prefilled = _clean_name(msg.display_name)
        self._pending[identity] = _AwaitingName(
            gym_id=gym.id, gym_name=gym.name, prefilled=prefilled
        )
        if prefilled:
            return NAME_CONFIRM.format(gym=gym.name, name=prefilled)
        return NAME_ASK.format(gym=gym.name)

    async def _confirm_name(
        self, identity: _Identity, msg: IncomingMessage, pending: _AwaitingName
    ) -> str:
        if pending.prefilled and _is_affirmative(msg.text):
            name = pending.prefilled
        else:
            name = _clean_name(msg.text)
        if not name:
            return NAME_ASK.format(gym=pending.gym_name)  # still waiting
        await self.store.link_member(pending.gym_id, name, *identity)
        del self._pending[identity]
        return WELCOME.format(name=name, gym=pending.gym_name)

    async def _confirm_switch(
        self,
        identity: _Identity,
        msg: IncomingMessage,
        linked: LinkedIdentity | None,
        pending: _AwaitingSwitch,
    ) -> str:
        del self._pending[identity]
        if linked is None:  # identity vanished mid-flow; start over
            return DEAD_END
        if not _is_affirmative(msg.text):  # anything but a clear yes stays put
            return SWITCH_CANCELLED.format(gym=linked.gym.name)
        # Fresh start at the new Gym: new Member row (same person, same name),
        # old row untouched, channel identity re-pointed.
        await self.store.link_member(pending.gym_id, linked.member.name, *identity)
        return SWITCHED.format(new_gym=pending.gym_name, name=linked.member.name)

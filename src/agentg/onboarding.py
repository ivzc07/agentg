"""The linking conversation: Invite codes in, phrased replies out.

Linking creates rows and switches tenancy, so *deciding* what happens
(link, re-ask, switch, dead-end) stays a deterministic state machine per
docs/spec.md §Onboarding & gym linking — never the LLM's call. But *saying*
it no longer has to be fixed strings: each step hands the phraser an
instruction (the facts, never invented) plus what the person just said, and
the phraser turns that into one natural reply. The Agent only ever speaks
for linked Members; this is the voice before that. Pending steps live in
memory: a restart mid-onboarding just means the person taps the Gym's
invite link again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agentg.config import Settings
from agentg.messages import IncomingMessage
from agentg.models import Gym
from agentg.store import LinkedIdentity, LinkingStore

DEAD_END_INSTRUCTION = (
    "They aren't linked to any gym yet and just sent something that isn't a "
    "gym invite link or code. Explain warmly that you're a coach who only "
    "works through partner gyms, and ask them to get their gym's invite link "
    "or QR code — the front desk or their coach has it — and tap it to get "
    "started."
)
NAME_CONFIRM_INSTRUCTION = (
    "They just linked to {gym} via their gym's invite code. Their profile "
    'name is "{name}". Warmly welcome them to {gym} and ask if you should '
    "call them {name} — tell them to reply yes, or say what they'd like to "
    "be called instead."
)
NAME_ASK_INSTRUCTION = (
    "They just linked to {gym} (or just declined their prefilled name). "
    "Warmly welcome them to {gym} and ask what they'd like to be called."
)
WELCOME_INSTRUCTION = (
    "Their name is now set as {name} at {gym} — linking is complete. Warmly "
    "confirm they're all set and welcome to {gym}, and tell them to message "
    "you once they're at the gym and you'll take it from there."
)
SAME_GYM_INSTRUCTION = (
    "{name} is already linked to {gym} and just tapped that gym's invite "
    "link again — no need to link again. Reassure them warmly and ask "
    "what's on for today."
)
LINK_INACTIVE_INSTRUCTION = (
    "They tapped an invite link that isn't active, but they're already "
    "linked to {gym}. Reassure them warmly that they're still set up at "
    "{gym}, no worries."
)
SWITCH_CONFIRM_INSTRUCTION = (
    "They tapped an invite link for {new_gym} but are currently linked to "
    "{old_gym}. Explain that switching means a fresh start at {new_gym} and "
    "their training history stays with {old_gym}, then clearly ask if they "
    "want to switch (a yes or no answer)."
)
SWITCHED_INSTRUCTION = (
    "They just confirmed switching gyms. They're now linked to {new_gym} as "
    "{name}, with a fresh start there; their history stayed with their old "
    "gym. Confirm this warmly."
)
SWITCH_CANCELLED_INSTRUCTION = (
    "They declined switching gyms. Reassure them warmly that they're still "
    "with {gym}, no problem."
)
LINK_EXPIRED_INSTRUCTION = (
    "Their invite code stopped working before they finished linking (it was "
    "regenerated). Apologize briefly and ask them to get their gym's "
    "current invite link or QR code and tap it to get set up."
)

Phraser = Callable[[str, str], Awaitable[str]]

_PHRASER_PROMPT = """\
You are this app's onboarding voice — the first messages a gym Member gets, \
before they're linked to their coach. Turn the instruction below into one \
short, warm reply to send right now: brief and direct, light emoji OK. \
Mirror the language of what they just said; with no signal yet, speak \
Spanish. Never invent facts beyond the instruction, and never skip what it \
asks for. Reply with the message text only, nothing else.\
"""


def build_phraser(settings: Settings) -> Phraser:
    """The production phraser: one plain model call per onboarding reply."""

    async def phrase(instruction: str, member_text: str) -> str:
        from litellm import acompletion  # deferred: import cost and test isolation

        response = await acompletion(
            model=settings.model,
            api_key=settings.model_api_key,
            messages=[
                {"role": "system", "content": _PHRASER_PROMPT},
                {"role": "user", "content": f'They just said: "{member_text}"\n\n{instruction}'},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    return phrase

AFFIRMATIVES = {"yes", "y", "yep", "yeah", "yup", "ok", "okay", "sure", "si", "sí", "correct"}
NEGATIVES = {"no", "n", "nope", "nah"}
MAX_NAME_LENGTH = 100
MAX_NAME_WORDS = 4  # past this it's a sentence deflecting the question, not a name


def _normalized(text: str) -> str:
    return text.strip().lower().strip("!. ")


def _is_affirmative(text: str) -> bool:
    return _normalized(text) in AFFIRMATIVES


def _is_negative(text: str) -> bool:
    return _normalized(text) in NEGATIVES


def _clean_name(text: str) -> str:
    return " ".join(text.split())[:MAX_NAME_LENGTH]


def _looks_like_a_name(text: str) -> bool:
    return 0 < len(text.split()) <= MAX_NAME_WORDS


@dataclass
class _AwaitingName:
    gym_id: int
    gym_name: str
    invite_code: str
    prefilled: str


@dataclass
class _AwaitingSwitch:
    gym_id: int
    gym_name: str
    invite_code: str


_Pending = _AwaitingName | _AwaitingSwitch
_Identity = tuple[str, str]


@dataclass
class Onboarding:
    store: LinkingStore
    phraser: Phraser
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
            return await self._start_link(identity, msg, linked, gym)
        if linked is None:
            return await self.phraser(DEAD_END_INSTRUCTION, msg.text)
        return None

    async def _handle_code(
        self, identity: _Identity, msg: IncomingMessage, linked: LinkedIdentity | None, code: str
    ) -> str:
        gym = await self.store.gym_by_invite_code(code) if code else None
        if gym is not None:
            return await self._start_link(identity, msg, linked, gym)
        if linked is not None:
            if not code:  # a bare /start from a linked Member
                instruction = SAME_GYM_INSTRUCTION.format(gym=linked.gym.name, name=linked.member.name)
            else:
                instruction = LINK_INACTIVE_INSTRUCTION.format(gym=linked.gym.name)
            return await self.phraser(instruction, msg.text)
        return await self.phraser(DEAD_END_INSTRUCTION, msg.text)

    async def _start_link(
        self, identity: _Identity, msg: IncomingMessage, linked: LinkedIdentity | None, gym: Gym
    ) -> str:
        if linked is not None:
            if linked.gym.id == gym.id:
                instruction = SAME_GYM_INSTRUCTION.format(gym=gym.name, name=linked.member.name)
                return await self.phraser(instruction, msg.text)
            self._pending[identity] = _AwaitingSwitch(
                gym_id=gym.id, gym_name=gym.name, invite_code=gym.invite_code
            )
            instruction = SWITCH_CONFIRM_INSTRUCTION.format(new_gym=gym.name, old_gym=linked.gym.name)
            return await self.phraser(instruction, msg.text)

        prefilled = _clean_name(msg.display_name)
        self._pending[identity] = _AwaitingName(
            gym_id=gym.id, gym_name=gym.name, invite_code=gym.invite_code, prefilled=prefilled
        )
        if prefilled:
            instruction = NAME_CONFIRM_INSTRUCTION.format(gym=gym.name, name=prefilled)
        else:
            instruction = NAME_ASK_INSTRUCTION.format(gym=gym.name)
        return await self.phraser(instruction, msg.text)

    async def _confirm_name(
        self, identity: _Identity, msg: IncomingMessage, pending: _AwaitingName
    ) -> str:
        # A pasted Invite code mid-flow restarts linking, not a name change.
        typed_gym = await self.store.gym_by_invite_code(msg.text)
        if typed_gym is not None:
            del self._pending[identity]
            return await self._start_link(identity, msg, None, typed_gym)
        if pending.prefilled and _is_affirmative(msg.text):
            name = pending.prefilled
        elif _is_negative(msg.text):
            pending.prefilled = ""  # they declined the prefill; ask outright
            return await self.phraser(NAME_ASK_INSTRUCTION.format(gym=pending.gym_name), msg.text)
        else:
            candidate = _clean_name(msg.text)
            name = candidate if _looks_like_a_name(candidate) else ""
        if not name:
            return await self.phraser(NAME_ASK_INSTRUCTION.format(gym=pending.gym_name), msg.text)
        if not await self._code_still_active(pending.gym_id, pending.invite_code):
            del self._pending[identity]
            return await self.phraser(LINK_EXPIRED_INSTRUCTION, msg.text)
        await self.store.link_member(pending.gym_id, name, *identity)
        # Cleared only after the write: a store error keeps the step retryable.
        del self._pending[identity]
        instruction = WELCOME_INSTRUCTION.format(name=name, gym=pending.gym_name)
        return await self.phraser(instruction, msg.text)

    async def _confirm_switch(
        self,
        identity: _Identity,
        msg: IncomingMessage,
        linked: LinkedIdentity | None,
        pending: _AwaitingSwitch,
    ) -> str:
        if linked is None:  # identity vanished mid-flow; start over
            del self._pending[identity]
            return await self.phraser(DEAD_END_INSTRUCTION, msg.text)
        if not _is_affirmative(msg.text):  # anything but a clear yes stays put
            del self._pending[identity]
            instruction = SWITCH_CANCELLED_INSTRUCTION.format(gym=linked.gym.name)
            return await self.phraser(instruction, msg.text)
        if not await self._code_still_active(pending.gym_id, pending.invite_code):
            del self._pending[identity]
            return await self.phraser(LINK_INACTIVE_INSTRUCTION.format(gym=linked.gym.name), msg.text)
        # Fresh start at the new Gym: new Member row (same person, same name),
        # old row untouched, channel identity re-pointed.
        await self.store.link_member(pending.gym_id, linked.member.name, *identity)
        # Cleared only after the write: a store error keeps the step retryable.
        del self._pending[identity]
        instruction = SWITCHED_INSTRUCTION.format(new_gym=pending.gym_name, name=linked.member.name)
        return await self.phraser(instruction, msg.text)

    async def _code_still_active(self, gym_id: int, invite_code: str) -> bool:
        """Regenerating an Invite code invalidates flows the old code started."""
        gym = await self.store.gym_by_invite_code(invite_code)
        return gym is not None and gym.id == gym_id

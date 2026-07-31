"""The linking conversation: Invite codes in, phrased replies out.

Linking creates rows and switches tenancy, so *deciding* what happens
(link, re-ask, switch, reject a near-miss code, dead-end) stays a
deterministic state machine per docs/spec.md §Onboarding & gym linking —
never the LLM's call. But *saying*
it no longer has to be fixed strings: each step hands the phraser an
instruction (the facts, never invented) plus what the person just said, and
the phraser turns that into one natural reply. The Agent only ever speaks
for linked Members; this is the voice before that. Pending steps live in
memory: a restart mid-linking just means the person taps the Gym's
invite link again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agentg.config import Settings
from agentg.messages import IncomingMessage
from agentg.models import Gym
from agentg.linking_store import (
    COACH_CODE_PREFIX,
    INVITE_CODE_ALPHABET,
    INVITE_CODE_LENGTH,
    LinkedIdentity,
    LinkingStore,
    normalize_invite_code,
)

DEAD_END_INSTRUCTION = (
    "They aren't linked to any gym yet and just sent something that isn't a "
    "gym invite link or code. Explain warmly that you're a coach who only "
    "works through partner gyms, and ask them to get their gym's invite link "
    "or QR code — the front desk or their coach has it — and tap it to get "
    "started."
)
CODE_NOT_FOUND_INSTRUCTION = (
    "They aren't linked to any gym yet and just typed something that looks "
    "like a gym invite code, but no gym matches it — most likely a typo. "
    "Tell them warmly that this code didn't work, ask them to double-check "
    "it character by character against what their gym gave them, and add "
    "that if it still fails they can get a fresh invite link or QR code "
    "from the gym's front desk or their coach."
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
COACH_WELCOME_INSTRUCTION = (
    "Their name is now set as {name} at {gym} — they linked via the gym's "
    "coach invite, so they're a Coach, not just a Member. Welcome them "
    "warmly to {gym} as its coach: they can shape the gym's rules doc, "
    "write routines for their members, and send /dashboard anytime to open "
    "their coach dashboard. Do not start a member intake or build them a "
    "training plan — but mention that if they'd like a plan for their own "
    "training too, they only have to ask."
)
COACH_PROMOTED_INSTRUCTION = (
    "{name} is already a Member of {gym} and just tapped the gym's coach "
    "invite link — they're now a Coach as well. Congratulate them warmly: "
    "they can now shape the gym's rules doc, write routines for their "
    "members, and send /dashboard to open the coach dashboard. Nothing "
    "about their own training changes — no intake, no new plan unless they "
    "ask for one."
)
COACH_SWITCHED_INSTRUCTION = (
    "They just confirmed switching gyms via a coach invite link. They're "
    "now linked to {new_gym} as {name}, coach-flagged, with a fresh start "
    "there; their training history stayed with their old gym. Confirm this "
    "warmly and welcome them as a coach of {new_gym}: the rules doc, "
    "writing routines for members, and /dashboard. Do not start a member "
    "intake — if they'd like a plan of their own too, they can ask."
)
ALREADY_COACH_INSTRUCTION = (
    "{name} is already a Coach of {gym} and just tapped the gym's coach "
    "invite link again — nothing to change. Reassure them warmly and ask "
    "what's on for today."
)

Phraser = Callable[[str, str], Awaitable[str]]

_PHRASER_PROMPT = """\
You are this app's linking voice — the first messages a gym Member gets, \
before they're linked to their coach. Turn the instruction below into one \
short, warm reply to send right now: brief and direct, light emoji OK. \
Mirror the language of what they just said; with no signal yet, speak \
Spanish. Never invent facts beyond the instruction, and never skip what it \
asks for. Reply with the message text only, nothing else.\
"""


def build_phraser(settings: Settings) -> Phraser:
    """The production phraser: one plain model call per linking reply."""

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


def _looks_like_invite_code(text: str) -> bool:
    """A near-miss code: typed like an invite code but matching no Gym.

    Shape only — a single word from the code alphabet, one character off
    the real length at most (a dropped or doubled char). Requiring a digit
    keeps ordinary short messages ("gracias", "perfecto") out: generation
    guarantees every real code carries one, Spanish words don't.
    """
    word = normalize_invite_code(text)
    if word.startswith(COACH_CODE_PREFIX):
        word = word[len(COACH_CODE_PREFIX):]
    if len(word.split()) != 1:  # empty, or a sentence — not a typed code
        return False
    if not INVITE_CODE_LENGTH - 1 <= len(word) <= INVITE_CODE_LENGTH + 1:
        return False
    if any(ch not in INVITE_CODE_ALPHABET for ch in word):
        return False
    return any(ch.isdigit() for ch in word)


@dataclass
class _AwaitingName:
    gym_id: int
    gym_name: str
    invite_code: str
    prefilled: str
    as_coach: bool = False


@dataclass
class _AwaitingSwitch:
    gym_id: int
    gym_name: str
    invite_code: str
    as_coach: bool = False


_Pending = _AwaitingName | _AwaitingSwitch
_Identity = tuple[str, str]


@dataclass
class Linking:
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

        # A typed Invite or coach code links too; a near-miss code is told
        # so; any other unlinked text dead-ends.
        resolved = await self._gym_for_code(msg.text)
        if resolved is not None:
            gym, as_coach = resolved
            return await self._start_link(identity, msg, linked, gym, as_coach)
        if linked is None:
            return await self._reply_unlinked_unknown(msg, msg.text)
        return None

    async def _gym_for_code(self, text: str) -> tuple[Gym, bool] | None:
        """Resolve a code from either namespace: ``(gym, as_coach)``."""
        gym = await self.store.gym_by_invite_code(text)
        if gym is not None:
            return gym, False
        gym = await self.store.gym_by_coach_invite_code(text)
        if gym is not None:
            return gym, True
        return None

    async def _handle_code(
        self, identity: _Identity, msg: IncomingMessage, linked: LinkedIdentity | None, code: str
    ) -> str:
        resolved = await self._gym_for_code(code) if code else None
        if resolved is not None:
            gym, as_coach = resolved
            return await self._start_link(identity, msg, linked, gym, as_coach)
        if linked is not None:
            if not code:  # a bare /start from a linked Member
                instruction = SAME_GYM_INSTRUCTION.format(gym=linked.gym.name, name=linked.member.name)
            else:
                instruction = LINK_INACTIVE_INSTRUCTION.format(gym=linked.gym.name)
            return await self.phraser(instruction, msg.text)
        return await self._reply_unlinked_unknown(msg, code)

    async def _reply_unlinked_unknown(self, msg: IncomingMessage, candidate: str) -> str:
        """Unlinked and nothing matched: a near-miss code (typed or tapped)
        is told the code didn't work; anything else dead-ends."""
        if _looks_like_invite_code(candidate):
            return await self.phraser(CODE_NOT_FOUND_INSTRUCTION, msg.text)
        return await self.phraser(DEAD_END_INSTRUCTION, msg.text)

    async def _start_link(
        self,
        identity: _Identity,
        msg: IncomingMessage,
        linked: LinkedIdentity | None,
        gym: Gym,
        as_coach: bool,
    ) -> str:
        if linked is not None:
            if linked.gym.id == gym.id:
                if as_coach and not linked.member.is_coach:
                    # An existing Member of this Gym tapping its coach link is
                    # promoted in place — no new row, no confirm. Atomic with
                    # the code check: a code regenerated since the tap revokes
                    # the promotion instead of racing through.
                    promoted = await self.store.promote_to_coach(
                        gym.id, linked.member.id, gym.coach_invite_code or ""
                    )
                    if promoted:
                        instruction = COACH_PROMOTED_INSTRUCTION.format(
                            gym=gym.name, name=linked.member.name
                        )
                    else:
                        instruction = LINK_INACTIVE_INSTRUCTION.format(gym=gym.name)
                elif as_coach:
                    instruction = ALREADY_COACH_INSTRUCTION.format(
                        gym=gym.name, name=linked.member.name
                    )
                else:
                    instruction = SAME_GYM_INSTRUCTION.format(gym=gym.name, name=linked.member.name)
                return await self.phraser(instruction, msg.text)
            code = gym.coach_invite_code if as_coach else gym.invite_code
            self._pending[identity] = _AwaitingSwitch(
                gym_id=gym.id, gym_name=gym.name, invite_code=code or "", as_coach=as_coach
            )
            instruction = SWITCH_CONFIRM_INSTRUCTION.format(new_gym=gym.name, old_gym=linked.gym.name)
            return await self.phraser(instruction, msg.text)

        prefilled = _clean_name(msg.display_name)
        code = gym.coach_invite_code if as_coach else gym.invite_code
        self._pending[identity] = _AwaitingName(
            gym_id=gym.id,
            gym_name=gym.name,
            invite_code=code or "",
            prefilled=prefilled,
            as_coach=as_coach,
        )
        if prefilled:
            instruction = NAME_CONFIRM_INSTRUCTION.format(gym=gym.name, name=prefilled)
        else:
            instruction = NAME_ASK_INSTRUCTION.format(gym=gym.name)
        return await self.phraser(instruction, msg.text)

    async def _confirm_name(
        self, identity: _Identity, msg: IncomingMessage, pending: _AwaitingName
    ) -> str:
        # A pasted Invite or coach code mid-flow restarts linking, not a
        # name change.
        resolved = await self._gym_for_code(msg.text)
        if resolved is not None:
            typed_gym, as_coach = resolved
            del self._pending[identity]
            return await self._start_link(identity, msg, None, typed_gym, as_coach)
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
        if pending.as_coach:
            # Atomic redemption: a code regenerated mid-flow revokes the whole
            # link — no plain-member partial state, no duplicate on retry.
            member = await self.store.link_member_as_coach(
                pending.gym_id, name, *identity, pending.invite_code
            )
            if member is None:
                del self._pending[identity]
                return await self.phraser(LINK_EXPIRED_INSTRUCTION, msg.text)
        else:
            if not await self._code_still_active(pending.gym_id, pending.invite_code):
                del self._pending[identity]
                return await self.phraser(LINK_EXPIRED_INSTRUCTION, msg.text)
            await self.store.link_member(pending.gym_id, name, *identity)
        # Cleared only after the write: a store error keeps the step retryable.
        del self._pending[identity]
        template = COACH_WELCOME_INSTRUCTION if pending.as_coach else WELCOME_INSTRUCTION
        instruction = template.format(name=name, gym=pending.gym_name)
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
        # Fresh start at the new Gym: new Member row (same person, same name),
        # old row untouched, channel identity re-pointed. The coach path
        # redeems atomically — a code regenerated mid-flow revokes the switch.
        if pending.as_coach:
            member = await self.store.link_member_as_coach(
                pending.gym_id, linked.member.name, *identity, pending.invite_code
            )
            if member is None:
                del self._pending[identity]
                return await self.phraser(
                    LINK_INACTIVE_INSTRUCTION.format(gym=linked.gym.name), msg.text
                )
        else:
            if not await self._code_still_active(pending.gym_id, pending.invite_code):
                del self._pending[identity]
                return await self.phraser(
                    LINK_INACTIVE_INSTRUCTION.format(gym=linked.gym.name), msg.text
                )
            await self.store.link_member(pending.gym_id, linked.member.name, *identity)
        # Cleared only after the write: a store error keeps the step retryable.
        del self._pending[identity]
        template = COACH_SWITCHED_INSTRUCTION if pending.as_coach else SWITCHED_INSTRUCTION
        instruction = template.format(new_gym=pending.gym_name, name=linked.member.name)
        return await self.phraser(instruction, msg.text)

    async def _code_still_active(self, gym_id: int, invite_code: str) -> bool:
        """Regenerating an Invite code invalidates flows the old code started.

        Coach codes need no pre-check here: they redeem atomically in the
        store (``link_member_as_coach`` / ``promote_to_coach``).
        """
        gym = await self.store.gym_by_invite_code(invite_code)
        return gym is not None and gym.id == gym_id

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
from agentg.models import Gym
from agentg.store import LinkedIdentity, LinkingStore

DEAD_END = (
    "¡Hola! 👋 Soy un coach que trabaja con gyms asociados, así que solo "
    "puedo darte de alta a través de tu gym. Consigue el enlace o el código QR "
    "de invitación de tu gym — lo tiene la recepción o tu coach —, tócalo y "
    "arrancamos."
)
NAME_CONFIRM = (
    "¡Qué gusto tenerte en {gym}! 🎉 Tengo tu nombre como {name} según tu "
    "perfil, ¿lo uso? Responde sí, o dime cómo te gustaría que te llame."
)
NAME_ASK = "¡Qué gusto tenerte en {gym}! 🎉 ¿Cómo te gustaría que te llame?"
WELCOME = (
    "¡Todo listo, {name} — qué gusto tenerte en {gym}! 💪 "
    "Avísame cuando estés en el gym y de ahí seguimos."
)
SAME_GYM = "Ya estás en {gym}, {name} — no hace falta volver a vincularte. ¿Qué toca hoy?"
LINK_INACTIVE = (
    "Ese enlace de invitación no parece estar activo. Sin bronca — sigues en {gym}."
)
SWITCH_CONFIRM = (
    "Ese es un enlace de invitación de {new_gym}. Cambiarte significa empezar "
    "de cero ahí — tu historial de entrenamiento se queda con {old_gym}. "
    "¿Quieres cambiarte? (sí / no)"
)
SWITCHED = (
    "¡Listo — qué gusto tenerte en {new_gym}, {name}! 💪 "
    "Empezamos de cero desde aquí; tu historial se quedó con tu gym anterior."
)
SWITCH_CANCELLED = "Sin problema — sigues con {gym}. 👍"
LINK_EXPIRED = (
    "Lo siento — esa invitación ya no está activa. 😕 Pídele a tu gym su enlace "
    "o código QR actual, tócalo y te doy de alta."
)

AFFIRMATIVES = {"yes", "y", "yep", "yeah", "yup", "ok", "okay", "sure", "si", "sí", "correct"}
NEGATIVES = {"no", "n", "nope", "nah"}
MAX_NAME_LENGTH = 100


def _normalized(text: str) -> str:
    return text.strip().lower().strip("!. ")


def _is_affirmative(text: str) -> bool:
    return _normalized(text) in AFFIRMATIVES


def _is_negative(text: str) -> bool:
    return _normalized(text) in NEGATIVES


def _clean_name(text: str) -> str:
    return " ".join(text.split())[:MAX_NAME_LENGTH]


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
            self._pending[identity] = _AwaitingSwitch(
                gym_id=gym.id, gym_name=gym.name, invite_code=gym.invite_code
            )
            return SWITCH_CONFIRM.format(new_gym=gym.name, old_gym=linked.gym.name)

        prefilled = _clean_name(msg.display_name)
        self._pending[identity] = _AwaitingName(
            gym_id=gym.id, gym_name=gym.name, invite_code=gym.invite_code, prefilled=prefilled
        )
        if prefilled:
            return NAME_CONFIRM.format(gym=gym.name, name=prefilled)
        return NAME_ASK.format(gym=gym.name)

    async def _confirm_name(
        self, identity: _Identity, msg: IncomingMessage, pending: _AwaitingName
    ) -> str:
        # A pasted Invite code mid-flow restarts linking, not a name change.
        typed_gym = await self.store.gym_by_invite_code(msg.text)
        if typed_gym is not None:
            del self._pending[identity]
            return self._start_link(identity, msg, None, typed_gym)
        if pending.prefilled and _is_affirmative(msg.text):
            name = pending.prefilled
        elif _is_negative(msg.text):
            pending.prefilled = ""  # they declined the prefill; ask outright
            return NAME_ASK.format(gym=pending.gym_name)
        else:
            name = _clean_name(msg.text)
        if not name:
            return NAME_ASK.format(gym=pending.gym_name)  # still waiting
        if not await self._code_still_active(pending.gym_id, pending.invite_code):
            del self._pending[identity]
            return LINK_EXPIRED
        await self.store.link_member(pending.gym_id, name, *identity)
        # Cleared only after the write: a store error keeps the step retryable.
        del self._pending[identity]
        return WELCOME.format(name=name, gym=pending.gym_name)

    async def _confirm_switch(
        self,
        identity: _Identity,
        msg: IncomingMessage,
        linked: LinkedIdentity | None,
        pending: _AwaitingSwitch,
    ) -> str:
        if linked is None:  # identity vanished mid-flow; start over
            del self._pending[identity]
            return DEAD_END
        if not _is_affirmative(msg.text):  # anything but a clear yes stays put
            del self._pending[identity]
            return SWITCH_CANCELLED.format(gym=linked.gym.name)
        if not await self._code_still_active(pending.gym_id, pending.invite_code):
            del self._pending[identity]
            return LINK_INACTIVE.format(gym=linked.gym.name)
        # Fresh start at the new Gym: new Member row (same person, same name),
        # old row untouched, channel identity re-pointed.
        await self.store.link_member(pending.gym_id, linked.member.name, *identity)
        # Cleared only after the write: a store error keeps the step retryable.
        del self._pending[identity]
        return SWITCHED.format(new_gym=pending.gym_name, name=linked.member.name)

    async def _code_still_active(self, gym_id: int, invite_code: str) -> bool:
        """Regenerating an Invite code invalidates flows the old code started."""
        gym = await self.store.gym_by_invite_code(invite_code)
        return gym is not None and gym.id == gym_id

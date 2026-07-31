"""The dashboard's per-browser EN/ES language layer (issue #106,
spec-dashboard §Language).

Language is per-browser, stored on no row: a toggle in the chrome persists
it in a long-lived cookie beside the session cookie; a first visit defaults
from ``Accept-Language``, falling back to Spanish — the product's one
no-signal default (#89). Chrome, weekdays, months, relative time and the
decimal mark translate; Exercise names, Workout names and the Member's own
words never do (#76).

Chat stays fully independent: the Agent keeps mirroring the conversation,
and bot-sent dashboard links follow the chat rule even when the dashboard
renders in the other language — nothing here touches the chat path.
"""

from __future__ import annotations

import re
from datetime import date

LANGS = ("es", "en")
DEFAULT_LANG = "es"

# The cookie beside the 90-day session cookie (#89): long-lived, so a
# Coach who picked a language keeps it across re-authentications.
LANG_COOKIE = "agentg_dashboard_lang"
LANG_COOKIE_TTL_SECONDS = 5 * 365 * 24 * 3600

WEEKDAYS = {
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}

MONTHS = {
    "es": ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"),
    "en": ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
}

# The Cards day-grid column initials, Mon first.
WEEKDAY_INITIALS = {
    "es": ("lu", "ma", "mi", "ju", "vi", "sá", "do"),
    "en": ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"),
}

DECIMAL_MARK = {"es": ",", "en": "."}

NOTE_KIND_LABELS = {
    "es": {
        "injury": "lesión",
        "preference": "preferencia",
        "goal": "objetivo",
        "constraint": "limitación",
        "safety": "seguridad",
        "other": "otro",
    },
    "en": {
        "injury": "injury",
        "preference": "preference",
        "goal": "goal",
        "constraint": "constraint",
        "safety": "safety",
        "other": "other",
    },
}

# Chrome strings. Every label, heading, tag, button and empty state —
# anything that is not a Member's own words or a row out of the Catalog.
STRINGS = {
    "es": {
        "settings": "Ajustes",
        "presets": "Presets",
        "search_placeholder": "Buscar por nombre",
        "view_table": "Tabla",
        "view_cards": "Tarjetas",
        "view_split": "Dividida",
        "members_count": "Miembros ({n})",
        "lapsed_tail": "Se perdieron ({n})",
        "new_tag": "nuevo",
        "snoozed_tag": "en pausa hasta el {date}",
        "lapsed_tag": "se perdió",
        "no_sessions_yet": "Aún sin sesiones",
        "trained_today": "entrenó hoy",
        "one_day_away": "1 día sin venir",
        "days_away": "{n} días sin venir",
        "band_hot": "Te necesitan ya",
        "band_warm": "Aflojando",
        "band_cool": "Al día",
        "grid_label": "últimas {n} semanas",
        "pick_a_member": "Elige un miembro",
        "back_to_roster": "← Todos los miembros",
        "member_since": "Miembro desde {date}",
        "one_session": "1 sesión",
        "n_sessions": "{n} sesiones",
        "last_session": "última sesión {date}",
        "routine": "Rutina",
        "no_routine": "Sin rutina activa",
        "sessions": "Sesiones",
        "visit_no_sets": "visita registrada, sin series",
        "one_set": "1 serie",
        "n_sets": "{n} series",
        "newer_page": "‹ más recientes",
        "older_page": "más antiguas ›",
        "page_x_of_y": "página {page} de {pages}",
        "last_weights": "Últimos pesos",
        "nothing_logged": "Nada registrado aún",
        "bodyweight": "peso corporal",
        "notes": "Notas",
        "no_notes": "Sin notas",
        "retired_tail": "Retiradas ({n})",
        "retired_on": "retirada el {date}",
        "verbatim_tag": "textual",
        # The Routine editor (issue #100).
        "edit": "Editar",
        "chip_agent": "Gestionada por el agente",
        "chip_coach": "Escrita por un coach",
        "chip_coach_named": "Escrita por {name}",
        "chip_consequence": (
            "Al guardar, este plan pasa a ser tuyo — el agente dejará de ajustarlo."
        ),
        "editor_title": "Rutina de {name}",
        "stale_error": (
            "Esta Rutina cambió mientras la editabas — aquí tienes la versión "
            "actual. Vuelve a aplicar tus cambios encima."
        ),
        "empty_routine_error": "Una Rutina necesita al menos un día con ejercicios.",
        "empty_workout_error": (
            "Cada día necesita al menos un ejercicio — o vacía el bloque y deja su "
            "selector en «— día —»."
        ),
        "undated_block_error": (
            "Un bloque con contenido no tiene día — elige un día de la semana o "
            "vacía el bloque."
        ),
        "duplicate_weekday_error": "Cada día de la semana solo puede aparecer una vez.",
        "bad_weekday_error": "Uno de los días no es válido.",
        "bad_sets_error": "Las series deben ser un número (p. ej. «squat, 4, 8-10»).",
        "unknown_exercises_error": (
            "Estos ejercicios no están en el catálogo: {names}. Escríbelos "
            "exactamente como aparecen en el catálogo."
        ),
        "workout_name_too_long": "El nombre del día no puede pasar de 100 caracteres.",
        "reps_too_long": (
            "Las repeticiones no pueden pasar de 40 caracteres (p. ej. «8-12»)."
        ),
        "sets_range_error": "Las series deben ser un número entre 1 y 99.",
        "editor_help": (
            "Un ejercicio por línea: nombre, series, repeticiones (series "
            "y repeticiones opcionales). Para quitar un día, vacía sus ejercicios y deja "
            "su selector en «— día —»."
        ),
        "catalog_label": "Catálogo de ejercicios",
        "save_routine": "Guardar Rutina",
        "pick_day": "— día —",
        "workout_name_placeholder": "Nombre (p. ej. Piernas)",
        # The safety-flag banner and roster marker (issue #101).
        "flag_tag": "⚑ seguridad",
        "safety_section": "⚑ Seguridad",
        "tick_off": "Marcar como vista",
        "flag_seen_by": "Vista por {who} el {date}",
        "flag_expired_unseen": "caducada, nunca vista",
        # Settings screen.
        "settings_title": "Ajustes",
        "invite_section": "Enlace de invitación",
        "invite_blurb": "El que usan los nuevos miembros para unirse a",
        "coach_section": "Enlace para coaches",
        "coach_blurb": "Privado: reenvíaselo solo a quien quieras sumar como coach.",
        "gym_name_section": "Nombre del gimnasio",
        "gym_name_help": "Es el nombre que ven los miembros al unirse.",
        "copy": "Copiar",
        "copied": "Copiado",
        "copy_failed": "No se pudo copiar",
        "regenerate": "Regenerar",
        "confirm_word": "regenerar",
        "confirm_prompt": "Escribe <b>{word}</b> para confirmar:",
        "confirm_mismatch": "Escribe <b>{word}</b> para confirmar la regeneración.",
        "invite_warning": (
            "Regenerar el enlace invalida el código actual — quien esté a mitad de "
            "vincularse tendrá que empezar de nuevo con el enlace nuevo."
        ),
        "coach_warning": (
            "Regenerar el enlace de coach invalida el código actual. Los coaches "
            "que ya se vincularon conservan su acceso."
        ),
        "gym_name_empty": "El nombre del gimnasio no puede estar vacío.",
        "save": "Guardar",
        "back_to_dashboard": "Volver al dashboard",
        "presets_title": "Presets",
        "create_preset": "Crear Preset",
        "preset_name": "Nombre del Preset",
        "preset_name_empty": "El nombre del Preset no puede estar vacío.",
        "preset_name_too_long": "El nombre del Preset no puede pasar de 100 caracteres.",
        "duplicate_preset_name": "Ya existe un Preset con ese nombre en este gimnasio.",
        "no_presets": "Aún no hay Presets.",
        "edit_preset": "Editar",
        "preset_editor_title": "Preset: {name}",
        "preset_chip": "Preset: {name}",
        "back_to_presets": "Volver a Presets",
        "apply_preset": "Aplicar Preset",
        "apply_members": "Miembros",
        "apply_all": "Todos los miembros",
        "apply": "Aplicar",
        "no_members_to_apply": "No hay miembros disponibles.",
    },
    "en": {
        "settings": "Settings",
        "presets": "Presets",
        "search_placeholder": "Search by name",
        "view_table": "Table",
        "view_cards": "Cards",
        "view_split": "Split",
        "members_count": "Members ({n})",
        "lapsed_tail": "Lapsed ({n})",
        "new_tag": "new",
        "snoozed_tag": "paused until {date}",
        "lapsed_tag": "lapsed",
        "no_sessions_yet": "No sessions yet",
        "trained_today": "trained today",
        "one_day_away": "1 day away",
        "days_away": "{n} days away",
        "band_hot": "Needs you now",
        "band_warm": "Slipping",
        "band_cool": "On track",
        "grid_label": "last {n} weeks",
        "pick_a_member": "Pick a member",
        "back_to_roster": "← All members",
        "member_since": "Member since {date}",
        "one_session": "1 session",
        "n_sessions": "{n} sessions",
        "last_session": "last session {date}",
        "routine": "Routine",
        "no_routine": "No active routine",
        "sessions": "Sessions",
        "visit_no_sets": "visit logged, no sets",
        "one_set": "1 set",
        "n_sets": "{n} sets",
        "newer_page": "‹ newer",
        "older_page": "older ›",
        "page_x_of_y": "page {page} of {pages}",
        "last_weights": "Last weights",
        "nothing_logged": "Nothing logged yet",
        "bodyweight": "bodyweight",
        "notes": "Notes",
        "no_notes": "No notes",
        "retired_tail": "Retired ({n})",
        "retired_on": "retired {date}",
        "verbatim_tag": "as written",
        # The Routine editor (issue #100).
        "edit": "Edit",
        "chip_agent": "Agent-managed",
        "chip_coach": "Coach-authored",
        "chip_coach_named": "Coach-authored — {name}",
        "chip_consequence": "Saving makes this plan yours — the Agent will stop adjusting it.",
        "editor_title": "{name}'s routine",
        "stale_error": (
            "This routine changed while you were editing — here is the current "
            "version. Re-apply your changes on top of it."
        ),
        "empty_routine_error": "A routine needs at least one day with exercises.",
        "empty_workout_error": (
            "Every day needs at least one exercise — or empty the block and leave "
            "its selector on «— day —»."
        ),
        "undated_block_error": (
            "A block with content has no day — pick a weekday or empty the block."
        ),
        "duplicate_weekday_error": "Each weekday can only appear once.",
        "bad_weekday_error": "One of the days is not valid.",
        "bad_sets_error": "Sets must be a number (e.g. «squat, 4, 8-10»).",
        "unknown_exercises_error": (
            "These exercises are not in the catalog: {names}. Type them exactly "
            "as they appear in the catalog."
        ),
        "workout_name_too_long": "The workout name cannot exceed 100 characters.",
        "reps_too_long": "Reps cannot exceed 40 characters (e.g. «8-12»).",
        "sets_range_error": "Sets must be a number between 1 and 99.",
        "editor_help": (
            "One exercise per line: name, sets, reps (sets and reps optional). "
            "To remove a day, clear its exercises and leave its selector on «— day —»."
        ),
        "catalog_label": "Exercise catalog",
        "save_routine": "Save routine",
        "pick_day": "— day —",
        "workout_name_placeholder": "Name (e.g. Legs)",
        # The safety-flag banner and roster marker (issue #101).
        "flag_tag": "⚑ safety",
        "safety_section": "⚑ Safety",
        "tick_off": "Mark as seen",
        "flag_seen_by": "Seen by {who} on {date}",
        "flag_expired_unseen": "expired, never seen",
        # Settings screen.
        "settings_title": "Settings",
        "invite_section": "Invite link",
        "invite_blurb": "The one new members use to join",
        "coach_section": "Coach link",
        "coach_blurb": "Private: forward it only to whoever you want to add as a coach.",
        "gym_name_section": "Gym name",
        "gym_name_help": "It is the name members see when they join.",
        "copy": "Copy",
        "copied": "Copied",
        "copy_failed": "Could not copy",
        "regenerate": "Regenerate",
        "confirm_word": "regenerate",
        "confirm_prompt": "Type <b>{word}</b> to confirm:",
        "confirm_mismatch": "Type <b>{word}</b> to confirm the regeneration.",
        "invite_warning": (
            "Regenerating the link invalidates the current code — anyone halfway "
            "through linking will have to start over with the new link."
        ),
        "coach_warning": (
            "Regenerating the coach link invalidates the current code. Coaches "
            "who already linked keep their access."
        ),
        "gym_name_empty": "The gym name cannot be empty.",
        "save": "Save",
        "back_to_dashboard": "Back to the dashboard",
        "presets_title": "Presets",
        "create_preset": "Create preset",
        "preset_name": "Preset name",
        "preset_name_empty": "The preset name cannot be empty.",
        "preset_name_too_long": "The preset name cannot exceed 100 characters.",
        "duplicate_preset_name": "A preset with that name already exists in this gym.",
        "no_presets": "No presets yet.",
        "edit_preset": "Edit",
        "preset_editor_title": "Preset: {name}",
        "preset_chip": "Preset: {name}",
        "back_to_presets": "Back to presets",
        "apply_preset": "Apply preset",
        "apply_members": "Members",
        "apply_all": "All members",
        "apply": "Apply",
        "no_members_to_apply": "There are no members to apply it to.",
    },
}


def resolve_lang(cookie_value: str | None, accept_language: str | None) -> str:
    """The language one browser reads in.

    The toggle's cookie wins; without it the browser's first
    ``Accept-Language`` range decides; anything without a signal falls back
    to Spanish (#89).
    """
    if cookie_value in LANGS:
        return cookie_value
    if accept_language:
        first_range = accept_language.split(",", 1)[0].split(";", 1)[0].strip().lower()
        if first_range.startswith("en"):
            return "en"
    return DEFAULT_LANG


def fmt_date(d: date, lang: str) -> str:
    """``15 jul 2026`` / ``15 Jul 2026`` — months translate."""
    return f"{d.day} {MONTHS[lang][d.month - 1]} {d.year}"


def fmt_number(value: float, lang: str) -> str:
    """A weight with the language's decimal mark: ``62,5`` / ``62.5``."""
    return f"{value:g}".replace(".", DECIMAL_MARK[lang])


def away_text(has_sessions: bool, gap_days: int, lang: str) -> str:
    """The shared Gap wording for roster rows and the Member page header —
    one helper so the surfaces never disagree."""
    t = STRINGS[lang]
    if not has_sessions:
        return t["no_sessions_yet"]
    if gap_days == 0:
        return t["trained_today"]
    if gap_days == 1:
        return t["one_day_away"]
    return t["days_away"].format(n=gap_days)


# --- The source-language tag on the Member's own words ---
#
# Notes and Set comments are written by the Agent in whatever language the
# Member chats in, and no row stores which. The tag only needs to answer
# "is this quote in the language the Coach is reading?", so a small
# stopword vote is enough — it is a provenance hint, not a classifier.
# Ambiguous text defaults to Spanish, the product's no-signal default.
# "me" votes in both lists: first-person quotes are the common short case
# in both languages, so the other words decide — "i"/"i'm" stay
# English-only, and common short Spanish signals (duele, siento, mucho,
# bien, mal, canso, no) break the tie the other way.

_ES_WORDS = frozenset(
    "el la los las de del que un una con por para mi mis su sus se al no "
    "quiero puedo solo antes después hacer muy más cuando porque entrenar "
    "dolor pero también tiene estoy lo es son está me duele siento mucho "
    "bien mal canso".split()
)
_EN_WORDS = frozenset(
    "the and to of in is it i me i'm my can want only before with for on at "
    "not hates will them train pain week but also have has am are was were "
    "would could should this that these those from after when what who "
    "help i'll i've i'd im".split()
)
_ES_ACCENTS = re.compile(r"[áéíóúñ¿¡]")
_WORDS = re.compile(r"[a-záéíóúñü]+(?:'[a-z]+)?")


def detect_language(text: str) -> str:
    """``"es"`` or ``"en"`` — the language a quote reads as, Spanish on a tie."""
    words = _WORDS.findall(text.lower())
    es = sum(word in _ES_WORDS for word in words)
    en = sum(word in _EN_WORDS for word in words)
    if _ES_ACCENTS.search(text):
        es += 2
    return "en" if en > es else "es"


def verbatim(text_lang: str, ui_lang: str) -> str:
    """The source-language tag a foreign quote carries: ``EN · textual`` /
    ``ES · as written``. A quote in the Coach's own language needs none."""
    if text_lang == ui_lang:
        return ""
    return f"{text_lang.upper()} · {STRINGS[ui_lang]['verbatim_tag']}"

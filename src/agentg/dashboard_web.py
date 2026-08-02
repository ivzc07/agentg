"""The dashboard's embedded HTTP server (spec-dashboard §Stack).

aiohttp on the bot's existing event loop, next to the long poller. The door
is three routes:

- ``GET /`` — the signed-in landing: the roster in one of three views the
  Coach switches between via a segmented control in the top bar
  (``?view=table|cards|split``, issue #106) — Table, Cards (severity bands
  plus a per-day attendance grid) and Split (a permanent roster rail with a
  Member in the right pane) — gated on the session cookie *and* a
  per-request ``is_coach`` re-check.
- ``GET /members/<id>`` — the Member's page: the read-only training record
  (issue #99) with the safety-flag banner (issue #101) plus the Routine
  editor's entry point (issue #100). Opened from Table or Cards it hides
  the switcher; with ``?view=split`` it fills Split's right pane and the
  switcher stays. A departed, forgotten, or mistyped id lands on one shared
  bare 404 — no tombstone, no "this member left" wording.
- ``GET /members/<id>/routine`` — the Routine editor: a structured
  weekday-to-exercises form whose header always carries the ownership chip
  (spec-dashboard §Routines & Presets).
- ``POST /members/<id>/routine`` — the editor's save: coach-authored and
  actor-stamped through the supersession machinery, refused with the fresh
  version when the active Routine changed since the editor loaded. A
  successful save messages the Member: their coach, named, plus the new
  plan. A running Session is never disturbed — the new plan simply applies
  from the next chat turn.
- ``POST /members/<id>/flags/<note_id>/tick-off`` — the page's other write:
  a Coach acknowledges a safety flag (who and when), which silences the
  roster marker without retiring the Note.
- ``GET /login/<token>`` — an interstitial that never spends the token, so
  a link-preview fetch (Telegram builds one unless the bot disables it)
  can't burn the one-time link; the browser auto-submits…
- ``POST /login/<token>`` — …which is what actually redeems the token and
  sets the session cookie.

Behind the same gate sits the tenant Settings screen (spec-dashboard
§Settings): the member invite link with its QR, the coach link, and the
gym name — nothing else. Both Regenerate buttons sit behind a typed
confirm, enforced client-side and again on the POST, because regenerating
invalidates half-finished linking conversations.

Anything unknown, used, expired, or demoted lands on the same friendly
"send /dashboard to your bot" page — never an error. (The door pages stay
Spanish, the product's no-signal default: the language cookie does not
exist yet when they render.)

The session is a stateless HMAC-signed cookie (member id, gym id, expiry),
re-issued on every authenticated visit so an active Coach never
re-authenticates within the 90-day sliding window. Beside it, a long-lived
language cookie carries the per-browser EN/ES toggle (issue #106); without
it, pages default from ``Accept-Language`` with a Spanish fallback
(spec-dashboard §Language). Three things never translate: Exercise names,
Workout names, and the Member's own words — Notes and Set comments render
verbatim with a small source-language tag when they differ from the
language the Coach reads.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from html import escape
from pathlib import Path
from urllib.parse import quote, unquote

import qrcode
import qrcode.image.svg
from aiohttp import web

from agentg.checkin_sweep import Notifier
from agentg.dashboard_i18n import (
    DECIMAL_MARK,
    LANG_COOKIE,
    LANG_COOKIE_TTL_SECONDS,
    LANGS,
    MONTHS,
    STRINGS,
    WEEKDAY_INITIALS,
    WEEKDAYS,
    detect_language,
    resolve_lang,
)
from agentg.dashboard_store import (
    DashboardStore,
    MemberPage,
    RosterRow,
    RoutineDayView,
)
from agentg.linking_store import GYM_NAME_MAX_LENGTH, LinkingStore
from agentg.models import Gym, Member, RoutinePreset
from agentg.routines import (
    DuplicatePresetNameError,
    ExerciseSpec,
    NoPresetMasterError,
    StaleRoutineError,
    UnknownExercisesError,
    WorkoutSpec,
)

logger = logging.getLogger(__name__)

SESSION_COOKIE = "agentg_dashboard"
SESSION_TTL = timedelta(days=90)

Clock = Callable[[], datetime]

VIEWS = ("table", "cards", "split")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# The door pages are Spanish — the product's no-signal default
# (spec-dashboard §Language): they render before the language cookie exists.
BOUNCE_TITLE = "Este enlace ya no sirve"
BOUNCE_BODY = (
    "Los enlaces al dashboard caducan y solo se pueden usar una vez. "
    "Envía <b>/dashboard</b> a tu bot en Telegram para recibir uno nuevo."
)

# Spanish aliases of the editor's STRINGS keys, for chat-side use (the
# member notice follows the chat rule, not the dashboard's language) and
# for tests asserting the no-signal default.
AGENT_CHIP = STRINGS["es"]["chip_agent"]
COACH_CHIP = STRINGS["es"]["chip_coach"]
CONSEQUENCE_LINE = STRINGS["es"]["chip_consequence"]
STALE_ERROR = STRINGS["es"]["stale_error"]
EMPTY_ROUTINE_ERROR = STRINGS["es"]["empty_routine_error"]
EMPTY_WORKOUT_ERROR = STRINGS["es"]["empty_workout_error"]
UNDATED_BLOCK_ERROR = STRINGS["es"]["undated_block_error"]
DUPLICATE_WEEKDAY_ERROR = STRINGS["es"]["duplicate_weekday_error"]
BAD_WEEKDAY_ERROR = STRINGS["es"]["bad_weekday_error"]
BAD_SETS_ERROR = STRINGS["es"]["bad_sets_error"]
UNKNOWN_EXERCISES_ERROR = STRINGS["es"]["unknown_exercises_error"]
NAME_TOO_LONG_ERROR = STRINGS["es"]["workout_name_too_long"]
REPS_TOO_LONG_ERROR = STRINGS["es"]["reps_too_long"]
SETS_RANGE_ERROR = STRINGS["es"]["sets_range_error"]

# Column limits the editor enforces before save — SQLite would silently
# accept an overflow, Postgres would answer with a DataError (a 500).
WORKOUT_NAME_MAX_LENGTH = 100  # Workout.name String(100)
REPS_MAX_LENGTH = 40  # WorkoutExercise.reps String(40)
# Sets are small by nature; unbounded ints overflow at flush (OverflowError
# on SQLite, DataError on Postgres).
SETS_MIN, SETS_MAX = 1, 99

# The typed confirm gating both Regenerate buttons (spec-dashboard
# §Settings): the word must be typed before the POST does anything, client-
# and server-side, because regenerating invalidates half-finished linking
# conversations. The word follows the page language; this constant is the
# Spanish one, kept for callers that predate the toggle.
REGENERATE_CONFIRM = STRINGS["es"]["confirm_word"]


# --- The design tokens and shared shell (issue: the UX/UI redesign) ---
#
# One dark editorial language for every page, adopted from the parked
# docs/prototypes/coach-dashboard-v3-dark.html ("the look is wanted"):
# pure black ground, two flat surfaces, hairline rules instead of card
# chrome, zero corner radius except pill chips, white as the loudest
# accent, a mono-uppercase eyebrow as the one typographic signature, and
# color reserved for training state — magenta kept, coral missed/red, amber
# slipping, purple extra. All values live here as custom properties; the
# per-surface style blocks below only compose them.

# Login door and bounce pages: one centered column, one action.
# Every form disables its submit buttons once a submit is on its way —
# a double-clicked Apply must not message every Member twice. The timeout
# lets the click's own submit complete before the buttons grey out. A
# cancelled confirm (the retire form) prevents the default but still
# bubbles here — defaultPrevented keeps the button alive. And a page
# restored from the back/forward cache comes back with its frozen DOM, so
# pageshow re-arms what a past submit disabled.
SUBMIT_GUARD_SCRIPT = """
document.addEventListener("submit", function (e) {
  if (e.defaultPrevented) return;
  var buttons = e.target.querySelectorAll("button[type=submit]");
  setTimeout(function () { buttons.forEach(function (b) { b.disabled = true; }); }, 0);
});
window.addEventListener("pageshow", function (e) {
  if (!e.persisted) return;
  document.querySelectorAll("form button[type=submit]").forEach(function (b) {
    if (!b.closest("form[data-confirm]")) b.disabled = false;
  });
});
"""


STATIC_DIR = Path(__file__).parent / "static"


@lru_cache(maxsize=4)
def _asset_version(filename: str) -> str:
    """A content hash in a static asset's URL, so a deploy never serves a
    90-day-cookie Coach last release's copy from their browser cache."""
    digest = hashlib.md5((STATIC_DIR / filename).read_bytes(), usedforsecurity=False).hexdigest()
    return digest[:8]


def _document(
    title: str, lang: str, body: str, *, scripts: str = "", with_htmx: bool = False
) -> str:
    """The one page skeleton every surface renders through. All styling
    lives in static/dashboard.css (ADR 0003): one cacheable sheet, served
    whole from the package - no build step. Pages that save in place
    (issue #128) opt into the vendored htmx with ``with_htmx``."""
    script_html = f"<script>{SUBMIT_GUARD_SCRIPT}{scripts}</script>"
    htmx_tag = (
        f'\n<script src="/static/htmx.min.js?v={_asset_version("htmx.min.js")}" defer></script>'
        if with_htmx
        else ""
    )
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="/static/dashboard.css?v={_asset_version("dashboard.css")}">{htmx_tag}
</head>
<body>
{body}
{script_html}
</body>
</html>"""


def _page(title: str, body: str, extra: str = "", lang: str = "es") -> str:
    inner = f"""<h1>{title}</h1>"""
    if body:
        inner += f"<p>{body}</p>"
    if extra:
        inner += extra
    content = f"""<div class="door"><div class="card">{inner}</div></div>"""
    return _document(title, lang, content)


def _bounce_page() -> str:
    return _page(BOUNCE_TITLE, BOUNCE_BODY)


def _invite_url(bot_username: str, code: str) -> str:
    """The deep link a joiner taps: ``t.me/<bot>?start=<code>``."""
    return f"https://t.me/{bot_username}?start={code}"


@lru_cache(maxsize=32)
def _qr_svg(data: str) -> str:
    """An inline SVG QR for the member invite link — a poster the Coach can
    print. The coach link gets none: it is forwarded privately, not posted.

    Memoized on the URL: the encode runs on the event loop the bot shares
    with Telegram long-polling, so after the first render of a link every
    Settings render is a dict lookup. Regeneration changes the URL, so the
    stale entry never serves again; the small cap bounds churn across
    regenerations.
    """
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return image.to_string(encoding="unicode")


# --- Language plumbing (issue #106) ---


def _next_path_sans_done(request: web.Request) -> str:
    """The page's own URL for round-trips (the language toggle), with the
    one-shot ``done`` flash stripped - a notice should not survive a
    toggle, a refresh of the toggled page, or a bookmark."""
    url = request.rel_url
    if "done" not in url.query:
        return url.path_qs
    return str(url.with_query([(k, v) for k, v in url.query.items() if k != "done"]))


def _done_notice(done: str | None, t: dict) -> str:
    """The one-line confirmation a ``?done=<key>`` redirect carries (issue
    #129). Only keys the copy table knows render; anything else is
    nothing — never an error, never echoed back."""
    key = f"done_{done}" if done else ""
    return f'<p class="notice-ok">{t[key]}</p>' if key in t else ""


def _is_htmx(request: web.Request) -> bool:
    """True when htmx is asking for a fragment swap (issue #128)."""
    return request.headers.get("HX-Request") == "true"


def _lang_of(request: web.Request) -> str:
    """The language this browser reads: the toggle's cookie, else
    ``Accept-Language``, else Spanish."""
    return resolve_lang(request.cookies.get(LANG_COOKIE), request.headers.get("Accept-Language"))


def _lang_toggle(next_path: str, lang: str) -> str:
    """The EN/ES toggle in the chrome; both links round-trip through
    ``/lang/<lang>`` and land back on the page the Coach was reading. The
    language being read is the marked one."""
    target = quote(next_path, safe="")
    links = " · ".join(
        f'<a href="/lang/{code}?next={target}"'
        f'{" aria-current=\"true\"" if code == lang else ""}>{code.upper()}</a>'
        for code in ("en", "es")
    )
    return f'<span class="lang-toggle">{links}</span>'


def _safe_next(raw: str | None) -> str:
    """A redirect target that can only ever be a path on this dashboard.

    The ONE guard both doors use (the magic-link redeem and the language
    toggle). A plain startswith check is not enough: "/\\t/evil.com" passes
    it, and yarl then normalizes the Location to the protocol-relative
    //evil.com — an open redirect. Any control or whitespace character,
    raw or percent-encoded, kills the path; so does anything that isn't a
    single-leading-slash local path."""
    if not raw:
        return "/"
    for candidate in (raw, unquote(raw)):
        if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in candidate):
            return "/"
        if not candidate.startswith("/") or candidate.startswith("//"):
            return "/"
    return raw


# --- The shared chrome: segmented view control, search, toggle, settings ---

SEARCH_SCRIPT = """
// Live, name-only, accent-insensitive filter. It only hides rows — the Gap
// sort never moves — and a lapsed match auto-expands the tail (a manually
// opened tail is never slammed shut). Identical in the three views
// (spec-dashboard §The roster).
const box = document.getElementById("search");
const lapsed = document.getElementById("lapsed");
const nomatch = document.getElementById("no-matches");
// The chrome's Members (N), rewritten to "X de N" while a query filters
// (issue #127); its resting label is restored when the box empties.
const counter = document.getElementById("members-count");
const restingLabel = counter ? counter.textContent : "";
const norm = (s) => s.normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
box.addEventListener("input", () => {
  const q = norm(box.value.trim());
  let lapsedHit = false;
  let shown = 0;
  let activeShown = 0;  // lapsed stay out of the counters (spec §The roster)
  document.querySelectorAll("[data-name]").forEach((row) => {
    const hit = !q || norm(row.dataset.name).includes(q);
    row.hidden = !hit;
    if (hit) {
      shown += 1;
      if (lapsed && lapsed.contains(row)) { if (q) lapsedHit = true; }
      else activeShown += 1;
    }
  });
  if (lapsed && lapsedHit) lapsed.open = true;
  if (nomatch) nomatch.hidden = shown > 0;
  if (counter) {
    counter.textContent = q
      ? counter.dataset.fmt.replaceAll("{shown}", activeShown).replaceAll("{total}", counter.dataset.total)
      : restingLabel;
  }
});
"""


def _seg(view: str, t: dict) -> str:
    """The Table / Cards / Split segmented control — a product control, not
    a designer's shortlist (spec-dashboard §The roster)."""
    links = "".join(
        f'<a href="/?view={v}"{" aria-current=\"true\"" if v == view else ""}>'
        f'{t[f"view_{v}"]}</a>'
        for v in VIEWS
    )
    return f'<nav class="seg" aria-label="{escape(t["nav_views"], quote=True)}">{links}</nav>'


def _chrome(
    gym_name: str,
    t: dict,
    next_path: str,
    lang: str,
    *,
    view: str | None = None,
    count: int | None = None,
    active: str | None = None,
) -> str:
    """The top bar every screen shares: gym name, the view switcher with the
    search beside it on roster pages (``view`` set), the Presets and
    Settings links with an active state, and the language toggle."""
    count_html = (
        f'<span class="count" id="members-count" data-total="{count}"'
        f' data-fmt="{escape(t["match_count"], quote=True)}">'
        f'{t["members_count"].format(n=count)}</span>' if count is not None else ""
    )
    seg = _seg(view, t) if view is not None else ""
    search = (
        f'<label class="sr" for="search">{t["search_placeholder"]}</label>'
        f'<input id="search" type="search" placeholder="{t["search_placeholder"]}" autocomplete="off">'
        if view is not None
        else ""
    )

    def quick(href: str, key: str) -> str:
        current = ' aria-current="true"' if active == key else ""
        return f'<a href="{href}"{current}>{t[key]}</a>'

    return f"""<header class="top">
<h1>{escape(gym_name)}</h1>{count_html}
{seg}
{search}
<span class="spacer"></span>
<nav class="quick" aria-label="{escape(t["nav_sections"], quote=True)}">{quick("/presets", "presets")}{quick("/settings", "settings")}</nav>
{_lang_toggle(next_path, lang)}
</header>"""


# --- The Member page (issue #99, spec-dashboard §The Member page) ---
#
# Read-only apart from the safety-flag banner's Tick off (issue #101); the
# Routine Edit entry point is a later ticket. One shape under all three
# roster views; opened from Table or Cards the switcher hides, in Split it
# stays.


def _not_found() -> web.Response:
    """The shared dead end: a departed, forgotten, or mistyped Member id all
    get the same bare 404 — no tombstone, no "this member left" wording, so
    the two exits stay indistinguishable (spec-dashboard §What a Coach sees)."""
    return web.Response(status=404, text="404", content_type="text/plain")


# --- The Routine editor (issue #100, spec-dashboard §Routines & Presets) ---
#
# One form: a block per pinned day (weekday select, Workout name, exercises
# one per line as "name, sets, reps" — structure only, never weights), a
# hidden stamp of the Routine it loaded for the stale-save check, and the
# ownership chip always in the header. A day comes off the Routine by clearing
# the whole block (exercises AND weekday); a half-filled block — content
# without a weekday, a weekday without exercises — is a refused mistake,
# never a silent drop.

EditorDay = tuple[int | None, str, str]  # (weekday, workout name, exercise lines)


@dataclass(frozen=True)
class RoutineEditorView:
    """The shared editor-facing subset for Members and Preset masters."""

    member_id: int
    name: str
    routine: list
    routine_id: int | None
    coach_authored: bool
    routine_author: str | None
    routine_preset_name: str | None = None

# The chat notice a web save sends the Member (issue #77, named per #91):
# deterministic and chat-side, so it follows the chat rule (Spanish like
# the nudges), never the dashboard's per-browser language.
ROUTINE_NOTICE = "Tu coach {coach} actualizó tu Rutina 📋\n{plan}"


def _days_from_view(view: MemberPage | RoutineEditorView) -> list[EditorDay]:
    """The active Routine as editor blocks, exercises back to one-per-line."""
    days: list[EditorDay] = []
    for day in view.routine:
        lines = []
        for name, sets, reps in day.exercises:
            line = name
            if sets is not None or reps is not None:
                line += f", {sets if sets is not None else ''}"
            if reps is not None:
                line += f", {reps}"
            lines.append(line)
        days.append((day.weekday, day.name, "\n".join(lines)))
    return days


def _parse_workouts_from_json(raw_workouts: list, lang: str) -> list[WorkoutSpec]:
    """The JSON editor body into WorkoutSpecs — the same rules as the form
    parser, with the same Coach-readable error messages."""
    t = STRINGS[lang]
    specs = []
    seen_weekdays: set[int] = set()
    for item in raw_workouts:
        if not isinstance(item, dict):
            raise ValueError(t["bad_weekday_error"])
        weekday = item.get("weekday")
        if weekday is None or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValueError(t["bad_weekday_error"])
        if weekday in seen_weekdays:
            raise ValueError(t["duplicate_weekday_error"])
        seen_weekdays.add(weekday)
        name = item.get("name", "")
        if not isinstance(name, str):
            name = ""
        raw_exercises = item.get("exercises")
        if not isinstance(raw_exercises, list) or not raw_exercises:
            raise ValueError(t["empty_workout_error"])
        exercises = []
        for ex in raw_exercises:
            if not isinstance(ex, dict):
                raise ValueError(t["bad_sets_error"])
            exercise_name = ex.get("exercise", "")
            if not isinstance(exercise_name, str) or not exercise_name.strip():
                raise ValueError(t["empty_workout_error"])
            sets = ex.get("sets")
            if sets is not None:
                if not isinstance(sets, int):
                    raise ValueError(t["bad_sets_error"])
                if not SETS_MIN <= sets <= SETS_MAX:
                    raise ValueError(t["sets_range_error"])
            reps = ex.get("reps")
            if reps is not None:
                if not isinstance(reps, str):
                    reps = str(reps)
                if len(reps) > REPS_MAX_LENGTH:
                    raise ValueError(t["reps_too_long"])
            exercises.append(ExerciseSpec(exercise_name, sets, reps))
        if len(name) > WORKOUT_NAME_MAX_LENGTH:
            raise ValueError(t["workout_name_too_long"])
        specs.append(WorkoutSpec(weekday, name or WEEKDAYS[lang][weekday], exercises))
    return specs


def _plain_scheme(sets: int | None, reps: str | None) -> str:
    """A set/rep scheme for plain text (the chat notice) — ``_scheme``'s
    unescaped, HTML-free sibling."""
    if sets is not None and reps is not None:
        return f" {sets}×{reps}"
    if reps is not None:
        return f" {reps}"
    if sets is not None:
        return f" {sets}"
    return ""


def routine_notice(coach_name: str, workouts: list[WorkoutSpec]) -> str:
    """The message the Member gets after a web save: their coach, named,
    plus the new plan (issues #77, #91)."""
    plan = "\n".join(
        f"{WEEKDAYS['es'][w.weekday]} — {w.name}: "
        + ", ".join(e.exercise + _plain_scheme(e.sets, e.reps) for e in w.exercises)
        for w in sorted(workouts, key=lambda w: w.weekday)
    )
    return ROUTINE_NOTICE.format(coach=coach_name, plan=plan)



def _preset_editor_view(preset: RoutinePreset, master: dict | None) -> RoutineEditorView:
    routine = [
        RoutineDayView(
            workout["weekday"],
            workout["name"],
            [(e["exercise"], e["sets"], e["reps"]) for e in workout["exercises"]],
        )
        for workout in (master["workouts"] if master else [])
    ]
    return RoutineEditorView(
        member_id=preset.id,
        name=preset.name,
        routine=routine,
        routine_id=master["routine_id"] if master else None,
        coach_authored=True,
        routine_author=master["created_by_name"] if master else None,
        routine_preset_name=preset.name,
    )


# --- The tenant Settings screen (spec-dashboard §Settings) ---


def _copy_button(url: str, t: dict) -> str:
    return (
        f'<button type="button" class="copy" data-copy="{escape(url, quote=True)}"'
        f' data-done="{escape(t["copied"], quote=True)}"'
        f' data-failed="{escape(t["copy_failed"], quote=True)}">'
        f'{t["copy"]}</button>'
    )


def _regenerate_form(action: str, warning: str, t: dict) -> str:
    """A Regenerate button the page script keeps disabled until the confirm
    word is typed. The script also applies the initial disable: without JS
    the button stays live and the POST's own word check — the load-bearing
    one — still refuses a wrong confirm."""
    word = t["confirm_word"]
    return f"""<form method="post" action="{action}" data-confirm="{word}">
<p>{warning}</p>
<p><label>{t["confirm_prompt"].format(word=word)}
<input type="text" name="confirm" autocomplete="off" required></label>
<button type="submit">{t["regenerate"]}</button></p>
</form>"""


SETTINGS_SCRIPT = """
document.querySelectorAll("button.copy").forEach(function (button) {
  button.addEventListener("click", function () {
    // navigator.clipboard needs a secure context — plain-HTTP origins leave
    // it undefined — and writeText itself can reject (denied permission).
    var restore = function () {
      setTimeout(function () { button.textContent = button.dataset.idle; }, 2000);
    };
    button.dataset.idle = button.dataset.idle || button.textContent;
    if (!navigator.clipboard) {
      button.textContent = button.dataset.failed;
      restore();
      return;
    }
    navigator.clipboard.writeText(button.dataset.copy).then(
      function () { button.textContent = button.dataset.done; restore(); },
      function () { button.textContent = button.dataset.failed; restore(); }
    );
  });
});
document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  var input = form.querySelector("input[name=confirm]");
  var submit = form.querySelector("button[type=submit]");
  submit.disabled = true;
  input.addEventListener("input", function () {
    submit.disabled = input.value.trim().toLowerCase() !== form.dataset.confirm;
  });
});
"""


def _settings_page(
    gym: Gym, bot_username: str, lang: str, next_path: str, error: str = "", success: str = ""
) -> str:
    """The whole tenant Settings screen: two invite links, two regenerations,
    and the gym name — each a distinct card block (spec-dashboard §Settings,
    issue #139)."""
    t = STRINGS[lang]
    member_url = _invite_url(bot_username, gym.invite_code)
    coach_url = _invite_url(bot_username, gym.coach_invite_code or "")
    # The error strings are the dashboard's own (confirm_mismatch carries
    # markup), never user input — rendered as-is like every STRINGS value.
    notice = success + (f'<p class="error">{error}</p>' if error else "")
    content = f"""{_chrome(gym.name, t, next_path, lang, active="settings")}
<div class="settings-wrap">
<h1>{t["settings_title"]}</h1>
{notice}
<section class="setcard" id="invite">
<h2>{t["invite_section"]}</h2>
<p>{t["invite_blurb"]} <b>{escape(gym.name)}</b>.</p>
<p><code>{escape(member_url)}</code> {_copy_button(member_url, t)}</p>
<div class="qr">{_qr_svg(member_url)}</div>
</section>
<section class="setcard consequential" id="regenerate-invite">
<h2>{t["regenerate"]}: {t["invite_section"].lower()}</h2>
{_regenerate_form("/settings/regenerate-invite", t["invite_warning"], t)}
</section>
<section class="setcard" id="coach-link">
<h2>{t["coach_section"]}</h2>
<p>{t["coach_blurb"]}</p>
<p><code>{escape(coach_url)}</code> {_copy_button(coach_url, t)}</p>
</section>
<section class="setcard consequential" id="regenerate-coach">
<h2>{t["regenerate"]}: {t["coach_section"].lower()}</h2>
{_regenerate_form("/settings/regenerate-coach", t["coach_warning"], t)}
</section>
<section class="setcard" id="gym-name">
<h2>{t["gym_name_section"]}</h2>
<p>{t["gym_name_help"]}</p>
<form method="post" action="/settings/gym-name">
<p><input type="text" name="name" value="{escape(gym.name, quote=True)}"
maxlength="{GYM_NAME_MAX_LENGTH}" required>
<button type="submit">{t["save"]}</button></p>
</form>
</section>
<p><a class="back" href="/">{t["back_to_dashboard"]}</a></p>
</div>"""
    return _document(
        f"{t['settings_title']} — {escape(gym.name)}",
        lang,
        content,
        scripts=SETTINGS_SCRIPT,
    )


def sign_session(member_id: int, gym_id: int, secret: str, now: datetime) -> str:
    """``member:gym:expiry:signature`` — the expiry is inside the signature."""
    expires = int((now + SESSION_TTL).timestamp())
    payload = f"{member_id}:{gym_id}:{expires}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_session(value: str, secret: str, now: datetime) -> tuple[int, int] | None:
    """The ``(member_id, gym_id)`` a cookie claims, if it verifies and is
    unexpired; anything malformed, tampered, or stale is ``None``."""
    try:
        member_id, gym_id, expires, signature = value.rsplit(":", 3)
        payload = f"{member_id}:{gym_id}:{expires}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires) <= int(now.timestamp()):
            return None
        return int(member_id), int(gym_id)
    except (ValueError, AttributeError):
        return None


# The directory where the Vite-built React bundle lives.
_FRONTEND_DIST = Path(__file__).parent.parent.parent / "frontend" / "dist"


def build_app(
    store: DashboardStore,
    linking: LinkingStore,
    *,
    session_secret: str,
    bot_username: str,
    secure_cookies: bool = True,
    clock: Clock = _utcnow,
    notifier: Notifier | None = None,
    spa_dist: Path | None = None,
) -> web.Application:
    def set_session(response: web.StreamResponse, member_id: int, gym_id: int) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            sign_session(member_id, gym_id, session_secret, clock()),
            max_age=int(SESSION_TTL.total_seconds()),
            path="/",
            httponly=True,
            secure=secure_cookies,
            samesite="Lax",
        )

    async def session_identity(request: web.Request) -> tuple[int, int] | None:
        cookie = request.cookies.get(SESSION_COOKIE)
        if cookie is None:
            return None
        return verify_session(cookie, session_secret, clock())

    async def require_coach(request: web.Request) -> tuple[Member, Gym] | None:
        """The session's ``(Member, Gym)`` if it still belongs to a Coach of
        that Gym. ``is_coach`` is re-checked per request, not per session — a
        demoted coach is out on their next click despite the 90-day cookie."""
        identity = await session_identity(request)
        if identity is None:
            return None
        return await store.coach_identity(*identity)

    def _view_of(request: web.Request) -> str:
        view = request.query.get("view", "table")
        return view if view in VIEWS else "table"

    async def set_language(request: web.Request) -> web.Response:
        """The chrome's EN/ES toggle: persist the pick in the long-lived
        cookie beside the session cookie and land back where the Coach was.
        An unknown language changes nothing."""
        lang = request.match_info["lang"]
        response = web.HTTPFound(_safe_next(request.query.get("next")))
        if lang in LANGS:
            response.set_cookie(
                LANG_COOKIE,
                lang,
                max_age=LANG_COOKIE_TTL_SECONDS,
                path="/",
                httponly=True,
                secure=secure_cookies,
                samesite="Lax",
            )
        raise response

    async def settings(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        response = web.Response(
            text=_settings_page(
                gym,
                bot_username,
                lang,
                _next_path_sans_done(request),
                success=_done_notice(request.query.get("done"), STRINGS[lang]),
            ),
            content_type="text/html",
        )
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    async def _regenerate(request: web.Request, which: str) -> web.Response:
        """Regenerate one invite code behind the typed confirm. A wrong or
        missing confirm changes nothing — the form's JS gate is convenience;
        this check is the load-bearing one."""
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        t = STRINGS[lang]
        form = await request.post()
        confirm = form.get("confirm", "")
        if not isinstance(confirm, str) or confirm.strip().lower() != t["confirm_word"]:
            # The error re-render points the language toggle at /settings —
            # this POST-only path would 405 a GET.
            response = web.Response(
                text=_settings_page(
                    gym,
                    bot_username,
                    lang,
                    "/settings",
                    error=t["confirm_mismatch"].format(word=t["confirm_word"]),
                ),
                content_type="text/html",
            )
            set_session(response, member.id, gym.id)  # sliding 90-day refresh
            return response
        if which == "invite":
            await linking.regenerate_invite_code(gym.id)
        else:
            await linking.regenerate_coach_invite_code(gym.id)
        response = web.HTTPFound("/settings?done=link_regenerated")
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        raise response

    async def regenerate_invite(request: web.Request) -> web.Response:
        return await _regenerate(request, "invite")

    async def regenerate_coach(request: web.Request) -> web.Response:
        return await _regenerate(request, "coach")

    async def gym_name(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        t = STRINGS[lang]
        form = await request.post()
        name = form.get("name", "")
        if not isinstance(name, str) or not name.strip():
            # Same as the confirm mismatch above: the toggle must not point
            # at this POST-only path.
            response = web.Response(
                text=_settings_page(
                    gym, bot_username, lang, "/settings", error=t["gym_name_empty"]
                ),
                content_type="text/html",
            )
            set_session(response, member.id, gym.id)  # sliding 90-day refresh
            return response
        await linking.rename_gym(gym.id, name)
        response = web.HTTPFound("/settings?done=saved")
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        raise response

    async def login_redeem(request: web.Request) -> web.Response:
        token = await store.redeem_login_token(request.match_info["token"])
        if token is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        # A safety-flag ping's token carries the Member's page as its
        # landing; the shared local-path guard decides (anything fishy
        # falls back to the roster).
        response = web.HTTPFound(_safe_next(token.next_path))
        set_session(response, token.member_id, token.gym_id)
        raise response

    # --- /api/session JSON endpoint (issue #155) ---

    async def api_session(request: web.Request) -> web.Response:
        """``GET /api/session`` — cookie-auth via ``require_coach``; returns
        the Coach's name and gym as JSON. Unauthenticated answers 401."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        response = web.json_response({"name": member.name, "gym": gym.name})
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    # --- /api/settings JSON endpoints (issue #153) ---


    async def api_login_peek(request: web.Request) -> web.Response:
        """``GET /api/login/{token}`` — validate a login token without spending
        it.  Returns ``{valid: true}`` or ``{valid: false}`` so the SPA
        interstitial can distinguish "click to sign in" from a dead link."""
        token = request.match_info["token"]
        row = await store.peek_login_token(token)
        return web.json_response({"valid": row is not None})


    async def api_settings(request: web.Request) -> web.Response:
        """``GET /api/settings`` — cookie-auth via ``require_coach``; returns
        gym name, both invite codes/URLs, QR SVG, and the bot username as JSON.
        Unauthenticated answers 401."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        # require_coach → coach_identity already SELECTs Member+Gym in the
        # same request, so the Gym row here is fresh — no stale read (the
        # re-fetch was redundant).  Serialize what the store computed.
        invite_url = _invite_url(bot_username, gym.invite_code)
        coach_url = _invite_url(bot_username, gym.coach_invite_code or "")
        response = web.json_response({
            "gym_name": gym.name,
            "invite_code": gym.invite_code,
            "invite_url": invite_url,
            "qr_svg": _qr_svg(invite_url),
            "coach_invite_code": gym.coach_invite_code,
            "coach_invite_url": coach_url,
            "bot_username": bot_username,
        })
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    async def api_settings_regenerate_invite(request: web.Request) -> web.Response:
        """``POST /api/settings/regenerate-invite`` — typed-confirm gated
        regeneration of the member invite code. Returns the new code and URL."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        lang = _lang_of(request)
        t = STRINGS[lang]
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        confirm = (body.get("confirm") or "").strip().lower()
        if confirm != t["confirm_word"]:
            return web.json_response(
                {"error": t["confirm_mismatch"].format(word=t["confirm_word"])},
                status=400,
            )
        new_code = await linking.regenerate_invite_code(gym.id)
        new_url = _invite_url(bot_username, new_code)
        response = web.json_response({
            "invite_code": new_code,
            "invite_url": new_url,
            "qr_svg": _qr_svg(new_url),
        })
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    async def api_settings_regenerate_coach(request: web.Request) -> web.Response:
        """``POST /api/settings/regenerate-coach`` — typed-confirm gated
        regeneration of the coach invite code. Returns the new code and URL."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        lang = _lang_of(request)
        t = STRINGS[lang]
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        confirm = (body.get("confirm") or "").strip().lower()
        if confirm != t["confirm_word"]:
            return web.json_response(
                {"error": t["confirm_mismatch"].format(word=t["confirm_word"])},
                status=400,
            )
        new_code = await linking.regenerate_coach_invite_code(gym.id)
        new_url = _invite_url(bot_username, new_code)
        response = web.json_response({
            "coach_invite_code": new_code,
            "coach_invite_url": new_url,
        })
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    async def api_settings_gym_name(request: web.Request) -> web.Response:
        """``POST /api/settings/gym-name`` — rename the gym. Returns the new name."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        lang = _lang_of(request)
        t = STRINGS[lang]
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        name = body.get("name", "")
        if not isinstance(name, str) or not name.strip():
            return web.json_response(
                {"error": t["gym_name_empty"]},
                status=400,
            )
        new_name = await linking.rename_gym(gym.id, name)
        response = web.json_response({"gym_name": new_name})
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    # --- /api/members/{id}/routine JSON endpoint (issue #151) ---

    async def api_member_routine_get(request: web.Request) -> web.Response:
        """``GET /api/members/{id}/routine`` — cookie-auth via ``require_coach``;
        returns the member's active Routine as JSON: weekday blocks, ownership
        info, the routine_id stamp for stale-save checks, and the exercise
        catalog. Unauthenticated answers 401; an unknown or unreachable member
        answers 404."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        _, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
        except ValueError:
            return web.json_response({"error": "not found"}, status=404)
        view = await store.member_page(gym.id, member_id)
        if view is None:
            return web.json_response({"error": "not found"}, status=404)
        catalog = await store.catalog_exercises()

        def serialise_day(day: RoutineDayView) -> dict:
            return {
                "weekday": day.weekday,
                "name": day.name,
                "exercises": [
                    {"exercise": name, "sets": sets, "reps": reps}
                    for name, sets, reps in day.exercises
                ],
            }

        body = {
            "member_id": view.member_id,
            "name": view.name,
            "routine": [serialise_day(day) for day in view.routine],
            "routine_id": view.routine_id,
            "coach_authored": view.coach_authored,
            "routine_author": view.routine_author,
            "routine_preset_name": view.routine_preset_name,
            "catalog": catalog,
        }
        response = web.json_response(body)
        set_session(response, coach[0].id, gym.id)
        return response

    async def api_member_routine_put(request: web.Request) -> web.Response:
        """``PUT /api/members/{id}/routine`` — cookie-auth via ``require_coach``;
        saves a coach-authored Routine through the supersession machinery.
        Accepts a JSON body with ``base_routine_id`` and ``workouts`` array.
        Answers 200 on success (with the fresh routine and notified flag),
        400 on validation errors, 409 on a stale save (with the fresh version),
        and 401/404 like the GET."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        coach_member, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
        except ValueError:
            return web.json_response({"error": "not found"}, status=404)
        target = await store.roster_member(gym.id, member_id)
        if target is None:
            return web.json_response({"error": "not found"}, status=404)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        lang = _lang_of(request)
        t = STRINGS[lang]

        base_routine_id = body.get("base_routine_id")
        raw_workouts = body.get("workouts")
        if not isinstance(raw_workouts, list):
            return web.json_response({"error": t["empty_routine_error"]}, status=400)

        # Parse workouts from JSON into WorkoutSpecs, reusing the existing
        # validation rules (weekday ranges, duplicates, sets/reps limits).
        try:
            workouts = _parse_workouts_from_json(raw_workouts, lang)
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=400)

        if not workouts:
            return web.json_response({"error": t["empty_routine_error"]}, status=400)

        try:
            await store.save_routine_from_web(
                gym.id, member_id, coach_member.id, base_routine_id, workouts
            )
        except StaleRoutineError:
            view = await store.member_page(gym.id, member_id)
            assert view is not None
            fresh = [
                {
                    "weekday": day.weekday,
                    "name": day.name,
                    "exercises": [
                        {"exercise": name, "sets": sets, "reps": reps}
                        for name, sets, reps in day.exercises
                    ],
                }
                for day in view.routine
            ]
            return web.json_response(
                {"error": t["stale_error"], "fresh_routine": fresh,
                 "fresh_routine_id": view.routine_id},
                status=409,
            )
        except UnknownExercisesError as error:
            message = t["unknown_exercises_error"].format(names=", ".join(error.names))
            return web.json_response({"error": message}, status=400)

        notified = False
        if notifier is not None:
            try:
                channel = await store.member_channel(member_id)
                if channel is not None:
                    await notifier.send(
                        channel[0],
                        channel[1],
                        routine_notice(coach_member.name, workouts),
                    )
                    notified = True
            except Exception:
                logger.exception("failed to notify member %s of the routine save", member_id)

        view = await store.member_page(gym.id, member_id)
        assert view is not None
        response = web.json_response(
            {
                "ok": True,
                "routine_id": view.routine_id,
                "routine": [
                    {
                        "weekday": day.weekday,
                        "name": day.name,
                        "exercises": [
                            {"exercise": name, "sets": sets, "reps": reps}
                            for name, sets, reps in day.exercises
                        ],
                    }
                    for day in view.routine
                ],
                "coach_authored": view.coach_authored,
                "routine_author": view.routine_author,
                "routine_preset_name": view.routine_preset_name,
                "notified": notified,
            }
        )
        set_session(response, coach_member.id, gym.id)
        return response

    # --- /api/roster JSON endpoint (issue #149) ---

    # --- /api/presets JSON endpoints (issue #152) ---

    async def api_presets_list(request: web.Request) -> web.Response:
        """``GET /api/presets`` — cookie-auth via ``require_coach``; returns
        the Gym's live Presets, eligible Members, and the default Preset id.
        Unauthenticated answers 401."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        presets = await store.presets(gym.id)
        members = await store.preset_members(gym.id)
        default_id = await store.default_preset_id(gym.id)
        master_ids = await store.preset_ids_with_masters(gym.id)

        body = {
            "presets": [
                {
                    "id": p.id,
                    "name": p.name,
                    "is_default": p.id == default_id,
                    "has_master": p.id in master_ids,
                }
                for p in presets
            ],
            "members": [{"id": m.id, "name": m.name} for m in members],
            "default_preset_id": default_id,
        }
        response = web.json_response(body)
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    async def api_presets_create(request: web.Request) -> web.Response:
        """``POST /api/presets`` — create a new Preset. Body: ``{"name": "..."}``.
        Flag-gated, cookie-auth via ``require_coach``."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        try:
            payload = await request.json()
            name = payload.get("name", "")
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        if not isinstance(name, str) or not name.strip():
            return web.json_response({"error": "preset_name_empty"}, status=400)
        if len(name.strip()) > 100:
            return web.json_response({"error": "preset_name_too_long"}, status=400)
        try:
            preset = await store.create_preset(gym.id, name)
        except DuplicatePresetNameError:
            return web.json_response({"error": "duplicate_preset_name"}, status=400)
        except ValueError:
            return web.json_response({"error": "preset_name_empty"}, status=400)
        response = web.json_response({"id": preset.id, "name": preset.name}, status=201)
        set_session(response, member.id, gym.id)
        return response

    async def api_presets_apply(request: web.Request) -> web.Response:
        """``POST /api/presets/{preset_id}/apply`` — apply a Preset to chosen
        Members. Body: ``{"member_ids": [...], "apply_all": bool}``.
        Flag-gated, cookie-auth via ``require_coach``."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return web.json_response({"error": "not_found"}, status=404)
        if await store.preset_for_gym(gym.id, preset_id) is None:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        apply_all = bool(payload.get("apply_all", False))
        raw_ids = payload.get("member_ids", [])
        if not isinstance(raw_ids, list):
            return web.json_response({"error": "preset_no_selection"}, status=400)
        try:
            member_ids = [int(v) for v in raw_ids]
        except (ValueError, TypeError):
            return web.json_response({"error": "not_found"}, status=404)
        if apply_all:
            member_ids = [m.id for m in await store.preset_members(gym.id)]
        elif not member_ids:
            return web.json_response({"error": "preset_no_selection"}, status=400)
        try:
            copies = await store.apply_preset(gym.id, preset_id, coach_member.id, member_ids)
        except NoPresetMasterError:
            return web.json_response({"error": "preset_no_master"}, status=400)
        except StaleRoutineError:
            return web.json_response({"error": "stale_error"}, status=409)
        except ValueError:
            return web.json_response({"error": "not_found"}, status=404)
        # Best-effort notify each member.
        for copy in copies:
            try:
                channel = await store.member_channel(copy.member_id)
                if channel is not None and notifier is not None:
                    await notifier.send(
                        channel[0],
                        channel[1],
                        routine_notice(coach_member.name, copy.workouts),
                    )
            except Exception:
                logger.exception("failed to notify member %s of the Preset apply", copy.member_id)
        response = web.json_response({"applied": len(copies)})
        set_session(response, coach_member.id, gym.id)
        return response

    async def api_presets_default(request: web.Request) -> web.Response:
        """``POST /api/presets/{preset_id}/default`` — set or clear the default
        Preset. Toggling the current default clears it.
        Flag-gated, cookie-auth via ``require_coach``."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return web.json_response({"error": "not_found"}, status=404)
        if await store.preset_for_gym(gym.id, preset_id) is None:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            current_default = await store.default_preset_id(gym.id)
            clearing = current_default == preset_id
            await store.set_default_preset(gym.id, None if clearing else preset_id)
        except ValueError:
            return web.json_response({"error": "not_found"}, status=404)
        new_default = await store.default_preset_id(gym.id)
        response = web.json_response({"default_preset_id": new_default})
        set_session(response, coach_member.id, gym.id)
        return response

    async def api_presets_retire(request: web.Request) -> web.Response:
        """``POST /api/presets/{preset_id}/retire`` — retire a Preset.
        Members keep their copies; the Preset can no longer be edited or applied.
        Flag-gated, cookie-auth via ``require_coach``."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return web.json_response({"error": "not_found"}, status=404)
        try:
            await store.retire_preset(gym.id, preset_id)
        except ValueError:
            return web.json_response({"error": "not_found"}, status=404)
        response = web.json_response({"retired": True})
        set_session(response, coach_member.id, gym.id)
        return response

    # --- /api/presets/{id}/routine JSON endpoints (#154) — the master
    # editor, mirroring the member Routine editor API (#151). ---

    def _serialise_master(preset: RoutinePreset, master: dict | None) -> dict:
        view = _preset_editor_view(preset, master)
        return {
            "preset_id": preset.id,
            "name": preset.name,
            "routine": [
                {
                    "weekday": day.weekday,
                    "name": day.name,
                    "exercises": [
                        {"exercise": name, "sets": sets, "reps": reps}
                        for name, sets, reps in day.exercises
                    ],
                }
                for day in view.routine
            ],
            # The master's routine_id — the stale-save stamp, exactly like
            # the member editor's base_routine_id contract.
            "routine_id": view.routine_id,
            "routine_author": view.routine_author,
        }

    async def api_preset_routine_get(request: web.Request) -> web.Response:
        """``GET /api/presets/{preset_id}/routine`` — cookie-auth via
        ``require_coach``; returns the Preset's master routine plus the
        exercise catalog. 404 for an unknown, retired, or foreign Preset."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return web.json_response({"error": "not found"}, status=404)
        preset = await store.preset_for_gym(gym.id, preset_id)
        if preset is None:
            return web.json_response({"error": "not found"}, status=404)
        master = await store.preset_master(preset.id)
        body = _serialise_master(preset, master)
        body["catalog"] = await store.catalog_exercises()
        response = web.json_response(body)
        set_session(response, member.id, gym.id)
        return response

    async def api_preset_routine_put(request: web.Request) -> web.Response:
        """``PUT /api/presets/{preset_id}/routine`` — save the Preset's
        master routine through the same supersession machinery as the
        server form did: 400 on validation errors, 409 with the fresh
        master on a stale save, and Member notifications for every linked
        copy the save forked or updated."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return web.json_response({"error": "not found"}, status=404)
        preset = await store.preset_for_gym(gym.id, preset_id)
        if preset is None:
            return web.json_response({"error": "not found"}, status=404)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        lang = _lang_of(request)
        t = STRINGS[lang]

        base_routine_id = body.get("base_routine_id")
        raw_workouts = body.get("workouts")
        if not isinstance(raw_workouts, list):
            return web.json_response({"error": t["empty_routine_error"]}, status=400)
        try:
            workouts = _parse_workouts_from_json(raw_workouts, lang)
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=400)
        if not workouts:
            return web.json_response({"error": t["empty_routine_error"]}, status=400)

        try:
            copies = await store.save_preset_master_from_web(
                gym.id, preset.id, coach_member.id, base_routine_id, workouts
            )
        except StaleRoutineError:
            fresh = await store.preset_master(preset.id)
            fresh_body = _serialise_master(preset, fresh)
            return web.json_response(
                {
                    "error": t["stale_error"],
                    "fresh_routine": fresh_body["routine"],
                    "fresh_routine_id": fresh_body["routine_id"],
                },
                status=409,
            )
        except UnknownExercisesError as error:
            message = t["unknown_exercises_error"].format(names=", ".join(error.names))
            return web.json_response({"error": message}, status=400)

        # Notify every Member whose linked copy the save forked/updated —
        # the same contract as the form path: a notification failure never
        # blocks the save.
        notified = 0
        for copy in copies:
            try:
                channel = await store.member_channel(copy.member_id)
                if channel is None:
                    logger.warning(
                        "failed to notify member %s of the Preset edit: no channel",
                        copy.member_id,
                    )
                elif notifier is not None:
                    await notifier.send(
                        channel[0],
                        channel[1],
                        routine_notice(coach_member.name, copy.workouts),
                    )
                    notified += 1
            except Exception:
                logger.exception(
                    "failed to notify member %s of the Preset edit", copy.member_id
                )

        fresh = await store.preset_master(preset.id)
        response_body = _serialise_master(preset, fresh)
        response_body["ok"] = True
        response_body["notified"] = notified
        response = web.json_response(response_body)
        set_session(response, coach_member.id, gym.id)
        return response

    async def api_roster(request: web.Request) -> web.Response:
        """``GET /api/roster`` — cookie-auth via ``require_coach``; returns
        the roster as JSON: active rows, lapsed tail, counts, and the sort
        key. Unauthenticated answers 401.

        Each row carries its attendance grid (4-week day cells) so the
        Cards view renders with one round-trip. No domain logic moves into
        the web layer — this endpoint serializes what the store already
        computes."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        rows, lapsed = await store.roster(gym.id)
        member_ids = [row.member_id for row in rows] + [row.member_id for row in lapsed]
        grids = await store.attendance(gym.id, member_ids) if member_ids else {}

        def serialise(row: RosterRow) -> dict:
            cells = grids.get(row.member_id, [])
            return {
                "member_id": row.member_id,
                "name": row.name,
                "gap_days": row.gap_days,
                "has_sessions": row.has_sessions,
                "is_new": row.is_new,
                "snoozed_until": row.snoozed_until.isoformat() if row.snoozed_until else None,
                "missed_days": row.missed_days,
                "severity": row.severity,
                "has_safety_flag": row.has_safety_flag,
                "attendance": [{"on": c.on.isoformat(), "state": c.state} for c in cells],
            }

        body = {
            "active": [serialise(row) for row in rows],
            "lapsed": [serialise(row) for row in lapsed],
            "counts": {"active": len(rows), "lapsed": len(lapsed)},
            "sortedBy": "gap_days",
        }
        response = web.json_response(body)
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    # --- /api/members/{id} JSON endpoint (issue #150) ---

    def _serialise_member_page(view: MemberPage) -> dict:
        """Serialize a ``MemberPage`` to JSON — no domain logic, just
        shape translation. Dates become ISO strings."""
        return {
            "member_id": view.member_id,
            "name": view.name,
            "member_since": view.member_since.isoformat(),
            "weight_unit": view.weight_unit,
            "session_count": view.session_count,
            "gap_days": view.gap_days,
            "has_sessions": view.has_sessions,
            "last_session_on": view.last_session_on.isoformat() if view.last_session_on else None,
            "lapsed": view.lapsed,
            "snoozed_until": view.snoozed_until.isoformat() if view.snoozed_until else None,
            "routine": [
                {
                    "weekday": day.weekday,
                    "name": day.name,
                    "exercises": [
                        {"name": name, "sets": sets, "reps": reps}
                        for name, sets, reps in day.exercises
                    ],
                }
                for day in view.routine
            ],
            "routine_id": view.routine_id,
            "routine_preset_name": view.routine_preset_name,
            "coach_authored": view.coach_authored,
            "routine_author": view.routine_author,
            "sessions": [
                {
                    "on": session.on.isoformat(),
                    "sets": [
                        {
                            "exercise": name,
                            "weight": weight,
                            "reps": reps,
                            "note": note,
                            # The Member's own words never translate; the
                            # detected source language lets the React page
                            # tag foreign quotes ("EN · textual") exactly
                            # like the server renderer did (#154 parity).
                            "note_lang": detect_language(note) if note else None,
                        }
                        for name, weight, reps, note in session.sets
                    ],
                }
                for session in view.sessions
            ],
            "page": view.page,
            "pages": view.pages,
            "weights": [
                {
                    "exercise": w.exercise,
                    "weight": w.weight,
                    "reps": w.reps,
                    "on": w.on.isoformat(),
                }
                for w in view.weights
            ],
            "notes": [
                {
                    "kind": n.kind,
                    "text": n.text,
                    "lang": detect_language(n.text),
                    "on": n.on.isoformat(),
                    "retired_on": n.retired_on.isoformat() if n.retired_on else None,
                }
                for n in view.notes
            ],
            "retired_notes": [
                {
                    "kind": n.kind,
                    "text": n.text,
                    "lang": detect_language(n.text),
                    "on": n.on.isoformat(),
                    "retired_on": n.retired_on.isoformat() if n.retired_on else None,
                }
                for n in view.retired_notes
            ],
            "safety_flags": [
                {
                    "note_id": f.note_id,
                    "text": f.text,
                    "on": f.on.isoformat(),
                    "status": f.status,
                    "acknowledged_on": f.acknowledged_on.isoformat() if f.acknowledged_on else None,
                    "acknowledged_by": f.acknowledged_by,
                }
                for f in view.safety_flags
            ],
        }

    async def api_member(request: web.Request) -> web.Response:
        """``GET /api/members/{id}`` — cookie-auth via ``require_coach``;
        returns the full Member page as JSON. Unauthenticated answers 401;
        unknown/ghost/coach answers 404. No domain logic moves into the
        web layer — this endpoint serializes what the store already computes."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        _, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
            if not 0 < member_id < 2**63:
                return web.json_response({"error": "not found"}, status=404)
        except (ValueError, OverflowError):
            return web.json_response({"error": "not found"}, status=404)
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        view = await store.member_page(gym.id, member_id, page=page)
        if view is None:
            return web.json_response({"error": "not found"}, status=404)
        body = _serialise_member_page(view)
        response = web.json_response(body)
        set_session(response, coach[0].id, gym.id)  # sliding 90-day refresh
        return response

    # --- /api/members/{id}/flags/{note_id}/tick-off JSON endpoint (issue #150) ---

    async def api_tick_off_flag(request: web.Request) -> web.Response:
        """``POST /api/members/{id}/flags/{note_id}/tick-off`` —
        acknowledge a safety flag via the JSON API. Same store call as the
        server-HTML path; returns JSON confirming the acknowledgement."""
        coach = await require_coach(request)
        if coach is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        member, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
            note_id = int(request.match_info["note_id"])
            if not (0 < member_id < 2**63 and 0 < note_id < 2**63):
                return web.json_response({"error": "not found"}, status=404)
        except (ValueError, OverflowError):
            return web.json_response({"error": "not found"}, status=404)
        note = await store.acknowledge_flag(gym.id, member_id, note_id, member.id)
        if note is None:
            return web.json_response({"error": "not found"}, status=404)
        response = web.json_response({"note_id": note.id, "acknowledged": True})
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    # --- /api/seed dev/demo data endpoint (issue #149) ---

    # --- SPA shell and static assets (issue #155, ADR 0004) ---

    resolved_dist = spa_dist or _FRONTEND_DIST

    def _inject_i18n(html: str, t: dict, lang: str) -> str:
        """Inject ``window.__I18N__`` bootstrap into the SPA shell HTML.

        Includes STRINGS plus the ``_months``, ``_weekday_initials`` and
        ``_decimal_mark`` keys the frontend's ``i18n.ts`` reads through
        ``getMonths`` / ``getWeekdayInitials`` / ``getDecimalMark``."""
        i18n_payload: dict = dict(t)
        # The active language rides along so the React chrome can mark the
        # EN/ES toggle and build /lang/{lang} links (#154 — the toggle
        # crossed from the server chrome to the SPA in the cutover).
        i18n_payload["_lang"] = lang
        i18n_payload["_months"] = list(MONTHS[lang])
        i18n_payload["_weekday_initials"] = list(WEEKDAY_INITIALS[lang])
        i18n_payload["_weekdays"] = list(WEEKDAYS[lang])
        i18n_payload["_decimal_mark"] = DECIMAL_MARK[lang]
        i18n_json = json.dumps(i18n_payload, ensure_ascii=False)
        # Escape <, U+2028, and U+2029 so no string value can close the
        # <script> tag early or inject a line separator (ADR 0004 §i18n 7a).
        safe_json = (
            i18n_json.replace("<", "\\u003c")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029")
        )
        i18n_script = f"<script>window.__I18N__ = {safe_json};</script>"
        # Insert after </head> (if present) or at the start of <body>.
        if "</head>" in html:
            html = html.replace("</head>", f"{i18n_script}\n</head>")
        elif "<body" in html:
            body_start = html.index("<body")
            body_close = html.index(">", body_start) + 1
            html = html[:body_close] + i18n_script + html[body_close:]
        else:
            html = i18n_script + html
        return html

    def _read_spa_index() -> str | None:
        """Read the Vite-built index.html; ``None`` if not built."""
        index_path = resolved_dist / "index.html"
        if not index_path.exists():
            return None
        return index_path.read_text(encoding="utf-8")

    async def spa_login_shell(request: web.Request) -> web.Response:
        """Serve the SPA shell **without** auth for the login/interstitial
        screen (issue #153).  The React app at ``/login/:token`` detects the
        route and renders the interstitial — no session required.

        The i18n bootstrap uses the language cookie if set, else the
        no-signal default (Spanish) — the same rule as the door pages."""
        lang = resolve_lang(
            request.cookies.get(LANG_COOKIE),
            request.headers.get("Accept-Language"),
        )
        t = STRINGS[lang]

        html = _read_spa_index()
        if html is None:
            return web.Response(
                text="SPA bundle not built — run `npm run build` in frontend/",
                status=503,
                content_type="text/plain",
            )

        html = _inject_i18n(html, t, lang)
        return web.Response(text=html, content_type="text/html")

    async def spa_shell(request: web.Request) -> web.Response:
        """Serve the Vite-built React bundle shell with ``window.__I18N__``
        bootstrap injected (ADR 0004 §i18n 7a)."""
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        t = STRINGS[lang]

        html = _read_spa_index()
        if html is None:
            return web.Response(
                text="SPA bundle not built — run `npm run build` in frontend/",
                status=503,
                content_type="text/plain",
            )

        html = _inject_i18n(html, t, lang)
        response = web.Response(text=html, content_type="text/html")
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    app = web.Application()
    # The roster — like every dashboard screen since the #154 cutover — is
    # the React SPA shell; the data lives behind /api/*.
    app.router.add_get("/", spa_shell)
    app.router.add_get("/api/session", api_session)
    app.router.add_get("/members/{member_id}", spa_shell)
    app.router.add_get("/presets", spa_shell)
    app.router.add_get("/presets/{preset_id}/routine", spa_shell)
    app.router.add_get("/settings", settings)
    app.router.add_post("/settings/regenerate-invite", regenerate_invite)
    app.router.add_post("/settings/regenerate-coach", regenerate_coach)
    app.router.add_post("/settings/gym-name", gym_name)
    app.router.add_get("/lang/{lang}", set_language)
    # GET /login/{token} serves the SPA login shell (public — the React
    # interstitial validates the token via /api/login/{token} without
    # spending it); the POST redemption stays server-side so the signed
    # session cookie is set exactly as before (#154 preserves the
    # login-token flow the bot depends on).
    app.router.add_get("/login/{token}", spa_login_shell)
    app.router.add_post("/login/{token}", login_redeem)
    app.router.add_static("/static/", STATIC_DIR)
    app.router.add_get("/members/{member_id}/routine", spa_shell)
    # The JSON API is the dashboard's only data surface after the React
    # cutover (#154) — registered unconditionally.
    app.router.add_get("/api/login/{token}", api_login_peek)
    app.router.add_get("/api/roster", api_roster)
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/settings/regenerate-invite", api_settings_regenerate_invite)
    app.router.add_post("/api/settings/regenerate-coach", api_settings_regenerate_coach)
    app.router.add_post("/api/settings/gym-name", api_settings_gym_name)
    app.router.add_get("/api/members/{member_id}/routine", api_member_routine_get)
    app.router.add_put("/api/members/{member_id}/routine", api_member_routine_put)
    app.router.add_get("/api/members/{member_id}", api_member)
    app.router.add_post(
        "/api/members/{member_id}/flags/{note_id}/tick-off", api_tick_off_flag
    )
    app.router.add_get("/api/presets", api_presets_list)
    app.router.add_post("/api/presets", api_presets_create)
    app.router.add_get("/api/presets/{preset_id}/routine", api_preset_routine_get)
    app.router.add_put("/api/presets/{preset_id}/routine", api_preset_routine_put)
    app.router.add_post("/api/presets/{preset_id}/apply", api_presets_apply)
    app.router.add_post("/api/presets/{preset_id}/default", api_presets_default)
    app.router.add_post("/api/presets/{preset_id}/retire", api_presets_retire)
    # The Vite bundle's asset files.  Guarded so a missing/partial bundle
    # degrades to a 503 shell instead of killing the bot at boot
    # (add_static raises on a missing directory).
    if (resolved_dist / "assets").is_dir():
        app.router.add_static("/assets/", resolved_dist / "assets")
    else:
        logger.warning(
            "SPA bundle missing at %s — dashboard screens will answer 503; "
            "run `npm run build` in frontend/",
            resolved_dist / "assets",
        )
    # SPA fallback, registered last so every real route above wins: any
    # unmatched GET is a React Router deep link on a cold load and gets the
    # authenticated shell (issue #149).
    app.router.add_get("/{tail:.*}", spa_shell)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Bind the app on the current event loop; the caller keeps the runner to
    ``cleanup()`` on shutdown."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("dashboard HTTP server listening on %s:%d", host, port)
    return runner

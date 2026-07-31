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
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from html import escape
from urllib.parse import quote, unquote

import qrcode
import qrcode.image.svg
from aiohttp import web
from multidict import MultiDictProxy

from agentg.checkin_sweep import Notifier
from agentg.dashboard_i18n import (
    LANG_COOKIE,
    LANG_COOKIE_TTL_SECONDS,
    LANGS,
    NOTE_KIND_LABELS,
    STRINGS,
    WEEKDAY_INITIALS,
    WEEKDAYS,
    away_text,
    detect_language,
    fmt_date,
    fmt_number,
    resolve_lang,
    verbatim,
)
from agentg.dashboard_store import (
    GRID_WEEKS,
    DashboardStore,
    DayCell,
    MemberPage,
    NoteView,
    RoutineDayView,
    RosterRow,
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
INTERSTITIAL_TITLE = "Abriendo tu dashboard…"
INTERSTITIAL_BUTTON = "Entrar al dashboard"

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
# color reserved for training state — mint kept, coral missed/red, amber
# slipping, purple extra. All values live here as custom properties; the
# per-surface style blocks below only compose them.

BASE_STYLE = """
:root {
  --bg: #000; --surface: #131313; --surface-2: #1b1c1e;
  --line: #2a2b2d; --line-2: #3a3a3c;
  --ink: #fff; --ink-2: #9a9a9a; --ink-3: #757579;
  --mint: #6ef3a5; --mint-tint: #0f2a1c;
  --coral: #f58060; --coral-tint: #2b1712;
  --amber: #f2b84b; --amber-tint: #2a2110;
  --purple: #8b7cf6; --purple-tint: #201e33;
  --gut: 16px;
  --mono: ui-monospace, "SF Mono", "Roboto Mono", Menlo, Consolas, monospace;
  --t-fast: 120ms ease-out;
  color-scheme: dark;
}
* { box-sizing: border-box; }
html { background: var(--bg); }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.45 ui-sans-serif, system-ui, -apple-system, "Helvetica Neue", sans-serif;
  -webkit-font-smoothing: antialiased; }
h1, h2, h3 { margin: 0; font-weight: 700; letter-spacing: -0.02em; }
a { color: inherit; text-decoration: none; }
p { margin: 0.6em 0; }
:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.eyebrow { font-family: var(--mono); text-transform: uppercase; font-size: 11px; letter-spacing: .14em; color: var(--ink-2); display: block; }
button { font: inherit; font-size: 14px; font-weight: 600; cursor: pointer;
  background: var(--surface-2); color: var(--ink); border: 1px solid var(--line-2);
  min-height: 44px; padding: 8px 16px;
  transition: background var(--t-fast), border-color var(--t-fast); }
button:hover { border-color: var(--ink-2); }
button:disabled { opacity: .4; cursor: default; }
.btn-primary { background: var(--ink); color: #000; border-color: var(--ink); }
.btn-primary:hover { background: #e6e6e6; border-color: #e6e6e6; }
.btn-primary.big { display: block; width: 100%; min-height: 55px; font-size: 16px; }
input[type="text"], input[type="search"], select, textarea {
  background: var(--surface-2); color: var(--ink); border: 1px solid var(--line-2);
  font: inherit; min-height: 44px; padding: 8px 12px;
  transition: border-color var(--t-fast); }
input[type="text"]:hover, input[type="search"]:hover, select:hover, textarea:hover { border-color: var(--ink-2); }
input[type="checkbox"] { accent-color: var(--mint); width: 16px; height: 16px; }
code { font-family: var(--mono); font-size: 13px; background: var(--surface-2); padding: 6px 8px; overflow-wrap: anywhere; }
summary { cursor: pointer; color: var(--ink-3); font-size: 13px; padding: 10px 0; }
summary:hover { color: var(--ink-2); }
.tag { display: inline-block; font-family: var(--mono); text-transform: uppercase; font-weight: 400;
  font-size: 10px; letter-spacing: .12em; padding: 3px 7px; white-space: nowrap;
  border: 1px solid var(--line-2); color: var(--ink-2); }
.tag-flag { background: var(--ink); border-color: var(--ink); color: #000; font-weight: 700; }
.tag-new { color: var(--ink); border-color: var(--ink-2); }
.chip { color: var(--ink-2); }
.error { background: var(--coral-tint); border: 1px solid var(--coral); color: var(--coral);
  padding: 12px 14px; font-size: 14px; margin: 14px 0; }
.muted { color: var(--ink-2); font-size: 13px; }
.emptystate { padding: 70px 24px; text-align: center; }
.emptystate h2 { font-size: 22px; margin-bottom: 8px; }
.emptystate p { color: var(--ink-2); font-size: 15px; margin: 0; }
.lang-toggle { white-space: nowrap; }
.lang-toggle a { font-family: var(--mono); font-size: 11px; letter-spacing: .12em;
  color: var(--ink-3); padding: 12px 6px; }
.lang-toggle a:hover { color: var(--ink-2); }
.lang-toggle a[aria-current="true"] { color: var(--ink); }
"""

# Login door and bounce pages: one centered column, one action.
DOOR_STYLE = """
.door { max-width: 26rem; margin: 18vh auto 0; padding: 0 var(--gut); text-align: center; }
.door h1 { font-size: 22px; }
.door p { color: var(--ink-2); font-size: 15px; }
.door form { margin-top: 24px; }
"""

# Every form disables its submit buttons once a submit is on its way —
# a double-clicked Apply must not message every Member twice. The timeout
# lets the click's own submit complete before the buttons grey out.
SUBMIT_GUARD_SCRIPT = """
document.addEventListener("submit", function (e) {
  var buttons = e.target.querySelectorAll("button[type=submit]");
  setTimeout(function () { buttons.forEach(function (b) { b.disabled = true; }); }, 0);
});
"""


def _document(
    title: str, lang: str, body: str, *, styles: str = "", scripts: str = ""
) -> str:
    """The one page skeleton every surface renders through."""
    script_html = f"<script>{SUBMIT_GUARD_SCRIPT}{scripts}</script>"
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{BASE_STYLE}{styles}</style>
</head>
<body>
{body}
{script_html}
</body>
</html>"""


def _page(title: str, body: str, extra: str = "", lang: str = "es") -> str:
    content = f"""<div class="door">
<h1>{title}</h1>
<p>{body}</p>
{extra}
</div>"""
    return _document(title, lang, content, styles=DOOR_STYLE)


def _bounce_page() -> str:
    return _page(BOUNCE_TITLE, BOUNCE_BODY)


def _interstitial_page(token: str) -> str:
    """A self-submitting POST form: one tap (or zero, with JS) for the human,
    a harmless GET for any link-preview fetcher."""
    form = f"""<form method="post" action="/login/{token}" id="go">
<button type="submit" class="btn-primary big">{INTERSTITIAL_BUTTON}</button>
</form>
<script>document.getElementById("go").submit();</script>"""
    return _page(INTERSTITIAL_TITLE, "", form)


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

ROSTER_STYLE = """
header.top { position: sticky; top: 0; z-index: 20; display: flex; align-items: center;
  gap: 6px 14px; flex-wrap: wrap; min-height: 46px; padding: 6px var(--gut);
  background: var(--bg); border-bottom: 1px solid var(--line); }
header.top h1 { font-size: 17px; font-weight: 600; letter-spacing: -0.01em; }
header.top .count { color: var(--ink-2); font-size: 13px; }
header.top .spacer { flex: 1; }
.quick a { color: var(--ink-2); font-size: 14px; padding: 12px 6px; }
.quick a:hover { color: var(--ink); }
.quick a[aria-current="true"] { color: var(--ink); font-weight: 600; }
.seg { display: inline-flex; border: 1px solid var(--line-2); border-radius: 999px; padding: 3px; gap: 2px; }
.seg a { color: var(--ink-2); padding: 9px 16px; border-radius: 999px; font-size: 13px; font-weight: 500;
  transition: color var(--t-fast); }
.seg a:hover { color: var(--ink); }
.seg a[aria-current="true"] { background: var(--ink); color: #000; }
#search { min-height: 40px; width: 200px; font-size: 14px; }
@media (max-width: 700px) { #search { flex: 1 1 100%; order: 9; width: auto; } }
.countbar { display: flex; align-items: baseline; gap: 8px; padding: 10px var(--gut);
  border-bottom: 1px solid var(--line); font-size: 14px; color: var(--ink-2); }
.countbar b { color: var(--ink); font-weight: 600; }
.roster-body { padding: 0 0 40px; }
ul#roster { list-style: none; padding: 0; margin: 0; }
#lapsed ul { list-style: none; padding: 0; margin: 0; }
#lapsed summary { padding: 12px var(--gut); }
.row { border-bottom: 1px solid var(--line); }
.row > a { display: grid; grid-template-columns: 44px minmax(0, 1fr); gap: 0 12px; align-items: center;
  padding: 10px var(--gut); transition: background var(--t-fast); }
.row > a:hover { background: #0b0b0b; }
.row > a[aria-current="true"] { background: #0e0e0e; box-shadow: inset 2px 0 0 var(--ink); }
.tile { width: 44px; height: 44px; background: var(--surface-2); display: grid; place-items: center;
  font-size: 15px; font-weight: 600; letter-spacing: .02em; }
.row .t { font-size: 16px; font-weight: 600; letter-spacing: -0.01em;
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.row .t .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
.row .meta { display: flex; gap: 4px 12px; color: var(--ink-2); font-size: 13px; margin-top: 2px; flex-wrap: wrap; }
.away { white-space: nowrap; }
.sev { white-space: nowrap; }
.sev-amber { color: var(--amber); font-weight: 600; }
.sev-red { color: var(--coral); font-weight: 600; }
/* Cards: severity bands plus the 4-week Mon-Sun day grid. */
.band { padding: 0 var(--gut); }
.band > h2 { font-family: var(--mono); text-transform: uppercase; font-size: 11px; letter-spacing: .14em;
  font-weight: 400; color: var(--ink-2); margin: 24px 0 10px; }
.band-hot > h2 { color: var(--coral); }
.band-warm > h2 { color: var(--amber); }
.band .count { color: var(--ink-3); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
.mcard { background: var(--surface); padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.mcard .top-row { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.mcard a.name { font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mcard a.name:hover { text-decoration: underline; }
.mcard .away { color: var(--ink-2); font-size: 13px; }
.daygrid { display: grid; grid-template-columns: repeat(7, 18px); gap: 4px; }
.daygrid i { height: 18px; background: var(--surface-2); }
.daygrid i.hit { background: var(--mint); }
.daygrid i.miss { background: transparent; box-shadow: inset 0 0 0 2px var(--coral); }
.daygrid i.future { background: transparent; border: 1px dashed var(--line-2); }
.daygrid .wd { font-family: var(--mono); font-size: 11px; line-height: 1; text-align: center;
  text-transform: uppercase; letter-spacing: .04em; color: var(--ink-2); }
.sparklab { font-size: 12px; color: var(--ink-3); }
.legend { display: flex; gap: 16px; padding: 18px var(--gut) 0; font-size: 12px; color: var(--ink-2); flex-wrap: wrap; }
.legend span { display: inline-flex; align-items: center; gap: 7px; }
.legend i { width: 12px; height: 12px; background: var(--surface-2); }
.legend i.l-hit { background: var(--mint); }
.legend i.l-miss { background: transparent; box-shadow: inset 0 0 0 2px var(--coral); }
/* Split: the roster never leaves. */
.split { display: grid; grid-template-columns: 340px minmax(0, 1fr); align-items: start; }
.split .rail { border-right: 1px solid var(--line); position: sticky; top: 47px;
  height: calc(100vh - 47px); overflow-y: auto; }
.split .pane { padding: 0 var(--gut) 40px; max-width: 60rem; }
.split .pane-empty { display: grid; place-items: center; min-height: 60vh; }
@media (max-width: 899px) {
  .split { grid-template-columns: 1fr; }
  .split .rail { position: static; height: auto; max-height: 45vh; border-right: 0;
    border-bottom: 1px solid var(--line); }
}
"""

MEMBER_STYLE = """
.member-wrap { max-width: 60rem; margin: 0 auto; padding: 0 var(--gut) 48px; }
a.back { display: inline-block; color: var(--ink-2); font-size: 14px; padding: 12px 0; }
a.back:hover { color: var(--ink); }
header.mhead { padding: 8px 0 4px; }
header.mhead h1 { font-size: 33px; line-height: 1.05; }
header.mhead .chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
header.mhead .facts { color: var(--ink-2); font-size: 14px; margin-top: 10px; }
.dot { display: inline-block; width: 3px; height: 3px; border-radius: 50%; background: var(--ink-3);
  vertical-align: middle; margin: 0 7px 2px; }
.columns { display: flex; gap: 32px; flex-wrap: wrap; align-items: flex-start; }
.col { flex: 1; min-width: 18rem; }
.card { margin: 26px 0 0; }
.card h2 { font-size: 20px; margin: 0 0 12px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
a.edit { font-size: 14px; font-weight: 600; letter-spacing: 0; border: 1px solid var(--line-2);
  padding: 8px 14px; margin-left: auto; transition: border-color var(--t-fast); }
a.edit:hover { border-color: var(--ink-2); }
.day { background: var(--surface); padding: 12px 14px; margin-bottom: 10px; }
.day > b { font-family: var(--mono); text-transform: uppercase; font-size: 11px; letter-spacing: .14em;
  font-weight: 400; color: var(--ink-2); display: block; margin-bottom: 2px; }
.card .day ul { list-style: none; margin: 8px 0 0; padding: 0; }
.card .day li { padding: 5px 0; border-top: 1px solid var(--line); font-size: 14px; }
.card .day li:first-child { border-top: 0; }
.sess { padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
.sess .set { color: var(--ink-2); font-size: 13px; padding: 2px 0; }
.sess .said { color: var(--coral); font-size: 13px; }
.card > ul { list-style: none; margin: 0; padding: 0; }
.card > ul > li { padding: 8px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
.note { padding: 10px 0; border-bottom: 1px solid var(--line); font-size: 14px; }
.pages { display: flex; gap: 16px; margin-top: 10px; align-items: baseline; }
.pages a { font-size: 14px; padding: 8px 0; }
.pages a:hover { text-decoration: underline; }
.pages .muted { margin: 0 auto; }
/* The loudest thing in the system is a white block, so the safety banner is one. */
.safety-banner { background: var(--ink); color: #000; padding: 16px; margin-top: 20px; }
.safety-banner h2 { color: #000; font-size: 20px; margin-bottom: 4px; }
.safety-banner .flag { padding: 8px 0; display: flex; gap: 8px 12px; align-items: center; flex-wrap: wrap; }
.safety-banner .flag b { font-size: 16px; letter-spacing: -0.01em; }
.safety-banner .muted { color: #555; }
.safety-banner form { margin: 0 0 0 auto; }
.safety-banner button { background: #000; color: #fff; border: 0; }
.safety-banner button:hover { background: #2a2a2a; }
/* The Member's own words, left alone, tagged when not in the Coach's language. */
.verbatim { border-left: 2px solid var(--line-2); padding-left: 8px; }
.langtag { display: inline-block; white-space: nowrap; font-family: var(--mono); font-size: 10px;
  letter-spacing: .12em; text-transform: uppercase; color: var(--ink-3); margin-left: 6px; }
"""

SEARCH_SCRIPT = """
// Live, name-only, accent-insensitive filter. It only hides rows — the Gap
// sort never moves — and a lapsed match auto-expands the tail (a manually
// opened tail is never slammed shut). Identical in the three views
// (spec-dashboard §The roster).
const box = document.getElementById("search");
const lapsed = document.getElementById("lapsed");
const nomatch = document.getElementById("no-matches");
const norm = (s) => s.normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
box.addEventListener("input", () => {
  const q = norm(box.value.trim());
  let lapsedHit = false;
  let shown = 0;
  document.querySelectorAll("[data-name]").forEach((row) => {
    const hit = !q || norm(row.dataset.name).includes(q);
    row.hidden = !hit;
    if (hit) shown += 1;
    if (hit && q && lapsed && lapsed.contains(row)) lapsedHit = true;
  });
  if (lapsed && lapsedHit) lapsed.open = true;
  if (nomatch) nomatch.hidden = shown > 0;
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
    return f'<nav class="seg">{links}</nav>'


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
        f'<span class="count">{t["members_count"].format(n=count)}</span>' if count is not None else ""
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
<nav class="quick">{quick("/presets", "presets")}{quick("/settings", "settings")}</nav>
{_lang_toggle(next_path, lang)}
</header>"""


def _member_href(member_id: int, view: str) -> str:
    """Member links carry the view they were opened from, so the way back
    (or the Split pane) matches where the Coach was."""
    return f"/members/{member_id}?view={view}"


def _initials(name: str) -> str:
    """The row tile's two-letter monogram."""
    parts = name.split()
    return "".join(part[0] for part in parts[:2]).upper() or "?"


def _severity_text(row: RosterRow, lang: str) -> str:
    """The severity sentence beside the away text — the count in words, so
    urgency is never conveyed by color alone."""
    if not row.severity:
        return ""
    t = STRINGS[lang]
    label = (
        t["missed_one"] if row.missed_days == 1 else t["missed_n"].format(n=row.missed_days)
    )
    return f'<span class="sev sev-{row.severity}">{label}</span>'


def _roster_row(
    row: RosterRow, view: str, lang: str, current_member_id: int | None = None
) -> str:
    t = STRINGS[lang]
    tags = ""
    if row.is_new:
        tags += f' <span class="tag tag-new">{t["new_tag"]}</span>'
    if row.snoozed_until is not None:
        until = fmt_date(row.snoozed_until, lang)
        tags += f' <span class="tag">{t["snoozed_tag"].format(date=until)}</span>'
    if row.has_safety_flag:
        # A marker on the row, never a re-sort (spec-dashboard §Safety flags).
        tags += f' <span class="tag tag-flag">{t["flag_tag"]}</span>'
    current = ' aria-current="true"' if row.member_id == current_member_id else ""
    # One physical line per row: the whole row is the link.
    return (
        f'<li class="row" data-name="{escape(row.name)}">'
        f'<a href="{_member_href(row.member_id, view)}"{current}>'
        f'<span class="tile" aria-hidden="true">{escape(_initials(row.name))}</span>'
        f'<span><span class="t"><span class="nm">{escape(row.name)}</span>{tags}</span>'
        f'<span class="meta"><span class="away">{away_text(row.has_sessions, row.gap_days, lang)}</span>'
        f"{_severity_text(row, lang)}</span></span></a></li>"
    )


def _lapsed_section(
    lapsed: list[RosterRow], view: str, lang: str, current_member_id: int | None = None
) -> str:
    """The collapsed tail, identical in the three views: out of the Gap sort
    and the counters, most-recently-active first (spec-dashboard §The
    roster)."""
    if not lapsed:
        return ""
    t = STRINGS[lang]
    items = "".join(_roster_row(row, view, lang, current_member_id) for row in lapsed)
    return f"""<details id="lapsed">
<summary>{t["lapsed_tail"].format(n=len(lapsed))}</summary>
<ul>{items}</ul>
</details>"""


def _countbar(t: dict) -> str:
    """The line that names the ordering, so the sort is never mysterious."""
    return f'<div class="countbar">{t["sorted_by_gap"]}</div>'


def _no_matches(t: dict) -> str:
    """The search's zero-result line; the filter script unhides it."""
    return f'<p id="no-matches" class="emptystate" hidden>{t["no_matches"]}</p>'


def _empty_roster(t: dict) -> str:
    """A brand-new gym: no Members at all — point at the invite link."""
    return f"""<div class="emptystate">
<h2>{t["empty_roster_title"]}</h2>
<p>{t["empty_roster_body"]}</p>
</div>"""


def _roster_document(
    gym_name: str, view: str, lang: str, next_path: str, count: int | None, body: str, split: bool
) -> str:
    t = STRINGS[lang]
    styles = ROSTER_STYLE + (MEMBER_STYLE if split else "")
    content = f"""{_chrome(gym_name, t, next_path, lang, view=view, count=count)}
{body}"""
    return _document(
        f"{escape(gym_name)} — Dashboard", lang, content, styles=styles, scripts=SEARCH_SCRIPT
    )


def _table_page(
    gym_name: str, rows: list[RosterRow], lapsed: list[RosterRow], lang: str, next_path: str
) -> str:
    t = STRINGS[lang]
    if not rows and not lapsed:
        body = f'<div class="roster-body">{_empty_roster(t)}</div>'
    else:
        items = "".join(_roster_row(row, "table", lang) for row in rows)
        body = f"""{_countbar(t)}
<div class="roster-body">
<ul id="roster">{items}</ul>
{_no_matches(t)}
{_lapsed_section(lapsed, "table", lang)}
</div>"""
    return _roster_document(gym_name, "table", lang, next_path, len(rows), body, split=False)


def _member_card(row: RosterRow, cells: list[DayCell], lang: str) -> str:
    """One Cards card: the shared markers and Gap text, plus the 4-week
    Mon–Sun day grid — one square per day, dashed for future days, judged
    per date against the Routine active on it. Hit squares fill mint;
    missed squares are hollow coral rings, so the two never read by hue
    alone."""
    t = STRINGS[lang]
    initials = "".join(f'<span class="wd">{w}</span>' for w in WEEKDAY_INITIALS[lang])
    squares = "".join(
        f'<i class="{cell.state}" title="{fmt_date(cell.on, lang)}"></i>' for cell in cells
    )
    tags = ""
    if row.is_new:
        tags += f' <span class="tag tag-new">{t["new_tag"]}</span>'
    if row.snoozed_until is not None:
        until = fmt_date(row.snoozed_until, lang)
        tags += f' <span class="tag">{t["snoozed_tag"].format(date=until)}</span>'
    if row.has_safety_flag:
        # A marker on the card, never a re-sort (spec-dashboard §Safety flags).
        tags += f' <span class="tag tag-flag">{t["flag_tag"]}</span>'
    severity = _severity_text(row, lang)
    return f"""<div class="mcard" data-name="{escape(row.name)}">
<div class="top-row"><a class="name" href="{_member_href(row.member_id, "cards")}">{escape(row.name)}</a>
<span class="away">{away_text(row.has_sessions, row.gap_days, lang)}</span></div>
{f'<div class="meta">{severity}</div>' if severity else ""}
<div class="daygrid" aria-hidden="true">{initials}{squares}</div>
<div class="sparklab">{t["grid_label"].format(n=GRID_WEEKS)}</div>
{f"<div>{tags}</div>" if tags else ""}
</div>"""


def _legend(t: dict) -> str:
    """What the day-grid squares mean, said once per Cards page."""
    return f"""<div class="legend">
<span><i class="l-hit"></i>{t["legend_hit"]}</span>
<span><i class="l-miss"></i>{t["legend_miss"]}</span>
</div>"""


def _cards_page(
    gym_name: str,
    rows: list[RosterRow],
    lapsed: list[RosterRow],
    grids: dict[int, list[DayCell]],
    lang: str,
    next_path: str,
) -> str:
    """The urgency bands — a reading of the same schedule-aware severity the
    Table colours with, never a new field: red needs you now, amber is
    slipping, the rest are on track. A Member with no Routine yet sits in a
    quiet fourth group instead of masquerading as on-track (the spec's
    grey-new rule). The three severity bands always render — an empty hot
    band saying 0 is good news, not a missing feature. The Gap sort holds
    inside each band."""
    t = STRINGS[lang]
    if not rows and not lapsed:
        body = f'<div class="roster-body">{_empty_roster(t)}</div>'
        return _roster_document(gym_name, "cards", lang, next_path, 0, body, split=False)
    bands: list[tuple[str, str, list[RosterRow]]] = [
        ("hot", t["band_hot"], []),
        ("warm", t["band_warm"], []),
        ("cool", t["band_cool"], []),
        ("new", t["band_new"], []),
    ]
    for row in rows:
        if row.severity == "red":
            band = "hot"
        elif row.severity == "amber":
            band = "warm"
        elif row.is_new:
            band = "new"
        else:
            band = "cool"
        next(b for b in bands if b[0] == band)[2].append(row)
    sections = ""
    for band_id, title, members in bands:
        if band_id == "new" and not members:
            continue  # a transient group, not a permanent fixture
        cards = "".join(_member_card(row, grids.get(row.member_id, []), lang) for row in members)
        sections += f"""<section class="band band-{band_id}" id="band-{band_id}">
<h2>{title} <span class="count">{len(members)}</span></h2>
{f'<div class="grid">{cards}</div>' if members else ""}
</section>"""
    body = f"""<div class="roster-body">
{sections}
{_legend(t)}
{_no_matches(t)}
{_lapsed_section(lapsed, "cards", lang)}
</div>"""
    return _roster_document(gym_name, "cards", lang, next_path, len(rows), body, split=False)


def _split_page(
    gym_name: str,
    rows: list[RosterRow],
    lapsed: list[RosterRow],
    pane: str,
    lang: str,
    next_path: str,
    current_member_id: int | None = None,
) -> str:
    """Split: a permanent left rail with the roster, the right pane holding
    a Member page or the pick-a-member placeholder. The switcher (and the
    search) stay visible with a Member open — nothing was left. The rail
    scrolls on its own, the open Member's row stays marked, and below 900px
    the two columns stack (no back link: still nothing was left)."""
    t = STRINGS[lang]
    if not rows and not lapsed:
        rail = _empty_roster(t)
    else:
        items = "".join(_roster_row(row, "split", lang, current_member_id) for row in rows)
        rail = f"""{_countbar(t)}
<ul id="roster">{items}</ul>
{_no_matches(t)}
{_lapsed_section(lapsed, "split", lang, current_member_id)}"""
    body = f"""<div class="split">
<div class="rail">
{rail}
</div>
<div class="pane">{pane}</div>
</div>"""
    return _roster_document(gym_name, "split", lang, next_path, len(rows), body, split=True)


def _split_placeholder(lang: str) -> str:
    return (
        f'<div class="pane-empty"><div class="emptystate">'
        f'<h2>{STRINGS[lang]["pick_a_member"]}</h2></div></div>'
    )


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


def _fmt_load(weight: float | None, unit: str, lang: str) -> str:
    """One wording for a set's load — Sessions and Últimos pesos never drift.
    The decimal mark follows the page language."""
    if weight is None:
        return STRINGS[lang]["bodyweight"]
    return f"{fmt_number(weight, lang)} {unit}"


def _scheme(sets: int | None, reps: str | None) -> str:
    if sets is not None and reps is not None:
        return f" — {sets} × {escape(reps)}"
    if reps is not None:
        return f" — {escape(reps)}"
    if sets is not None:
        return f" — {sets}"
    return ""


def _verbatim_quote(text: str, lang: str) -> str:
    """The Member's own words: never translated, carrying a small
    source-language tag when they differ from the language the Coach reads
    (spec-dashboard §Language)."""
    tag = verbatim(detect_language(text), lang)
    tag_html = f'<span class="langtag">{escape(tag)}</span>' if tag else ""
    return f'<span class="verbatim">{escape(text)}{tag_html}</span>'


def _set_lines(sets: list[tuple[str, float | None, int, str | None]], unit: str, lang: str) -> str:
    """Collapse a Session's sets into one line per (Exercise, weight):
    ``bench press 60 kg × 8,8,8`` — warm-ups at another weight stay separate.
    Set comments render below their line, verbatim, as the Member's words."""
    lines = []
    grouped: dict[tuple[str, float | None], list[int]] = {}
    comments: dict[tuple[str, float | None], list[str]] = {}
    for name, weight, reps, note in sets:
        grouped.setdefault((name, weight), []).append(reps)
        # log_sets stamps the same comment on every rep Set of the line —
        # quote it once per collapsed line, not once per rep.
        if note and note not in comments.setdefault((name, weight), []):
            comments[(name, weight)].append(note)
    for (name, weight), reps_list in grouped.items():
        lines.append(
            f'<div class="set">{escape(name)} {_fmt_load(weight, unit, lang)} × {",".join(str(r) for r in reps_list)}</div>'
        )
        for note in comments.get((name, weight), []):
            lines.append(f'<div class="said">“{_verbatim_quote(note, lang)}”</div>')
    return "".join(lines)


def _ownership_chip(
    coach_authored: bool, author: str | None, lang: str, preset_name: str | None = None
) -> str:
    """The ownership chip (issue #86): named Coach-authored while the actor
    stamp survives, plain when it has blanked, Agent-managed until the
    first coach save. Always visible — the fork is silent but never a
    surprise."""
    t = STRINGS[lang]
    if preset_name is not None:
        label = t["preset_chip"].format(name=escape(preset_name))
    elif coach_authored:
        label = t["chip_coach_named"].format(name=escape(author)) if author else t["chip_coach"]
    else:
        label = t["chip_agent"]
    return f'<span class="tag chip">{label}</span>'


def _routine_card(view: MemberPage, lang: str) -> str:
    t = STRINGS[lang]
    if not view.routine:
        body = f'<p class="muted">{t["no_routine"]}</p>'
    else:
        days = []
        for day in view.routine:
            exercises = "".join(
                f"<li>{escape(name)}{_scheme(sets, reps)}</li>"
                for name, sets, reps in day.exercises
            )
            days.append(
                f'<div class="day"><b>{WEEKDAYS[lang][day.weekday]}</b> '
                f"{escape(day.name)}<ul>{exercises}</ul></div>"
            )
        body = "".join(days)
    header = (
        f'<h2>{t["routine"]} {_ownership_chip(view.coach_authored, view.routine_author, lang, view.routine_preset_name)} '
        f'<a class="edit" href="/members/{view.member_id}/routine">{t["edit"]}</a></h2>'
    )
    return f'<section class="card">{header}{body}</section>'


def _sessions_card(view: MemberPage, lang: str, roster_view: str) -> str:
    t = STRINGS[lang]
    items = []
    for session in view.sessions:
        count = len(session.sets)
        if count == 0:
            headline = t["visit_no_sets"]
        elif count == 1:
            headline = t["one_set"]
        else:
            headline = t["n_sets"].format(n=count)
        items.append(
            f'<div class="sess"><b>{fmt_date(session.on, lang)}</b> '
            f'<span class="muted">{headline}</span>'
            f"{_set_lines(session.sets, view.weight_unit, lang)}</div>"
        )
    if not items:
        items.append(f'<p class="muted">{t["no_sessions_yet"]}</p>')
    nav = ""
    if view.pages > 1:
        # Pagination keeps the view the Member page was opened in, so Split
        # never drops the Coach out of the pane.
        newer = (
            f'<a href="/members/{view.member_id}?page={view.page - 1}&view={roster_view}">{t["newer_page"]}</a>'
            if view.page > 1
            else ""
        )
        older = (
            f'<a href="/members/{view.member_id}?page={view.page + 1}&view={roster_view}">{t["older_page"]}</a>'
            if view.page < view.pages
            else ""
        )
        nav = (
            f'<nav class="pages">{newer}'
            f'<span class="muted">{t["page_x_of_y"].format(page=view.page, pages=view.pages)}</span>{older}</nav>'
        )
    return f'<section class="card"><h2>{t["sessions"]}</h2>{"".join(items)}{nav}</section>'


def _weights_card(view: MemberPage, lang: str) -> str:
    t = STRINGS[lang]
    if not view.weights:
        rows = f'<p class="muted">{t["nothing_logged"]}</p>'
    else:
        rows = "".join(
            f'<li><b>{escape(w.exercise)}</b> '
            f'{_fmt_load(w.weight, view.weight_unit, lang)}'
            f' × {",".join(str(r) for r in w.reps)}'
            f' <span class="muted">· {fmt_date(w.on, lang)}</span></li>'
            for w in view.weights
        )
        rows = f"<ul>{rows}</ul>"
    return f'<section class="card"><h2>{t["last_weights"]}</h2>{rows}</section>'


def _note_row(note: NoteView, lang: str) -> str:
    t = STRINGS[lang]
    label = NOTE_KIND_LABELS[lang].get(note.kind, note.kind)
    retired = (
        f' <span class="muted">· {t["retired_on"].format(date=fmt_date(note.retired_on, lang))}</span>'
        if note.retired_on is not None
        else ""
    )
    return (
        f'<div class="note"><span class="tag">{escape(label)}</span> '
        f"{_verbatim_quote(note.text, lang)}"
        f' <span class="muted">· {fmt_date(note.on, lang)}</span>{retired}</div>'
    )


def _notes_card(view: MemberPage, lang: str) -> str:
    t = STRINGS[lang]
    body = "".join(_note_row(note, lang) for note in view.notes) or (
        f'<p class="muted">{t["no_notes"]}</p>'
    )
    if view.retired_notes:
        retired = "".join(_note_row(note, lang) for note in view.retired_notes)
        body += f"""<details class="tail">
<summary>{t["retired_tail"].format(n=len(view.retired_notes))}</summary>
{retired}
</details>"""
    return f'<section class="card"><h2>{t["notes"]}</h2>{body}</section>'


def _safety_banner(view: MemberPage, lang: str, roster_view: str) -> str:
    """The safety-flag banner above the Member page's columns.

    Open flags carry the Tick off action; acknowledged ones name the Coach
    and the date (acknowledging is not retiring — the Note stays in the
    Notes card too); an expired unacknowledged flag stays labelled
    "expired, never seen" (spec-dashboard §Safety flags). The form keeps
    the view the page was opened from, so ticking off never bounces a
    Split or Cards Coach back to Table."""
    if not view.safety_flags:
        return ""
    t = STRINGS[lang]
    items = []
    for flag in view.safety_flags:
        text = f"<b>{escape(flag.text)}</b> · {fmt_date(flag.on, lang)}"
        if flag.status == "open":
            action = (
                f'<form method="post" '
                f'action="/members/{view.member_id}/flags/{flag.note_id}/tick-off'
                f'?view={roster_view}">'
                f'<button type="submit">{t["tick_off"]}</button></form>'
            )
        elif flag.status == "acknowledged":
            who = flag.acknowledged_by or "—"
            when = fmt_date(flag.acknowledged_on, lang) if flag.acknowledged_on else ""
            action = (
                f'<span class="muted">'
                f'{t["flag_seen_by"].format(who=escape(who), date=when)}</span>'
            )
        else:
            action = f'<span class="muted">{t["flag_expired_unseen"]}</span>'
        items.append(f'<div class="flag">{text} {action}</div>')
    return (
        f'<section class="card safety-banner"><h2>{t["safety_section"]}</h2>'
        + "".join(items)
        + "</section>"
    )


def _member_content(view: MemberPage, lang: str, roster_view: str) -> str:
    """The one Member page body, shared by the standalone page and Split's
    right pane."""
    t = STRINGS[lang]
    tags = ""
    if view.lapsed:
        tags += f' <span class="tag">{t["lapsed_tag"]}</span>'
    if view.snoozed_until is not None:
        until = fmt_date(view.snoozed_until, lang)
        tags += f' <span class="tag">{t["snoozed_tag"].format(date=until)}</span>'
    count = t["one_session"] if view.session_count == 1 else t["n_sessions"].format(n=view.session_count)
    facts = (
        f"{t['member_since'].format(date=fmt_date(view.member_since, lang))} · {count}"
        f" · {away_text(view.has_sessions, view.gap_days, lang)}"
    )
    if view.last_session_on is not None:
        facts += f" · {t['last_session'].format(date=fmt_date(view.last_session_on, lang))}"
    return f"""<header class="mhead">
<h1>{escape(view.name)}{tags}</h1>
<div class="facts">{facts}</div>
</header>
{_safety_banner(view, lang, roster_view)}
<div class="columns">
<div class="col">
{_routine_card(view, lang)}
{_sessions_card(view, lang, roster_view)}
</div>
<div class="col">
{_weights_card(view, lang)}
{_notes_card(view, lang)}
</div>
</div>"""


def _member_page(gym_name: str, view: MemberPage, roster_view: str, lang: str, next_path: str) -> str:
    """The standalone Member page: the switcher (and the search) hide per
    the spec, but the rest of the chrome stays, and a back link returns to
    the view the Member was opened from."""
    t = STRINGS[lang]
    back = f'<a class="back" href="/?view={roster_view}">{t["back_to_roster"]}</a>'
    content = f"""{_chrome(gym_name, t, next_path, lang)}
<div class="member-wrap">
{back}
{_member_content(view, lang, roster_view)}
</div>"""
    return _document(
        f"{escape(view.name)} — {escape(gym_name)}",
        lang,
        content,
        styles=ROSTER_STYLE + MEMBER_STYLE,
    )


# --- The Routine editor (issue #100, spec-dashboard §Routines & Presets) ---
#
# One form: a block per pinned day (weekday select, Workout name, exercises
# one per line as "name, sets, reps" — structure only, never weights), a
# hidden stamp of the Routine it loaded for the stale-save check, and the
# ownership chip always in the header. A day comes off the plan by clearing
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


def _days_from_view(view: MemberPage) -> list[EditorDay]:
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


def _days_from_form(form: MultiDictProxy) -> list[EditorDay]:
    """The raw submitted form back into editor blocks, verbatim — the
    rejection page for a form that wouldn't parse (bad sets, half-filled
    blocks) must show exactly what the Coach typed, bad line included."""
    days: list[EditorDay] = []

    def texts(key: str) -> list[str]:
        return [v if isinstance(v, str) else "" for v in form.getall(key, [])]

    for weekday_raw, name, body in zip(
        texts("weekday"), texts("workout_name"), texts("exercises")
    ):
        if not weekday_raw.strip() and not name.strip() and not body.strip():
            continue  # the spare blank block the page always appends
        try:
            parsed = int(weekday_raw)
            weekday = parsed if 0 <= parsed <= 6 else None
        except ValueError:
            weekday = None
        days.append((weekday, name, body))
    return days


def _editor_day(day: EditorDay, lang: str) -> str:
    weekday, name, exercises_text = day
    t = STRINGS[lang]
    options = [f'<option value="">{t["pick_day"]}</option>']
    for i, weekday_name in enumerate(WEEKDAYS[lang]):
        selected = " selected" if weekday == i else ""
        options.append(f'<option value="{i}"{selected}>{weekday_name}</option>')
    return f"""<fieldset class="day-edit">
<select name="weekday">{"".join(options)}</select>
<input type="text" name="workout_name" value="{escape(name, quote=True)}"
placeholder="{escape(t["workout_name_placeholder"], quote=True)}" maxlength="100">
<textarea name="exercises" rows="4"
placeholder="squat, 4, 8-10">{escape(exercises_text)}</textarea>
</fieldset>"""


def _parse_workouts(form: MultiDictProxy, lang: str) -> list[WorkoutSpec]:
    """The editor form into WorkoutSpecs. A fully blank block (the spare one
    the page always appends) is dropped; anything malformed or half-filled —
    content without a weekday, a weekday without exercises, a duplicate
    weekday — raises ``ValueError`` with a Coach-readable message."""

    def texts(key: str) -> list[str]:
        return [v.strip() if isinstance(v, str) else "" for v in form.getall(key, [])]

    t = STRINGS[lang]
    specs = []
    seen_weekdays: set[int] = set()
    for weekday_raw, name, body in zip(
        texts("weekday"), texts("workout_name"), texts("exercises")
    ):
        if not weekday_raw:
            if name or body.strip():
                # Half-filled blocks are a mistake, never a silent drop — a
                # day comes off the plan by clearing its exercises too.
                raise ValueError(t["undated_block_error"])
            continue
        try:
            weekday = int(weekday_raw)
        except ValueError:
            raise ValueError(t["bad_weekday_error"]) from None
        if not 0 <= weekday <= 6:
            raise ValueError(t["bad_weekday_error"])
        if weekday in seen_weekdays:
            # Downstream pickers disagree on duplicate days (first vs last
            # wins) — the editor refuses to write the ambiguity.
            raise ValueError(t["duplicate_weekday_error"])
        seen_weekdays.add(weekday)
        exercises = []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split(",")]
            sets = None
            if len(parts) > 1 and parts[1]:
                try:
                    sets = int(parts[1])
                except ValueError:
                    raise ValueError(t["bad_sets_error"]) from None
                if not SETS_MIN <= sets <= SETS_MAX:
                    raise ValueError(t["sets_range_error"])
            reps = parts[2] if len(parts) > 2 and parts[2] else None
            if reps is not None and len(reps) > REPS_MAX_LENGTH:
                raise ValueError(t["reps_too_long"])
            exercises.append(ExerciseSpec(parts[0], sets, reps))
        if not exercises:
            # A picked weekday with no exercises is a mistake, not a rest
            # day — a day comes off the plan via the empty-day selector,
            # never by saving an empty Workout.
            raise ValueError(t["empty_workout_error"])
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


EDITOR_STYLE = """
.editor-wrap { max-width: 44rem; margin: 0 auto; padding: 0 var(--gut) 48px; }
.editor-wrap header h1 { font-size: 22px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.day-edit { background: var(--surface); border: 0; margin: 12px 0; padding: 14px 16px; }
.day-edit label { display: block; font-size: 13px; color: var(--ink-2); margin: 10px 0 0; }
.day-edit select, .day-edit input, .day-edit textarea { display: block; width: 100%; margin: 4px 0 0; }
.day-edit select { width: auto; }
.day-edit textarea { font-family: var(--mono); min-height: 96px; }
.consequence { color: var(--ink-2); font-size: 14px; margin: 6px 0 0; }
.editor-help { color: var(--ink-2); font-size: 13px; margin: 12px 0 4px; }
.catalog { display: flex; flex-wrap: wrap; gap: 6px; padding: 6px 0 10px; }
.catchip { border: 1px solid var(--line-2); color: var(--ink-2); font-size: 12px; padding: 4px 8px; white-space: nowrap; }
.fresh-version { background: var(--surface); padding: 4px 16px 12px; margin: 12px 0; }
"""


def _routine_editor_page(
    gym_name: str,
    view: RoutineEditorView,
    days: list[EditorDay],
    catalog: list[str],
    lang: str,
    next_path: str,
    error: str = "",
    base: str | None = None,
    action: str | None = None,
    back_href: str | None = None,
    title_key: str = "editor_title",
    consequence_key: str | None = None,
) -> str:
    t = STRINGS[lang]
    chip = _ownership_chip(
        view.coach_authored, view.routine_author, lang, view.routine_preset_name
    )
    if consequence_key is None:
        consequence_key = (
            "chip_consequence"
            if not view.coach_authored or view.routine_preset_name is not None
            else None
        )
    consequence = (
        f'<p class="consequence">{t[consequence_key]}</p>' if consequence_key else ""
    )
    notice = f'<p class="error">{escape(error)}</p>' if error else ""
    blocks = "".join(_editor_day(day, lang) for day in days) + _editor_day((None, "", ""), lang)
    # The stale-check stamp: the view's active Routine by default, but a
    # rejected save keeps the SUBMITTED stamp — rebuilding it from the fresh
    # view would let a retry slip past the stale check (review round 4).
    if base is None:
        base = "" if view.routine_id is None else str(view.routine_id)
    title = t[title_key].format(name=escape(view.name))
    action = action or f"/members/{view.member_id}/routine"
    back_href = back_href or f"/members/{view.member_id}"
    content = f"""{_chrome(gym_name, t, next_path, lang)}
<div class="editor-wrap">
<a class="back" href="{back_href}">← {escape(view.name)}</a>
<header>
<h1>{title} {chip}</h1>
{consequence}
</header>
{notice}
<form method="post" action="{action}">
<input type="hidden" name="base_routine_id" value="{base}">
<p class="editor-help">{t["editor_help"]}</p>
<div id="days">
{blocks}
</div>
<details>
<summary>{t["catalog_label"]}</summary>
<div class="catalog">{"".join(f'<span class="catchip">{escape(name)}</span>' for name in catalog)}</div>
</details>
<p><button type="submit" class="btn-primary big">{t["save_routine"]}</button></p>
</form>
</div>"""
    return _document(
        f"{title} — {escape(gym_name)}",
        lang,
        content,
        styles=ROSTER_STYLE + MEMBER_STYLE + EDITOR_STYLE,
    )


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


def _presets_page(
    gym_name: str,
    presets: list[RoutinePreset],
    members: list[Member],
    default_preset_id: int | None,
    lang: str,
    next_path: str,
    error: str = "",
) -> str:
    """The Coach-only Presets index and copy-on-apply forms (issue #102)."""
    t = STRINGS[lang]
    notice = f'<p class="error">{escape(error)}</p>' if error else ""
    create = f"""<section class="card">
<h2>{t["create_preset"]}</h2>
<form method="post" action="/presets">
<label>{t["preset_name"]}
<input type="text" name="name" maxlength="100" required>
</label> <button type="submit">{t["create_preset"]}</button>
</form></section>"""
    if not presets:
        cards = f'<p class="muted">{t["no_presets"]}</p>'
    else:
        cards = []
        for preset in presets:
            member_choices = "".join(
                f'<label><input type="checkbox" name="member_ids" value="{member.id}">'
                f" {escape(member.name)}</label> "
                for member in members
            )
            if members:
                apply_form = f"""<form method="post" action="/presets/{preset.id}/apply">
<fieldset><legend>{t["apply_preset"]}</legend>
<label><input type="checkbox" name="apply_all" value="1"> {t["apply_all"]}</label>
<div>{member_choices}</div>
<button type="submit">{t["apply"]}</button>
</fieldset></form>"""
            else:
                apply_form = f'<p class="muted">{t["no_members_to_apply"]}</p>'
            if default_preset_id == preset.id:
                default_form = (
                    f'<p><span class="tag">{t["preset_default"]}</span> '
                    f'<form method="post" action="/presets/{preset.id}/default">'
                    f'<button type="submit">{t["clear_default_preset"]}</button></form></p>'
                )
            else:
                default_form = (
                    f'<form method="post" action="/presets/{preset.id}/default">'
                    f'<button type="submit">{t["set_default_preset"]}</button></form>'
                )
            retire_form = (
                f'<form method="post" action="/presets/{preset.id}/retire">'
                f'<button type="submit">{t["retire_preset"]}</button></form>'
            )
            cards.append(
                f'<section class="card"><h2>{escape(preset.name)} '
                f'<a href="/presets/{preset.id}/routine">{t["edit_preset"]}</a></h2>'
                f"{default_form}{apply_form}{retire_form}</section>"
            )
        cards = "".join(cards)
    body = f'<main class="editor-wrap">{notice}{create}{cards}</main>'
    content = f"""{_chrome(gym_name, t, next_path, lang, active="presets")}
{body}"""
    return _document(
        f'{t["presets_title"]} — {escape(gym_name)}',
        lang,
        content,
        styles=ROSTER_STYLE + MEMBER_STYLE + EDITOR_STYLE,
    )


# --- The tenant Settings screen (spec-dashboard §Settings) ---

SETTINGS_STYLE = """
.settings-wrap { max-width: 36rem; margin: 0 auto; padding: 0 var(--gut) 48px; }
.settings-wrap > h1 { font-size: 22px; padding: 18px 0 2px; }
.setcard { background: var(--surface); padding: 18px; margin-top: 16px; }
.setcard h2 { font-size: 17px; }
.setcard p { color: var(--ink-2); font-size: 14px; }
.setcard code { display: inline-block; color: var(--ink); max-width: 100%; }
/* A QR must sit on white to scan — the one white block that is not a flag. */
.qr { background: #fff; padding: 12px; width: 200px; margin: 12px 0; }
.qr svg { display: block; width: 100%; height: auto; }
"""


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


def _settings_page(gym: Gym, bot_username: str, lang: str, next_path: str, error: str = "") -> str:
    """The whole tenant Settings screen: two invite links and the gym name,
    nothing else (spec-dashboard §Settings — no new settings). One card per
    concern."""
    t = STRINGS[lang]
    member_url = _invite_url(bot_username, gym.invite_code)
    coach_url = _invite_url(bot_username, gym.coach_invite_code or "")
    # The error strings are the dashboard's own (confirm_mismatch carries
    # markup), never user input — rendered as-is like every STRINGS value.
    notice = f'<p class="error">{error}</p>' if error else ""
    content = f"""{_chrome(gym.name, t, next_path, lang, active="settings")}
<div class="settings-wrap">
<h1>{t["settings_title"]}</h1>
{notice}
<section class="setcard" id="invite">
<h2>{t["invite_section"]}</h2>
<p>{t["invite_blurb"]} <b>{escape(gym.name)}</b>.</p>
<p><code>{escape(member_url)}</code> {_copy_button(member_url, t)}</p>
<div class="qr">{_qr_svg(member_url)}</div>
{_regenerate_form("/settings/regenerate-invite", t["invite_warning"], t)}
</section>
<section class="setcard" id="coach-link">
<h2>{t["coach_section"]}</h2>
<p>{t["coach_blurb"]}</p>
<p><code>{escape(coach_url)}</code> {_copy_button(coach_url, t)}</p>
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
        styles=ROSTER_STYLE + MEMBER_STYLE + SETTINGS_STYLE,
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


def build_app(
    store: DashboardStore,
    linking: LinkingStore,
    *,
    session_secret: str,
    bot_username: str,
    secure_cookies: bool = True,
    clock: Clock = _utcnow,
    notifier: Notifier | None = None,
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

    async def home(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        view = _view_of(request)
        next_path = request.rel_url.path_qs
        rows, lapsed = await store.roster(gym.id)
        if view == "cards":
            grids = await store.attendance(gym.id, [row.member_id for row in rows])
            text = _cards_page(gym.name, rows, lapsed, grids, lang, next_path)
        elif view == "split":
            text = _split_page(gym.name, rows, lapsed, _split_placeholder(lang), lang, next_path)
        else:
            text = _table_page(gym.name, rows, lapsed, lang, next_path)
        response = web.Response(text=text, content_type="text/html")
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

    async def member_page(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
        except ValueError:
            return _not_found()
        try:
            page = int(request.query.get("page", "1"))
        except ValueError:
            page = 1
        view = await store.member_page(gym.id, member_id, page=page)
        if view is None:  # departed, forgotten, or another Gym's: the shared 404
            return _not_found()
        lang = _lang_of(request)
        roster_view = _view_of(request)
        next_path = request.rel_url.path_qs
        if roster_view == "split":
            # Split keeps the rail and the switcher with a Member open.
            rows, lapsed = await store.roster(gym.id)
            pane = _member_content(view, lang, roster_view)
            text = _split_page(
                gym.name, rows, lapsed, pane, lang, next_path, current_member_id=member_id
            )
        else:
            text = _member_page(gym.name, view, roster_view, lang, next_path)
        response = web.Response(text=text, content_type="text/html")
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        return response

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

    async def routine_editor(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        _, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
        except ValueError:
            return _not_found()
        view = await store.member_page(gym.id, member_id)
        if view is None:  # same rule as the Member page: the shared 404
            return _not_found()
        lang = _lang_of(request)
        catalog = await store.catalog_exercises()
        response = web.Response(
            text=_routine_editor_page(
                gym.name,
                view,
                _days_from_view(view),
                catalog,
                lang,
                request.rel_url.path_qs,
            ),
            content_type="text/html",
        )
        set_session(response, coach[0].id, gym.id)  # sliding 90-day refresh
        return response

    async def routine_save(request: web.Request) -> web.Response:
        """The editor's save. Three ways to say no, all with the form back:
        a malformed form, exercises outside the catalog (the Coach's edits
        stay on the page, base stamp included), and a stale save (the fresh
        version replaces them). On yes: supersede, notify the Member, back
        to the page."""
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        coach_member, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
        except ValueError:
            return _not_found()
        target = await store.roster_member(gym.id, member_id)
        if target is None:  # another Gym's, a coach's, a ghost: the shared 404
            return _not_found()
        lang = _lang_of(request)
        t = STRINGS[lang]

        form = await request.post()
        base_raw = form.get("base_routine_id", "")
        base_routine_id = None
        if isinstance(base_raw, str) and base_raw.strip():
            try:
                base_routine_id = int(base_raw)
            except ValueError:
                return _not_found()

        async def reject(
            error: str,
            status: int,
            days: list[EditorDay] | None = None,
            base: str | None = None,
        ) -> web.Response:
            view = await store.member_page(gym.id, member_id)
            assert view is not None  # roster_member above already scoped it
            catalog = await store.catalog_exercises()
            response = web.Response(
                status=status,
                text=_routine_editor_page(
                    gym.name,
                    view,
                    days if days is not None else _days_from_view(view),
                    catalog,
                    lang,
                    # The language toggle must not point at this POST-only
                    # path (it would 405 a GET) — aim it at the editor GET.
                    f"/members/{member_id}/routine",
                    error=error,
                    base=base,
                ),
                content_type="text/html",
            )
            set_session(response, coach_member.id, gym.id)
            return response

        submitted_base = base_raw if isinstance(base_raw, str) else ""
        try:
            workouts = _parse_workouts(form, lang)
        except ValueError as error:
            # The Coach's edits stay on the page, bad line included — and the
            # submitted base too, or a retry would slip past the stale check.
            return await reject(
                str(error), 400, days=_days_from_form(form), base=submitted_base
            )
        if not workouts:
            return await reject(
                t["empty_routine_error"], 400, days=_days_from_form(form), base=submitted_base
            )
        try:
            await store.save_routine_from_web(
                gym.id, member_id, coach_member.id, base_routine_id, workouts
            )
        except StaleRoutineError:
            # The fresh version on the page; the Coach re-applies on top.
            return await reject(t["stale_error"], 409)
        except UnknownExercisesError as error:
            # Spanish and coach-facing like every other rejection — never
            # the raw English agent-tool message.
            message = t["unknown_exercises_error"].format(names=", ".join(error.names))
            return await reject(
                message, 400, days=_days_from_form(form), base=submitted_base
            )

        if notifier is not None:
            # Best-effort, whole block: the save already committed, so any
            # failure on the notify path — the channel lookup included —
            # logs and still redirects. A lost message never eats a save.
            try:
                channel = await store.member_channel(member_id)
                if channel is not None:
                    await notifier.send(
                        channel[0],
                        channel[1],
                        routine_notice(coach_member.name, workouts),
                    )
            except Exception:
                logger.exception("failed to notify member %s of the routine save", member_id)
        response = web.HTTPFound(f"/members/{member_id}")
        set_session(response, coach_member.id, gym.id)  # sliding 90-day refresh
        raise response

    async def presets_page(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        response = web.Response(
            text=_presets_page(
                gym.name,
                await store.presets(gym.id),
                await store.preset_members(gym.id),
                await store.default_preset_id(gym.id),
                lang,
                request.rel_url.path_qs,
            ),
            content_type="text/html",
        )
        set_session(response, member.id, gym.id)
        return response

    async def preset_create(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        t = STRINGS[lang]
        form = await request.post()
        name = form.get("name", "")
        if not isinstance(name, str) or not name.strip():
            error = t["preset_name_empty"]
        elif len(name.strip()) > 100:
            error = t["preset_name_too_long"]
        else:
            try:
                await store.create_preset(gym.id, name)
            except DuplicatePresetNameError:
                error = t["duplicate_preset_name"]
            except ValueError:
                error = t["preset_name_empty"]
            else:
                response = web.HTTPFound("/presets")
                set_session(response, member.id, gym.id)
                raise response
        response = web.Response(
            status=400,
            text=_presets_page(
                gym.name,
                await store.presets(gym.id),
                await store.preset_members(gym.id),
                await store.default_preset_id(gym.id),
                lang,
                "/presets",
                error=error,
            ),
            content_type="text/html",
        )
        set_session(response, member.id, gym.id)
        return response

    async def preset_editor(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        _, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return _not_found()
        preset = await store.preset_for_gym(gym.id, preset_id)
        if preset is None:
            return _not_found()
        master = await store.preset_master(preset.id)
        view = _preset_editor_view(preset, master)
        lang = _lang_of(request)
        response = web.Response(
            text=_routine_editor_page(
                gym.name,
                view,
                _days_from_view(view),
                await store.catalog_exercises(),
                lang,
                request.rel_url.path_qs,
                action=f"/presets/{preset.id}/routine",
                back_href="/presets",
                title_key="preset_editor_title",
                consequence_key="preset_master_consequence",
            ),
            content_type="text/html",
        )
        set_session(response, coach[0].id, gym.id)
        return response

    async def preset_save(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return _not_found()
        preset = await store.preset_for_gym(gym.id, preset_id)
        if preset is None:
            return _not_found()
        lang = _lang_of(request)
        t = STRINGS[lang]
        form = await request.post()
        base_raw = form.get("base_routine_id", "")
        base_routine_id = None
        if isinstance(base_raw, str) and base_raw.strip():
            try:
                base_routine_id = int(base_raw)
            except ValueError:
                return _not_found()
        submitted_base = base_raw if isinstance(base_raw, str) else ""

        async def reject(
            error: str,
            status: int,
            days: list[EditorDay] | None = None,
            base: str | None = None,
        ) -> web.Response:
            fresh = await store.preset_master(preset.id)
            view = _preset_editor_view(preset, fresh)
            response = web.Response(
                status=status,
                text=_routine_editor_page(
                    gym.name,
                    view,
                    days if days is not None else _days_from_view(view),
                    await store.catalog_exercises(),
                    lang,
                    f"/presets/{preset.id}/routine",
                    error=error,
                    base=base,
                    action=f"/presets/{preset.id}/routine",
                    back_href="/presets",
                    title_key="preset_editor_title",
                    consequence_key="preset_master_consequence",
                ),
                content_type="text/html",
            )
            set_session(response, coach_member.id, gym.id)
            return response

        try:
            workouts = _parse_workouts(form, lang)
        except ValueError as error:
            return await reject(
                str(error), 400, days=_days_from_form(form), base=submitted_base
            )
        if not workouts:
            return await reject(
                t["empty_routine_error"], 400, days=_days_from_form(form), base=submitted_base
            )
        try:
            copies = await store.save_preset_master_from_web(
                gym.id, preset.id, coach_member.id, base_routine_id, workouts
            )
        except StaleRoutineError:
            return await reject(t["stale_error"], 409)
        except UnknownExercisesError as error:
            message = t["unknown_exercises_error"].format(names=", ".join(error.names))
            return await reject(message, 400, days=_days_from_form(form), base=submitted_base)
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
            except Exception:
                logger.exception("failed to notify member %s of the Preset edit", copy.member_id)
        response = web.HTTPFound(f"/presets/{preset.id}/routine")
        set_session(response, coach_member.id, gym.id)
        raise response

    async def preset_apply(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return _not_found()
        if await store.preset_for_gym(gym.id, preset_id) is None:
            return _not_found()
        lang = _lang_of(request)
        t = STRINGS[lang]

        async def reject(error: str, status: int) -> web.Response:
            response = web.Response(
                status=status,
                text=_presets_page(
                    gym.name,
                    await store.presets(gym.id),
                    await store.preset_members(gym.id),
                    await store.default_preset_id(gym.id),
                    lang,
                    "/presets",
                    error=error,
                ),
                content_type="text/html",
            )
            set_session(response, coach_member.id, gym.id)
            return response

        form = await request.post()
        apply_all = bool(form.get("apply_all"))
        try:
            raw_ids = form.getall("member_ids", [])
            member_ids = [int(value) for value in raw_ids if isinstance(value, str)]
        except ValueError:
            return _not_found()
        if apply_all:
            member_ids = [member.id for member in await store.preset_members(gym.id)]
        elif not member_ids:
            return await reject(t["preset_no_selection"], 400)
        try:
            copies = await store.apply_preset(gym.id, preset_id, coach_member.id, member_ids)
        except NoPresetMasterError:
            return await reject(t["preset_no_master"], 400)
        except StaleRoutineError:
            return await reject(t["stale_error"], 409)
        except ValueError:
            return _not_found()
        for copy in copies:
            try:
                channel = await store.member_channel(copy.member_id)
                if channel is None:
                    logger.warning("failed to notify member %s of the Preset apply: no channel", copy.member_id)
                elif notifier is not None:
                    await notifier.send(channel[0], channel[1], routine_notice(coach_member.name, copy.workouts))
            except Exception:
                logger.exception("failed to notify member %s of the Preset apply", copy.member_id)
        response = web.HTTPFound("/presets")
        set_session(response, coach_member.id, gym.id)
        raise response

    async def preset_default(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return _not_found()
        if await store.preset_for_gym(gym.id, preset_id) is None:
            return _not_found()
        try:
            current_default = await store.default_preset_id(gym.id)
            await store.set_default_preset(
                gym.id, None if current_default == preset_id else preset_id
            )
        except ValueError:
            return _not_found()
        response = web.HTTPFound("/presets")
        set_session(response, coach_member.id, gym.id)
        raise response

    async def preset_retire(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        coach_member, gym = coach
        try:
            preset_id = int(request.match_info["preset_id"])
        except ValueError:
            return _not_found()
        try:
            await store.retire_preset(gym.id, preset_id)
        except ValueError:
            return _not_found()
        response = web.HTTPFound("/presets")
        set_session(response, coach_member.id, gym.id)
        raise response

    async def tick_off_flag(request: web.Request) -> web.Response:
        """Tick a safety flag off: stamp who (this Coach) and when.

        Acknowledging is not retiring — the Note stays live for the Agent.
        Anything unreachable (unknown, foreign, non-safety, retired) gets
        the shared 404."""
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        try:
            member_id = int(request.match_info["member_id"])
            note_id = int(request.match_info["note_id"])
        except ValueError:
            return _not_found()
        note = await store.acknowledge_flag(gym.id, member_id, note_id, member.id)
        if note is None:
            return _not_found()
        # Back to the Member's page, in the view it was opened from — Split
        # must not drop the Coach out of the pane.
        view = request.query.get("view", "table")
        view = view if view in VIEWS else "table"
        response = web.HTTPFound(f"/members/{member_id}?view={view}")
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        raise response

    async def settings(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        lang = _lang_of(request)
        response = web.Response(
            text=_settings_page(gym, bot_username, lang, request.rel_url.path_qs),
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
        response = web.HTTPFound("/settings")
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
        response = web.HTTPFound("/settings")
        set_session(response, member.id, gym.id)  # sliding 90-day refresh
        raise response

    async def login_form(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        if await store.peek_login_token(token) is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        return web.Response(text=_interstitial_page(token), content_type="text/html")

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

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/members/{member_id}", member_page)
    app.router.add_get("/members/{member_id}/routine", routine_editor)
    app.router.add_post("/members/{member_id}/routine", routine_save)
    app.router.add_get("/presets", presets_page)
    app.router.add_post("/presets", preset_create)
    app.router.add_get("/presets/{preset_id}/routine", preset_editor)
    app.router.add_post("/presets/{preset_id}/routine", preset_save)
    app.router.add_post("/presets/{preset_id}/apply", preset_apply)
    app.router.add_post("/presets/{preset_id}/default", preset_default)
    app.router.add_post("/presets/{preset_id}/retire", preset_retire)
    app.router.add_post("/members/{member_id}/flags/{note_id}/tick-off", tick_off_flag)
    app.router.add_get("/settings", settings)
    app.router.add_post("/settings/regenerate-invite", regenerate_invite)
    app.router.add_post("/settings/regenerate-coach", regenerate_coach)
    app.router.add_post("/settings/gym-name", gym_name)
    app.router.add_get("/lang/{lang}", set_language)
    app.router.add_get("/login/{token}", login_form)
    app.router.add_post("/login/{token}", login_redeem)
    return app


async def start_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Bind the app on the current event loop; the caller keeps the runner to
    ``cleanup()`` on shutdown."""
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("dashboard HTTP server listening on %s:%d", host, port)
    return runner

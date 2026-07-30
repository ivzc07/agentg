"""The dashboard's embedded HTTP server (spec-dashboard §Stack).

aiohttp on the bot's existing event loop, next to the long poller. Three
routes are the whole door:

- ``GET /`` — the signed-in landing: the Table roster, a dense read-only
  list of the Gym's Members Gap-sorted, gated on the session cookie *and* a
  per-request ``is_coach`` re-check.
- ``GET /members/<id>`` — the Member's page: the read-only training record
  (issue #99). A departed, forgotten, or mistyped id lands on one shared
  bare 404 — no tombstone, no "this member left" wording.
- ``GET /login/<token>`` — an interstitial that never spends the token, so
  a link-preview fetch (Telegram builds one unless the bot disables it)
  can't burn the one-time link; the browser auto-submits…
- ``POST /login/<token>`` — …which is what actually redeems the token and
  sets the session cookie.

Anything unknown, used, expired, or demoted lands on the same friendly
"send /dashboard to your bot" page — never an error.

The session is a stateless HMAC-signed cookie (member id, gym id, expiry),
re-issued on every authenticated visit so an active Coach never
re-authenticates within the 90-day sliding window.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from html import escape

from aiohttp import web

from agentg.checkin import WEEKDAY_NAMES
from agentg.dashboard_store import DashboardStore, MemberPage, NoteView, RosterRow

logger = logging.getLogger(__name__)

SESSION_COOKIE = "agentg_dashboard"
SESSION_TTL = timedelta(days=90)

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# The pages are Spanish — the product's no-signal default (spec-dashboard
# §Language). Chrome translations land with the real screens.
BOUNCE_TITLE = "Este enlace ya no sirve"
BOUNCE_BODY = (
    "Los enlaces al dashboard caducan y solo se pueden usar una vez. "
    "Envía <b>/dashboard</b> a tu bot en Telegram para recibir uno nuevo."
)
INTERSTITIAL_TITLE = "Abriendo tu dashboard…"
INTERSTITIAL_BUTTON = "Entrar al dashboard"


def _page(title: str, body: str, extra: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
</head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto; padding: 0 1rem;">
<h1>{title}</h1>
<p>{body}</p>
{extra}
</body>
</html>"""


def _bounce_page() -> str:
    return _page(BOUNCE_TITLE, BOUNCE_BODY)


def _interstitial_page(token: str) -> str:
    """A self-submitting POST form: one tap (or zero, with JS) for the human,
    a harmless GET for any link-preview fetcher."""
    form = f"""<form method="post" action="/login/{token}" id="go">
<button type="submit" style="font-size: 1rem; padding: 0.6rem 1.2rem;">{INTERSTITIAL_BUTTON}</button>
</form>
<script>document.getElementById("go").submit();</script>"""
    return _page(INTERSTITIAL_TITLE, "", form)


# --- The Table roster (spec-dashboard §The roster) ---
#
# Copy follows the adopted screens (docs/prototypes/coach-dashboard-v2.html),
# Spanish — the product's no-signal default. Severity colouring is a later
# ticket: the list ships uncoloured.

NEW_TAG = "nuevo"
NO_SESSIONS_YET = "Aún sin sesiones"


def _away_text(has_sessions: bool, gap_days: int) -> str:
    """The shared Gap wording for the roster row and the Member page header —
    one helper so the two surfaces never disagree."""
    if not has_sessions:
        return NO_SESSIONS_YET
    if gap_days == 0:
        return "entrenó hoy"
    if gap_days == 1:
        return "1 día sin venir"
    return f"{gap_days} días sin venir"


def _roster_row(row: RosterRow) -> str:
    tags = ""
    if row.is_new:
        tags += f' <span class="tag tag-new">{NEW_TAG}</span>'
    if row.snoozed_until is not None:
        until = row.snoozed_until.strftime("%d/%m/%Y")
        tags += f' <span class="tag">en pausa hasta el {until}</span>'
    return (
        f'<li class="row" data-name="{escape(row.name)}">'
        f'<a class="name" href="/members/{row.member_id}">{escape(row.name)}</a>{tags}'
        f'<span class="away">{_away_text(row.has_sessions, row.gap_days)}</span></li>'
    )


def _roster_page(
    gym_name: str, rows: list[RosterRow], lapsed: list[RosterRow]
) -> str:
    lapsed_section = ""
    if lapsed:
        items = "".join(_roster_row(row) for row in lapsed)
        lapsed_section = f"""<details id="lapsed">
<summary>Se perdieron ({len(lapsed)})</summary>
<ul>{items}</ul>
</details>"""
    items = "".join(_roster_row(row) for row in rows)
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(gym_name)} — Dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 40rem; margin: 2rem auto; padding: 0 1rem; }}
header {{ display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap; }}
header h1 {{ font-size: 1.25rem; margin: 0; }}
header .count {{ color: #666; }}
#search {{ margin-left: auto; font-size: 1rem; padding: 0.3rem 0.6rem; }}
ul {{ list-style: none; padding: 0; margin: 1rem 0; }}
.row {{ display: flex; align-items: baseline; gap: 0.6rem; padding: 0.45rem 0.2rem; border-bottom: 1px solid #eee; }}
.row a.name {{ font-weight: 600; color: inherit; }}
.row .away {{ margin-left: auto; color: #666; font-size: 0.9rem; white-space: nowrap; }}
.tag {{ font-size: 0.75rem; padding: 0.1rem 0.45rem; border-radius: 1rem; background: #eee; color: #555; white-space: nowrap; }}
#lapsed summary {{ cursor: pointer; color: #666; }}
</style>
</head>
<body>
<header>
<h1>{escape(gym_name)}</h1>
<span class="count">Miembros ({len(rows)})</span>
<input id="search" type="search" placeholder="Buscar por nombre" autocomplete="off">
</header>
<ul id="roster">{items}</ul>
{lapsed_section}
<script>
// Live, name-only, accent-insensitive filter. It only hides rows — the Gap
// sort never moves — and a lapsed match auto-expands the tail.
const box = document.getElementById("search");
const lapsed = document.getElementById("lapsed");
const norm = (s) => s.normalize("NFD").replace(/[\\u0300-\\u036f]/g, "").toLowerCase();
box.addEventListener("input", () => {{
  const q = norm(box.value.trim());
  let lapsedHit = false;
  document.querySelectorAll("[data-name]").forEach((row) => {{
    const hit = !q || norm(row.dataset.name).includes(q);
    row.hidden = !hit;
    if (hit && q && lapsed && lapsed.contains(row)) lapsedHit = true;
  }});
  if (lapsed) lapsed.open = lapsedHit;
}});
</script>
</body>
</html>"""


# --- The Member page (issue #99, spec-dashboard §The Member page) ---
#
# Read-only: the Routine Edit entry point is a later ticket. Copy follows the
# adopted screens (docs/prototypes/coach-dashboard-v2.html), Spanish — the
# product's no-signal default.

NOTE_KIND_LABELS = {
    "injury": "lesión",
    "preference": "preferencia",
    "goal": "objetivo",
    "constraint": "limitación",
    "safety": "seguridad",
    "other": "otro",
}


def _fmt_date(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _not_found() -> web.Response:
    """The shared dead end: a departed, forgotten, or mistyped Member id all
    get the same bare 404 — no tombstone, no "this member left" wording, so
    the two exits stay indistinguishable (spec-dashboard §What a Coach sees)."""
    return web.Response(status=404, text="404", content_type="text/plain")


def _scheme(sets: int | None, reps: str | None) -> str:
    if sets is not None and reps is not None:
        return f" — {sets} × {escape(reps)}"
    if reps is not None:
        return f" — {escape(reps)}"
    return ""


def _set_lines(sets: list[tuple[str, float | None, int]], unit: str) -> str:
    """Collapse a Session's sets into one line per (Exercise, weight):
    ``bench press 60 kg × 8,8,8`` — warm-ups at another weight stay separate."""
    lines = []
    grouped: dict[tuple[str, float | None], list[int]] = {}
    for name, weight, reps in sets:
        grouped.setdefault((name, weight), []).append(reps)
    for (name, weight), reps_list in grouped.items():
        load = f"{weight:g} {unit}" if weight is not None else "peso corporal"
        lines.append(
            f'<div class="set">{escape(name)} {load} × {",".join(str(r) for r in reps_list)}</div>'
        )
    return "".join(lines)


def _routine_card(view: MemberPage) -> str:
    if not view.routine:
        body = '<p class="muted">Sin rutina activa</p>'
    else:
        days = []
        for day in view.routine:
            exercises = "".join(
                f"<li>{escape(name)}{_scheme(sets, reps)}</li>"
                for name, sets, reps in day.exercises
            )
            days.append(
                f'<div class="day"><b>{WEEKDAY_NAMES[day.weekday]}</b> '
                f"{escape(day.name)}<ul>{exercises}</ul></div>"
            )
        body = "".join(days)
    return f'<section class="card"><h2>Rutina</h2>{body}</section>'


def _sessions_card(view: MemberPage) -> str:
    items = []
    for session in view.sessions:
        count = len(session.sets)
        if count == 0:
            headline = "visita registrada, sin series"
        elif count == 1:
            headline = "1 serie"
        else:
            headline = f"{count} series"
        items.append(
            f'<div class="sess"><b>{_fmt_date(session.on)}</b> '
            f'<span class="muted">{headline}</span>'
            f"{_set_lines(session.sets, view.weight_unit)}</div>"
        )
    if not items:
        items.append(f'<p class="muted">{NO_SESSIONS_YET}</p>')
    nav = ""
    if view.pages > 1:
        newer = (
            f'<a href="/members/{view.member_id}?page={view.page - 1}">‹ más recientes</a>'
            if view.page > 1
            else ""
        )
        older = (
            f'<a href="/members/{view.member_id}?page={view.page + 1}">más antiguas ›</a>'
            if view.page < view.pages
            else ""
        )
        nav = (
            f'<nav class="pages">{newer}'
            f'<span class="muted">página {view.page} de {view.pages}</span>{older}</nav>'
        )
    return f'<section class="card"><h2>Sesiones</h2>{"".join(items)}{nav}</section>'


def _weights_card(view: MemberPage) -> str:
    if not view.weights:
        rows = '<p class="muted">Nada registrado aún</p>'
    else:
        rows = "".join(
            f'<li><b>{escape(w.exercise)}</b> '
            f'{f"{w.weight:g} {view.weight_unit}" if w.weight is not None else "peso corporal"}'
            f' × {",".join(str(r) for r in w.reps)}'
            f' <span class="muted">· {_fmt_date(w.on)}</span></li>'
            for w in view.weights
        )
        rows = f"<ul>{rows}</ul>"
    return f'<section class="card"><h2>Últimos pesos</h2>{rows}</section>'


def _note_row(note: NoteView) -> str:
    label = NOTE_KIND_LABELS.get(note.kind, note.kind)
    retired = (
        f' <span class="muted">· retirada el {_fmt_date(note.retired_on)}</span>'
        if note.retired_on is not None
        else ""
    )
    return (
        f'<div class="note"><span class="tag">{escape(label)}</span> '
        f"{escape(note.text)}"
        f' <span class="muted">· {_fmt_date(note.on)}</span>{retired}</div>'
    )


def _notes_card(view: MemberPage) -> str:
    body = "".join(_note_row(note) for note in view.notes) or (
        '<p class="muted">Sin notas</p>'
    )
    if view.retired_notes:
        retired = "".join(_note_row(note) for note in view.retired_notes)
        body += f"""<details class="tail">
<summary>Retiradas ({len(view.retired_notes)})</summary>
{retired}
</details>"""
    return f'<section class="card"><h2>Notas</h2>{body}</section>'


def _member_page(gym_name: str, view: MemberPage) -> str:
    tags = ""
    if view.lapsed:
        tags += ' <span class="tag">se perdió</span>'
    if view.snoozed_until is not None:
        tags += f' <span class="tag">en pausa hasta el {_fmt_date(view.snoozed_until)}</span>'
    count = "1 sesión" if view.session_count == 1 else f"{view.session_count} sesiones"
    facts = f"Miembro desde {_fmt_date(view.member_since)} · {count} · {_away_text(view.has_sessions, view.gap_days)}"
    if view.last_session_on is not None:
        facts += f" · última sesión {_fmt_date(view.last_session_on)}"
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(view.name)} — {escape(gym_name)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 60rem; margin: 2rem auto; padding: 0 1rem; }}
a.back {{ color: #666; text-decoration: none; }}
header h1 {{ font-size: 1.4rem; margin: 0.5rem 0 0.2rem; }}
header .facts {{ color: #666; }}
.columns {{ display: flex; gap: 2rem; flex-wrap: wrap; align-items: flex-start; }}
.col {{ flex: 1; min-width: 18rem; }}
.card {{ margin: 1rem 0; }}
.card h2 {{ font-size: 1rem; margin: 0 0 0.5rem; }}
.card ul {{ margin: 0.2rem 0 0.6rem; padding-left: 1.2rem; }}
.day b {{ text-transform: capitalize; }}
.sess {{ padding: 0.4rem 0; border-bottom: 1px solid #eee; }}
.sess .set {{ color: #333; font-size: 0.9rem; }}
.note {{ padding: 0.3rem 0; }}
.muted {{ color: #666; font-size: 0.9rem; }}
.tag {{ font-size: 0.75rem; padding: 0.1rem 0.45rem; border-radius: 1rem; background: #eee; color: #555; white-space: nowrap; }}
.pages {{ display: flex; gap: 1rem; margin-top: 0.6rem; }}
.pages .muted {{ margin: 0 auto; }}
.tail summary {{ cursor: pointer; color: #666; }}
</style>
</head>
<body>
<a class="back" href="/">← Todos los miembros</a>
<header>
<h1>{escape(view.name)}{tags}</h1>
<div class="facts">{facts}</div>
</header>
<div class="columns">
<div class="col">
{_routine_card(view)}
{_sessions_card(view)}
</div>
<div class="col">
{_weights_card(view)}
{_notes_card(view)}
</div>
</div>
</body>
</html>"""


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
    *,
    session_secret: str,
    secure_cookies: bool = True,
    clock: Clock = _utcnow,
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

    async def home(request: web.Request) -> web.Response:
        identity = await session_identity(request)
        if identity is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        coach = await store.coach_identity(*identity)
        if coach is None:  # demoted or forgotten: out on the next click
            return web.Response(text=_bounce_page(), content_type="text/html")
        _, gym = coach
        rows, lapsed = await store.roster(gym.id)
        response = web.Response(
            text=_roster_page(gym.name, rows, lapsed), content_type="text/html"
        )
        set_session(response, *identity)  # sliding 90-day refresh on every visit
        return response

    async def member_page(request: web.Request) -> web.Response:
        identity = await session_identity(request)
        if identity is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        coach = await store.coach_identity(*identity)
        if coach is None:  # demoted or forgotten: out on the next click
            return web.Response(text=_bounce_page(), content_type="text/html")
        _, gym = coach
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
        response = web.Response(
            text=_member_page(gym.name, view), content_type="text/html"
        )
        set_session(response, *identity)  # sliding 90-day refresh on every visit
        return response

    async def login_form(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        if await store.peek_login_token(token) is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        return web.Response(text=_interstitial_page(token), content_type="text/html")

    async def login_redeem(request: web.Request) -> web.Response:
        token = await store.redeem_login_token(request.match_info["token"])
        if token is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        response = web.HTTPFound("/")
        set_session(response, token.member_id, token.gym_id)
        raise response

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/members/{member_id}", member_page)
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

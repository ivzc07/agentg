"""The dashboard's embedded HTTP server (spec-dashboard §Stack).

aiohttp on the bot's existing event loop, next to the long poller. Three
routes are the whole door:

- ``GET /`` — the signed-in landing: the Table roster, a dense read-only
  list of the Gym's Members Gap-sorted, gated on the session cookie *and* a
  per-request ``is_coach`` re-check.
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
from datetime import UTC, datetime, timedelta
from html import escape

from aiohttp import web

from agentg.dashboard_store import DashboardStore, RosterRow

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
# Spanish — the product's no-signal default. Severity colouring (issue #98)
# follows each Member's own schedule; the palette is the prototype's.

NEW_TAG = "nuevo"
NO_SESSIONS_YET = "Aún sin sesiones"


def _away_text(row: RosterRow) -> str:
    if not row.has_sessions:
        return NO_SESSIONS_YET
    if row.gap_days == 1:
        return "1 día sin venir"
    return f"{row.gap_days} días sin venir"


def _roster_row(row: RosterRow) -> str:
    tags = ""
    if row.is_new:
        tags += f' <span class="tag tag-new">{NEW_TAG}</span>'
    if row.snoozed_until is not None:
        until = row.snoozed_until.strftime("%d/%m/%Y")
        tags += f' <span class="tag">en pausa hasta el {until}</span>'
    severity = f" sev-{row.severity}" if row.severity else ""
    # Read-only: the click-through to the Member's page is a later ticket.
    return (
        f'<li class="row" data-name="{escape(row.name)}">'
        f'<span class="name">{escape(row.name)}</span>{tags}'
        f'<span class="away{severity}">{_away_text(row)}</span></li>'
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
.row .name {{ font-weight: 600; }}
.row .away {{ margin-left: auto; color: #666; font-size: 0.9rem; white-space: nowrap; }}
.row .away.sev-amber {{ color: #9a5b00; font-weight: 600; }}
.row .away.sev-red {{ color: #b3261e; font-weight: 600; }}
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

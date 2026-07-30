"""The dashboard's embedded HTTP server (spec-dashboard §Stack).

aiohttp on the bot's existing event loop, next to the long poller. Three
routes are the whole door:

- ``GET /`` — the signed-in shell, gated on the session cookie *and* a
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

from agentg.dashboard_store import DashboardStore

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


def _shell_page(gym_name: str) -> str:
    body = f"Has iniciado sesión como coach de <b>{escape(gym_name)}</b>."
    return _page("Dashboard", body)


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
        response = web.Response(text=_shell_page(gym.name), content_type="text/html")
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

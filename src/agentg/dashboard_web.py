"""The dashboard's embedded HTTP server (spec-dashboard §Stack).

aiohttp on the bot's existing event loop, next to the long poller. The door
is three routes:

- ``GET /`` — the signed-in shell, gated on the session cookie *and* a
  per-request ``is_coach`` re-check.
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

import qrcode
import qrcode.image.svg
from aiohttp import web

from agentg.dashboard_store import DashboardStore
from agentg.linking_store import LinkingStore
from agentg.models import Gym, Member

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

# The typed confirm gating both Regenerate buttons (spec-dashboard
# §Settings): the word must be typed before the POST does anything, client-
# and server-side, because regenerating invalidates half-finished linking
# conversations.
REGENERATE_CONFIRM = "regenerar"

SETTINGS_TITLE = "Ajustes"
SETTINGS_INVITE_WARNING = (
    "Regenerar el enlace invalida el código actual — quien esté a mitad de "
    "vincularse tendrá que empezar de nuevo con el enlace nuevo."
)
SETTINGS_COACH_WARNING = (
    "Regenerar el enlace de coach invalida el código actual. Los coaches "
    "que ya se vincularon conservan su acceso."
)
SETTINGS_GYM_NAME_HELP = "Es el nombre que ven los miembros al unirse."
CONFIRM_MISMATCH_ERROR = f"Escribe <b>{REGENERATE_CONFIRM}</b> para confirmar la regeneración."
GYM_NAME_EMPTY_ERROR = "El nombre del gimnasio no puede estar vacío."


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
    return _page("Dashboard", body, '<p><a href="/settings">Ajustes</a></p>')


def _invite_url(bot_username: str, code: str) -> str:
    """The deep link a joiner taps: ``t.me/<bot>?start=<code>``."""
    return f"https://t.me/{bot_username}?start={code}"


def _qr_svg(data: str) -> str:
    """An inline SVG QR for the member invite link — a poster the Coach can
    print. The coach link gets none: it is forwarded privately, not posted."""
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    return image.to_string(encoding="unicode")


def _copy_button(url: str) -> str:
    return (
        f'<button type="button" class="copy" data-copy="{escape(url, quote=True)}">'
        "Copiar</button>"
    )


def _regenerate_form(action: str, warning: str) -> str:
    """A Regenerate button that stays disabled until the confirm word is
    typed; the POST re-checks the word, so the confirm holds without JS."""
    return f"""<form method="post" action="{action}" data-confirm="{REGENERATE_CONFIRM}">
<p>{warning}</p>
<p><label>Escribe <b>{REGENERATE_CONFIRM}</b> para confirmar:
<input type="text" name="confirm" autocomplete="off" required></label>
<button type="submit" disabled>Regenerar</button></p>
</form>"""


SETTINGS_SCRIPT = """<script>
document.querySelectorAll("button.copy").forEach(function (button) {
  button.addEventListener("click", function () {
    navigator.clipboard.writeText(button.dataset.copy);
    button.textContent = "Copiado";
  });
});
document.querySelectorAll("form[data-confirm]").forEach(function (form) {
  var input = form.querySelector("input[name=confirm]");
  var submit = form.querySelector("button[type=submit]");
  input.addEventListener("input", function () {
    submit.disabled = input.value.trim().toLowerCase() !== form.dataset.confirm;
  });
});
</script>"""


def _settings_page(gym: Gym, bot_username: str, error: str = "") -> str:
    """The whole tenant Settings screen: two invite links and the gym name,
    nothing else (spec-dashboard §Settings — no new settings)."""
    member_url = _invite_url(bot_username, gym.invite_code)
    coach_url = _invite_url(bot_username, gym.coach_invite_code or "")
    notice = f'<p style="color: #b00;">{error}</p>' if error else ""
    body = f"""{notice}
<section id="invite">
<h2>Enlace de invitación</h2>
<p>El que usan los nuevos miembros para unirse a <b>{escape(gym.name)}</b>.</p>
<p><code>{escape(member_url)}</code> {_copy_button(member_url)}</p>
{_qr_svg(member_url)}
{_regenerate_form("/settings/regenerate-invite", SETTINGS_INVITE_WARNING)}
</section>
<section id="coach-link">
<h2>Enlace para coaches</h2>
<p>Privado: reenvíaselo solo a quien quieras sumar como coach.</p>
<p><code>{escape(coach_url)}</code> {_copy_button(coach_url)}</p>
{_regenerate_form("/settings/regenerate-coach", SETTINGS_COACH_WARNING)}
</section>
<section id="gym-name">
<h2>Nombre del gimnasio</h2>
<p>{SETTINGS_GYM_NAME_HELP}</p>
<form method="post" action="/settings/gym-name">
<p><input type="text" name="name" value="{escape(gym.name, quote=True)}"
maxlength="200" required>
<button type="submit">Guardar</button></p>
</form>
</section>
<p><a href="/">Volver al dashboard</a></p>
{SETTINGS_SCRIPT}"""
    return _page(f"{SETTINGS_TITLE} — {escape(gym.name)}", "", body)


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

    def signed_in(response: web.StreamResponse, member_id: int, gym_id: int) -> None:
        set_session(response, member_id, gym_id)  # sliding 90-day refresh

    async def home(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        response = web.Response(text=_shell_page(gym.name), content_type="text/html")
        signed_in(response, member.id, gym.id)
        return response

    async def settings(request: web.Request) -> web.Response:
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        response = web.Response(
            text=_settings_page(gym, bot_username), content_type="text/html"
        )
        signed_in(response, member.id, gym.id)
        return response

    async def _regenerate(request: web.Request, which: str) -> web.Response:
        """Regenerate one invite code behind the typed confirm. A wrong or
        missing confirm changes nothing — the form's JS gate is convenience;
        this check is the load-bearing one."""
        coach = await require_coach(request)
        if coach is None:
            return web.Response(text=_bounce_page(), content_type="text/html")
        member, gym = coach
        form = await request.post()
        confirm = form.get("confirm", "")
        if not isinstance(confirm, str) or confirm.strip().lower() != REGENERATE_CONFIRM:
            response = web.Response(
                text=_settings_page(gym, bot_username, error=CONFIRM_MISMATCH_ERROR),
                content_type="text/html",
            )
            signed_in(response, member.id, gym.id)
            return response
        if which == "invite":
            await linking.regenerate_invite_code(gym.id)
        else:
            await linking.regenerate_coach_invite_code(gym.id)
        response = web.HTTPFound("/settings")
        signed_in(response, member.id, gym.id)
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
        form = await request.post()
        name = form.get("name", "")
        if not isinstance(name, str) or not name.strip():
            response = web.Response(
                text=_settings_page(gym, bot_username, error=GYM_NAME_EMPTY_ERROR),
                content_type="text/html",
            )
            signed_in(response, member.id, gym.id)
            return response
        await linking.rename_gym(gym.id, name)
        response = web.HTTPFound("/settings")
        signed_in(response, member.id, gym.id)
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
        response = web.HTTPFound("/")
        set_session(response, token.member_id, token.gym_id)
        raise response

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/settings", settings)
    app.router.add_post("/settings/regenerate-invite", regenerate_invite)
    app.router.add_post("/settings/regenerate-coach", regenerate_coach)
    app.router.add_post("/settings/gym-name", gym_name)
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

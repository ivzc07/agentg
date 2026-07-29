# How a Coach signs in to a web dashboard

Research for [#73](https://github.com/ivzc07/agentg/issues/73), on the map [Coach web dashboard](https://github.com/ivzc07/agentg/issues/70).
Planning only: nothing here is a build instruction.

## Where identity lives today

There are no passwords, emails, or sessions anywhere in the product.
A chat identity is a row in `member_channels` (`channel`, `channel_user_id`), unique on the pair, pointing at a `Member` who belongs to exactly one `Gym` (`src/agentg/models.py:83`).
`channel_user_id` is the Telegram numeric user id as a string, never the mutable `@username` (`src/agentg/channels/telegram.py:70`).

`LinkingStore.identity_for(channel, channel_user_id)` already resolves that pair straight to a `LinkedIdentity(member, gym)` in one query (`src/agentg/linking_store.py:70`).
A Coach is a `Member` with `is_coach = True` (`src/agentg/models.py:71`).

So the whole of "who is signing in, and which Gym do they own" reduces to: **get a trustworthy Telegram user id into the web request**.
Every option below is judged on how it does that and what it costs.

## Option A: Telegram Login (OpenID Connect)

Telegram Login is now a standard **OpenID Connect provider** at `https://oauth.telegram.org`, discovery at `https://oauth.telegram.org/.well-known/openid-configuration`.
The old iframe-based JavaScript widget, the one every blog post and tutorial describes, is explicitly archived as legacy.
This matters: the widely copied verification recipe (`secret_key = SHA256(bot_token)`, compare a `hash` field) belongs to that legacy path, not to the current one.

**Setup.** In @BotFather, Bot Settings > Web Login, register the Allowed URLs (the site origin and the redirect URI), and collect a Client ID (the bot id) and Client Secret.
Telegram will only redirect to pre-registered URLs.

**Flow.** Authorization Code with PKCE (S256).
The browser goes to `https://oauth.telegram.org/auth` with `client_id`, `redirect_uri`, `response_type=code`, `scope`, `state`, `code_challenge`.
The callback returns a `code`, which the server exchanges at `https://oauth.telegram.org/token` using Basic auth over `client_id:client_secret`.
The response carries an `id_token`: an RS256 JWT by default, verified against `https://oauth.telegram.org/.well-known/jwks.json`, checking `iss` is `https://oauth.telegram.org`, `aud` is the bot id, and `exp`.
There is no separate UserInfo endpoint; all claims ride in the ID token.

**The mapping detail that decides whether this works at all.**
The claim equal to the Telegram user id is `id`, and `id` is returned only under the `profile` scope.
The `sub` claim is a different, longer, opaque identifier.
Since `member_channels.channel_user_id` stores the Bot API user id, the join key must be `id`, which makes `profile` a mandatory scope rather than an optional nicety.
Confirm this empirically with one real login against a real bot before anything is built on it; the whole option rests on `id` being byte-identical to `message.from_user.id`.

**Maps onto existing rows.** `id` becomes `identity_for("telegram", str(id))`, which returns the Member and Gym.
`member.is_coach` gates the dashboard.
**No new domain tables.**

**Forces into the data model.** Only a session, and only if sessions are server-side.
A signed cookie carrying `member_id`, `gym_id`, and an issued-at timestamp needs no table at all.

**Cost.** An OIDC client library (`authlib` is the obvious Python choice), a public HTTPS origin registered with BotFather, and cookie handling.
No email vendor, no password storage, no reset flow.
One gotcha if the JavaScript popup library is used rather than a plain server-side redirect: a `Cross-Origin-Opener-Policy: same-origin` header breaks the flow, and must be relaxed to `same-origin-allow-popups`.

**Risks.** A hard dependency on Telegram's OIDC service being up, and on it staying stable, since it is new enough that the legacy path is still what most of the internet documents.
A Coach who moves to a different Telegram account loses dashboard access until they re-link, which is already true of their chat.

## Option B: bot-issued magic link

The Coach types `/dashboard` in the chat they already use.
The bot replies with a one-time URL; opening it sets the session cookie.

The identity is already resolved before the link is even generated: the bot handled a message, so it has `channel_user_id`, so `identity_for` has already produced the Member and Gym.
The link is the proof of identity, because only that Telegram account could have received it.

**Maps onto existing rows.** Same `identity_for` path as A. No new domain concepts.

**Forces into the data model.** One new table, roughly `dashboard_login_tokens` (token hash, `member_id`, `gym_id`, `expires_at`, `used_at`), plus the same session mechanism as A.
A stateless signed token avoids the table, but single-use redemption needs somewhere to record "spent", so the table comes back.

**Cost.** The cheapest of the three.
No BotFather configuration, no OIDC library, no external service in the login path, no third-party cookie or COOP concerns.
It also survives the channel swap the spec is built around (`docs/spec.md:35-43`): on WhatsApp the same flow is the same flow, whereas Telegram OIDC would have to be replaced outright.

**Risks.** A link sitting in chat history is a bearer secret: forwardable, screenshot-able, and it lives in the transcript.
Short TTL (minutes, not hours), single use, and binding the session to the browser that redeemed it are the mitigations.
Telegram may fetch the URL server-side to build a link preview, which would burn a single-use token before the human clicks it; disabling the preview or redeeming on POST handles it, but it needs testing rather than assuming.

## Option C: email plus password

Nothing in the product holds an email address or a password.
`Member` carries a name, `is_coach`, and check-in state, and nothing else about a person.
No mail sender exists anywhere: the dependency list is aiogram, openai-agents, asyncpg, apscheduler (`pyproject.toml`).

**Forces into the data model.** An `email` (unique) and `password_hash`, either on `Member` or in a separate `coach_credentials` table, plus email-verification tokens, plus password-reset tokens, plus sessions.
That is three or four new concepts against zero or one for the alternatives.

**Cost.** The highest by a wide margin, and the cost is mostly not the login screen.
It is a transactional email provider (a new vendor, a new secret, a new Coolify env var, and deliverability as an ongoing concern), a password hashing dependency, login rate limiting, and a full account-recovery flow.
The circular part: the most trustworthy recovery channel available to this product is the Telegram bot, which is option B.

**When it would earn its place.** Only if a Coach must reach the dashboard without a Telegram account.
Per `CONTEXT.md`, a Coach is a coach-flagged Member chatting through the same bot, so that person does not exist today.

## Option D: Telegram Mini App (not in the ticket, but adjacent)

The dashboard opens inside Telegram itself.
The client hands the page a signed `initData` string, verified with `secret_key = HMAC_SHA256(bot_token, "WebAppData")` and then `HMAC_SHA256(data_check_string, secret_key) == hash`, with an `auth_date` freshness check on top.
Zero new tables, zero new vendors, no login screen at all, and the Coach never leaves the app they already live in.

Against it: a Mini App is shaped like a phone.
[#71](https://github.com/ivzc07/agentg/issues/71) landed a Gym-wide roster sorted by Gap plus a Routine-writing screen, and writing a Routine wants a keyboard and a wide viewport.
Telegram Desktop does run Mini Apps, so this is a constraint rather than a disqualification, and it is a constraint the prototype in [#76](https://github.com/ivzc07/agentg/issues/76) would have to design around.

## What every option needs regardless

None of these are free, because the repo has no web surface at all yet.
Whichever wins, three things arrive with it:

1. **A web server.** Nothing in `pyproject.toml` speaks HTTP.
2. **A public HTTPS origin.** Delivery today is long polling precisely so there is "no public endpoint, domain, or webhook secret" (`docs/spec.md:41`). That sentence stops being true.
3. **A session.** Otherwise the Coach re-authenticates on every page.

The single-replica constraint (exactly one process may poll a bot token) means the web app either shares the bot's process or becomes a second Coolify application pointed at the same Postgres.
That is a deployment question, and it belongs to the fog patch the map already records, not to this ticket.

## Where this leaves the decision

Option C is ruled out on evidence: it invents an identity system the product does not have, to serve a user who does not exist.

Options A and B are both cheap and both map onto `identity_for` with no new domain concepts.
The choice between them is not really technical, it is a question about the Coach's day:

- **A** gives a bookmarkable URL. The Coach opens the dashboard like any other web app, and Telegram is only the identity provider.
- **B** makes the chat the front door. The Coach asks the bot for the dashboard and gets let in, and the web app is an extension of the conversation rather than a separate place.

B is cheaper to build, has no external dependency in the login path, and survives a future channel swap.
A is more conventional, does not put a bearer token in a chat transcript, and reads as a real product rather than a back door.

That is a product call, not a research finding, so it is left to a human.

## Sources

- Telegram, *Log In With Telegram* (current OIDC flow; legacy widget archived): https://core.telegram.org/widgets/login
- Telegram, *Telegram Mini Apps* (`initData` validation): https://core.telegram.org/bots/webapps
- This repo: `src/agentg/models.py`, `src/agentg/linking_store.py`, `src/agentg/channels/telegram.py`, `docs/spec.md`, `CONTEXT.md`, `pyproject.toml`

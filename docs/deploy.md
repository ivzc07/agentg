# Production deployment (Coolify)

Spec: [docs/spec.md — Hosting & deployment](spec.md#hosting--deployment).
Production runs on the owner's Coolify instance (host IP
`187.77.195.48`, server `localhost`), project **agentg**, environment
**production**. Everything below was created via the Coolify API and lives in
Coolify — nothing secret is in this repo.

## Shape

- **Application `agentg`** (uuid `c13aisi5hlhwh48dzy8xvk62`) — Dockerfile build
  from `git@github.com:ivzc07/agentg.git`, branch `main`, cloned with a read-only
  deploy key (`agentg-deploy-key`). The public coach dashboard is
  `https://agentg.187.77.195.48.sslip.io`, routed by Coolify to the embedded
  HTTP server on `DASHBOARD_PORT` (8080). `DASHBOARD_BASE_URL` uses that same
  HTTPS origin, so Telegram magic links open the public deployment. Single
  replica — exactly one instance may poll a given bot token; do not scale this
  app.
- **Database `agentg-postgres`** (uuid `t5hwnbn31qoamtdr74vggj9k`) — a standalone
  Coolify Postgres 16 resource (not compose-managed, so Coolify's scheduled
  backups can cover it). Not publicly exposed; the app reaches it over the shared
  Docker network at hostname `t5hwnbn31qoamtdr74vggj9k:5432`.

Single-instance is enforced by shape, not by an option: Coolify runs one
container per application and nothing here configures replicas or additional
servers. Scaling the app up would break long polling — don't.

## Auto-deploy

Push to `main` → GitHub webhook
(`https://<coolify-host>/webhooks/source/github/events/manual`, secured
with the app's webhook secret) → Coolify pulls, builds the Dockerfile, and swaps
the container. Merging a PR is a deploy.

To recreate the webhook: GitHub → ivzc07/agentg → Settings → Webhooks — push
events, JSON payload, to the URL above; the secret is the application's
"Manual Git Webhook Secret (GitHub)" shown in Coolify under
Application → agentg → Webhooks.

## Configuration (Coolify env vars, runtime-only)

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | production bot token from @BotFather |
| `MODEL` | LiteLLM model string (`deepseek/deepseek-chat` in production) |
| `MODEL_API_KEY` | API key matching `MODEL` |
| `DATABASE_URL` | `postgresql+asyncpg://…@t5hwnbn31qoamtdr74vggj9k:5432/agentg` |
| `DASHBOARD_BASE_URL` | public origin for `/dashboard` magic links — set when the subdomain is attached (defaults to `http://localhost:8080`) |
| `DASHBOARD_PORT` | embedded HTTP server port (defaults to `8080`) |
| `DASHBOARD_SESSION_SECRET` | optional session-cookie HMAC key (defaults to the bot token) |

All required variables are configured in Coolify as runtime secrets. The
production container is running, connected to Postgres, serving the dashboard,
and long-polling Telegram. Never copy secret values into this repository.

## Backups

`agentg-postgres` has a Coolify scheduled backup at **03:00 UTC daily**. It
keeps 7 local copies and 30 offsite copies in the existing Cloudflare R2
storage (`r2-chatwoot`); Coolify isolates the agentg objects under the database
resource's own path.

The initial backup completed successfully on 2026-08-15 and produced an offsite
R2 object. That dump was restored into an isolated throwaway Postgres 16
container and verified (19 public tables, 1 gym, 1 member), then the throwaway
container was deleted. Restore future incidents through Database →
`agentg-postgres` → Backups.

## Environments

Production only. Developers test locally with a separate dev bot token
(see [README](../README.md#run-locally)); there is no staging.

## Go-live checklist (one-time)

1. Production bot created and configured in Coolify.
2. Production model and matching API key configured in Coolify.
3. `agentg` application running from `main` as a single replica.
4. Public dashboard routed over HTTPS to port 8080.
5. Daily offsite backup configured and restore-tested (see [Backups](#backups)).

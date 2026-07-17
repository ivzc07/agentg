# Production deployment (Coolify)

Spec: [docs/spec.md — Hosting & deployment](spec.md#hosting--deployment).
Production runs on the existing Coolify VPS (`45.132.242.102`), project **agentg**,
environment **production**. Everything below was created via the Coolify API and
lives in Coolify — nothing secret is in this repo.

## Shape

- **Application `agentg`** (uuid `zdfud9x2qzchbhmug9uukvmi`) — Dockerfile build
  from `git@github.com:ivzc07/agentg.git`, branch `main`, cloned with a read-only
  deploy key (`agentg-deploy-key`). No domain and no exposed ports: the app only
  long-polls Telegram. Health checks are off (no HTTP surface). Single replica —
  exactly one instance may poll a given bot token; do not scale this app.
- **Database `agentg-postgres`** (uuid `bvsw36dl6dnqq1mhn7lm22ed`) — a standalone
  Coolify Postgres 16 resource (not compose-managed, so Coolify's scheduled
  backups cover it). Not publicly exposed; the app reaches it over the shared
  Docker network at hostname `bvsw36dl6dnqq1mhn7lm22ed:5432`.

Single-instance is enforced by shape, not by an option: Coolify runs one
container per application and nothing here configures replicas or additional
servers. Scaling the app up would break long polling — don't.

## Auto-deploy

Push to `main` → GitHub webhook (`.../webhooks/source/github/events/manual` on
the Coolify instance, secured with the app's webhook secret) → Coolify pulls,
builds the Dockerfile, and swaps the container. Merging a PR is a deploy.

To recreate the webhook: GitHub → ivzc07/agentg → Settings → Webhooks — push
events, JSON payload, to the URL above; the secret is the application's
"Manual Git Webhook Secret (GitHub)" shown in Coolify under
Application → agentg → Webhooks.

## Configuration (Coolify env vars, runtime-only)

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | production bot token from @BotFather |
| `MODEL` | LiteLLM model string (`openai/gpt-4o-mini`) |
| `MODEL_API_KEY` | API key matching `MODEL` |
| `DATABASE_URL` | `postgresql+asyncpg://…@bvsw36dl6dnqq1mhn7lm22ed:5432/agentg` |

`DATABASE_URL` and the schedule below are already set. `TELEGRAM_BOT_TOKEN` and
`MODEL_API_KEY` hold `CHANGE_ME…` placeholders until the owner sets the real
values in Coolify (Application → agentg → Environment Variables) and starts the
app. With placeholders the container exits at aiogram's token validation — after
it has connected to Postgres and created the schema, so clone, build, deploy,
and database wiring are all verified; only the Telegram reply path waits on the
real secrets.

## Backups

Daily `pg_dump --all` of `agentg-postgres` at 03:00 UTC via Coolify's scheduled
backup: 7 dumps kept locally, 30 kept offsite in the `r2-backup` S3 storage
(Cloudflare R2) — ~30 days at daily cadence, same policy as the other databases
on this box. Restore through Coolify: Database → agentg-postgres → Backups.

## Environments

Production only. Developers test locally with a separate dev bot token
(see [README](../README.md#run-locally)); there is no staging.

## Go-live checklist (one-time)

1. Create the production bot with @BotFather.
2. In Coolify, replace the `TELEGRAM_BOT_TOKEN` and `MODEL_API_KEY` placeholders.
3. Start (or redeploy) the `agentg` application.
4. Message the production bot — the Agent should reply.

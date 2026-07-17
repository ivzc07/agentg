# agentg

A chat-based gym coach agent (see [docs/spec.md](docs/spec.md) and [CONTEXT.md](CONTEXT.md)).
Members chat with the Agent on Telegram; conversation history lives in Postgres.

## Run locally

1. Create a dev bot with [@BotFather](https://t.me/BotFather) (separate from production).
2. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN` and `MODEL_API_KEY`
   (plus `MODEL` if you don't want the default).
3. ```
   docker compose up --build
   ```

Message your dev bot on Telegram — you get a model-generated reply. Restart the
app (`docker compose restart app`) and it still remembers the conversation:
history is stored in Postgres via the Agents SDK session store.

Delivery is long polling — no public endpoint or webhook. Exactly one instance
may poll a given bot token, so keep the app at a single replica.

## Production

Production runs on Coolify and auto-deploys on every push to `main`; secrets
live as Coolify env vars. See [docs/deploy.md](docs/deploy.md).

## Development

```
uv venv -p 3.12 .venv
uv pip install -p .venv -e ".[dev]"
.venv/Scripts/python -m pytest     # .venv/bin/python on Linux/macOS
.venv/Scripts/python -m mypy src
```

Layout (per [ADR 0001](docs/adr/0001-agent-framework-openai-agents-sdk.md)):

- `src/agentg/runtime.py` — the channel-agnostic agent loop; imports nothing from aiogram.
- `src/agentg/channels/` — channel adapters; all Telegram-specific code lives here.
- `src/agentg/agent.py` — the Agent definition (OpenAI Agents SDK + LiteLLM).

# Agent framework comparison — gym coach agent

Research for [ticket #2 "Research: pick the agent framework"](https://github.com/ivzc07/agentg/issues/2).
**The recommendation at the bottom is not final until the owner approves it in [issue #3 "Approve the framework and stack"](https://github.com/ivzc07/agentg/issues/3).**

Product needs judged against (from the ticket): concurrent Telegram members under one coach identity · long-term per-member memory · scheduled/proactive check-ins · swappable chat channel (Telegram → WhatsApp) · multi-gym data separation · cost + self-hosting on Coolify.

Verified against primary sources (official docs + GitHub), July 2026.

## Comparison at a glance

| Criterion | Claude Agent SDK | OpenAI Agents SDK | Google ADK | LangGraph | n8n |
|---|---|---|---|---|---|
| License | SDK repo MIT, but usage governed by [Anthropic Commercial ToS](https://platform.claude.com/docs/en/api/agent-sdk/overview) | [MIT](https://github.com/openai/openai-agents-python) | [Apache-2.0](https://github.com/google/adk-python) | [MIT](https://github.com/langchain-ai/langgraph) | [Sustainable Use License](https://docs.n8n.io/sustainable-use-license/) (internal business use OK) |
| Languages | Python, TypeScript | Python, JS/TS | Python, TS, Go, Java, Kotlin | Python, JS | visual + JS snippets |
| Model choice | Claude only | Any, via [LiteLLM/Any-LLM adapters](https://openai.github.io/openai-agents-python/models/litellm/) | Any, via [LiteLLM/Ollama/vLLM connectors](https://google.github.io/adk-docs/agents/models/) | Any (bring your own) | Any |
| Conversation memory (self-hostable) | File/session-dir based, aimed at coding-agent workflows | [Built-in Sessions: SQLite, SQLAlchemy, Redis, Mongo, Dapr, encrypted](https://openai.github.io/openai-agents-python/sessions/) | Sessions API; persistent [MemoryService is in-memory or Vertex-backed](https://google.github.io/adk-docs/sessions/memory/) — self-host persistence is DIY | [Checkpointers + cross-thread Store (Postgres/Redis/Mongo)](https://langchain-ai.github.io/langgraph/concepts/persistence/) | Chat-memory nodes per workflow |
| Telegram / WhatsApp | DIY adapter | DIY adapter | DIY adapter | DIY adapter | [Native Telegram node](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.telegram/) + [WhatsApp node](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.whatsapp/) |
| Scheduling | DIY (cron/APScheduler) | DIY | DIY | DIY (cron is a managed-platform feature) | [Native Schedule Trigger](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.scheduletrigger/) |
| Multi-gym separation | App-level either way | App-level (own DB schema) | App-level | App-level (Store namespaces help) | Weak — no real data model |
| Self-host on Coolify | Yes (Docker) | Yes (plain Python service) | Yes, but persistent memory leans Google Cloud | Yes (OSS core) | Yes (official Docker image) |

## Notes per framework

**Claude Agent SDK** — gives you "the same tools, agent loop, and context management that power Claude Code" ([overview](https://platform.claude.com/docs/en/api/agent-sdk/overview)). It is built around a filesystem-and-tools harness (read files, run commands) — superb for coding agents, but a multi-tenant chat bot doesn't need that harness, and its memory model (per-session working directories, CLAUDE.md-style files) doesn't map cleanly onto thousands of members. Locked to Claude models; usage governed by Anthropic's Commercial Terms even in your own product ([license section](https://platform.claude.com/docs/en/api/agent-sdk/overview)). Ruled out on model lock-in + wrong-shaped harness, not on quality.

**OpenAI Agents SDK** — small, focused abstraction: agents, tools, guardrails, handoffs, and built-in **Sessions** for persistent conversation history with self-hostable backends (SQLite, SQLAlchemy/Postgres, Redis, Mongo, Dapr) ([sessions docs](https://openai.github.io/openai-agents-python/sessions/)). Despite the name it is **not** welded to OpenAI models — third-party adapters (LiteLLM, Any-LLM) let it drive ~any provider, including Anthropic ([third-party adapters](https://openai.github.io/openai-agents-python/models/litellm/)). MIT, ~28k stars, active daily (GitHub API, July 2026). Python + JS variants.

**Google ADK** — mature multi-language kit (Python/TS/Go/Java/Kotlin), model-agnostic via connectors ([models](https://google.github.io/adk-docs/agents/models/)). Its long-term **MemoryService** ships as in-memory (no persistence) or Vertex AI-backed ([memory docs](https://google.github.io/adk-docs/sessions/memory/)) — durable self-hosted memory means implementing the interface yourself, and the deployment story centers on Cloud Run/GKE/Agent Runtime. More framework than this product needs, with a Google Cloud pull we don't want on Coolify.

**LangGraph** — MIT, ~37k stars. Strongest persistence model of the four: checkpointers give durable per-thread state, and the cross-thread **Store** (PostgresStore etc.) with namespaces is a natural fit for per-member long-term memory ([persistence](https://langchain-ai.github.io/langgraph/concepts/persistence/), [memory](https://langchain-ai.github.io/langgraph/concepts/memory/)). The cost is the graph abstraction itself: nodes/edges/compiled state machines are aimed at complex multi-step workflows. The v1 coach is one agent + tools + memory — LangGraph's power would be carried, not used. Solid runner-up; revisit if routine generation grows into a genuine multi-step pipeline.

**n8n (low-code baseline)** — the only candidate with native Telegram, WhatsApp, and cron nodes, and a one-click Docker/Coolify deploy. License permits our internal business use ([Sustainable Use License](https://docs.n8n.io/sustainable-use-license/)). But the product's core — structured per-member memory (lifts, sessions, gaps), coach-overridable routine rules, multi-gym data model, tested business logic — lives in exactly the place n8n is weakest: it has no real data model, and complex branching logic becomes unmaintainable workflow JSON. Fine as glue (e.g. a future WhatsApp bridge), wrong as the foundation.

## Recommendation

**OpenAI Agents SDK (Python).**

Decisive reasons:

1. **Right-sized abstraction with memory included.** It's the only candidate that pairs a minimal agent loop with production-grade, *self-hostable* session persistence out of the box (SQLAlchemy/Postgres session backend) — the closest match to "one coach, many members, each with durable history" without buying a graph engine or a cloud.
2. **No lock-in on either axis.** MIT-licensed code and model-agnostic via LiteLLM — we can run Claude, GPT, Gemini, or a cheap model per task, and change later without touching the architecture. Claude Agent SDK fails this on models; ADK's persistence pulls toward Google Cloud.
3. **Channel-swap and scheduling stay ours.** No framework has a channel abstraction anyway (except n8n), so the Telegram adapter is a thin module we own — swapping to WhatsApp later touches one adapter, not the agent. Proactive check-ins are a plain scheduler invoking the same agent.

### Proposed stack

| Piece | Choice |
|---|---|
| Language | Python 3.12+ |
| Agent framework | `openai-agents` (OpenAI Agents SDK) |
| Model access | LiteLLM adapter — provider-agnostic, pick per-task models |
| Telegram | `aiogram` v3 (async, webhook-friendly), isolated in a `channels/` adapter module |
| Persistence | PostgreSQL via SQLAlchemy — domain tables (members, gyms, sessions, lifts, routines; every row carries `gym_id`) + `SQLAlchemySession` for conversation memory |
| Scheduling | APScheduler (in-process cron for check-in sweeps) |
| Deploy | Docker Compose (app + Postgres) on Coolify |

Structured coach memory (lifts, last-session dates, injuries) is deliberately **not** framework memory — it's our own Postgres tables the agent reads/writes through tools. Framework sessions only hold conversation history. This keeps the memory design (ticket #7) framework-independent.

**Approval gate:** owner sign-off required in [issue #3](https://github.com/ivzc07/agentg/issues/3) before any implementation.

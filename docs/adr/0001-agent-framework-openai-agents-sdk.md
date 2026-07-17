# ADR 0001: Agent framework — OpenAI Agents SDK (Python)

- **Status:** Accepted
- **Date:** 2026-07-17
- **Decision drivers:** [Research: pick the agent framework (#2)](https://github.com/ivzc07/agentg/issues/2) · approved by the project owner in [Approve the framework and stack (#3)](https://github.com/ivzc07/agentg/issues/3)

## Context

The gym coach agent needs: concurrent Telegram members under one coach identity, long-term per-member memory, scheduled proactive check-ins, a swappable chat channel (Telegram now, WhatsApp likely later), multi-gym data separation, and cheap self-hosting on Coolify.

Five candidates were compared against those needs (full comparison with cited sources: `docs/research/agent-framework-comparison.md`): Claude Agent SDK, OpenAI Agents SDK, Google ADK, LangGraph, and n8n.

## Decision

Build on the **OpenAI Agents SDK (Python)** with this stack:

| Piece | Choice |
|---|---|
| Language | Python 3.12+ |
| Agent framework | `openai-agents` (OpenAI Agents SDK) |
| Model access | LiteLLM adapter — provider-agnostic, per-task model choice |
| Telegram | `aiogram` v3, isolated in a `channels/` adapter module |
| Persistence | PostgreSQL via SQLAlchemy — domain tables (every row carries `gym_id`) + `SQLAlchemySession` for conversation memory |
| Scheduling | APScheduler (in-process cron for check-in sweeps) |
| Deploy | Docker Compose (app + Postgres) on Coolify |

Structured coach memory (lifts, session dates, injuries) lives in **our own Postgres tables accessed through agent tools**, not framework memory. Framework sessions hold conversation history only, keeping the memory design framework-independent.

## Rationale

1. **Right-sized abstraction with memory included.** The only candidate pairing a minimal agent loop with production-grade, self-hostable session persistence out of the box (SQLite/SQLAlchemy/Redis/Mongo backends).
2. **No lock-in on either axis.** MIT-licensed, and model-agnostic via LiteLLM — Claude, GPT, or Gemini per task, switchable without touching the architecture. The "OpenAI" name does not bind us to OpenAI models or cloud.
3. **Channel-swap and scheduling stay ours.** No code framework offers a channel abstraction anyway, so a thin Telegram adapter we own makes the WhatsApp move a one-module swap; check-ins are a plain scheduler invoking the same agent.

## Alternatives considered

- **Claude Agent SDK** — locked to Claude models; harness shaped for coding agents (filesystem, per-session dirs), not a multi-member chat product.
- **Google ADK** — durable self-hosted memory is DIY (MemoryService ships in-memory or Vertex-backed); ecosystem pulls toward Google Cloud, against the Coolify commitment.
- **LangGraph** — runner-up. Strongest persistence model, but the graph abstraction is machinery v1 (one agent + tools + memory) would carry, not use. Revisit if routine generation grows into a genuine multi-step pipeline.
- **n8n** — native Telegram/WhatsApp/cron nodes, but no real data model; core product logic would live in workflow JSON instead of tested code.

## Consequences

- Telegram specifics must stay inside the `channels/` adapter — nothing outside it may import aiogram.
- Every domain table carries `gym_id` from the first migration.
- The per-member memory design (map ticket #7) is constrained to plain Postgres tables + agent tools, independent of framework memory.
- Owner sign-off on this ADR is the gate that was cleared; implementation may reference it as settled.

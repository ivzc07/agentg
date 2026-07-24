"""Injected model backend: a queue of scripted tool calls and final messages.

Drives the OpenAI Agents SDK agent loop offline so the deterministic
behavioral layer needs no network. Each ``get_response`` pops one step.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agents.items import ModelResponse, TResponseInputItem
from agents.model_settings import ModelSettings
from agents.models.interface import Model
from agents.models.interface import ModelTracing
from agents.tool import Tool
from agents.usage import Usage
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText


@dataclass(frozen=True)
class ToolStep:
    """One model turn that invokes a single tool."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MessageStep:
    """One model turn that produces the final assistant text for the user turn."""

    text: str


Step = ToolStep | MessageStep


def tool(name: str, **args: Any) -> ToolStep:
    return ToolStep(name=name, args=args)


def message(text: str) -> MessageStep:
    return MessageStep(text=text)


class ScriptedModel(Model):
    """A Model that replays a queue of tool/message steps with no network."""

    def __init__(self) -> None:
        self._queue: deque[Step] = deque()
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, steps: Sequence[Step]) -> None:
        self._queue.extend(steps)

    def clear(self) -> None:
        self._queue.clear()

    @property
    def remaining(self) -> int:
        return len(self._queue)

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        self.calls.append(
            {
                "system_instructions": system_instructions,
                "input": input,
                "tool_names": [t.name for t in tools],
            }
        )
        if not self._queue:
            raise AssertionError(
                "ScriptedModel has no steps left — the agent loop asked for "
                "another model response than the conversation scripted"
            )
        step = self._queue.popleft()
        n = len(self.calls)
        if isinstance(step, MessageStep):
            output = [
                ResponseOutputMessage(
                    id=f"msg_{n}",
                    role="assistant",
                    status="completed",
                    type="message",
                    content=[
                        ResponseOutputText(
                            type="output_text", text=step.text, annotations=[]
                        )
                    ],
                )
            ]
        else:
            output = [
                ResponseFunctionToolCall(
                    type="function_call",
                    name=step.name,
                    call_id=f"call_{n}",
                    arguments=json.dumps(dict(step.args)),
                )
            ]
        return ModelResponse(output=output, usage=Usage(), response_id=f"resp_{n}")

    def stream_response(
        self,
        system_instructions: str | None,
        input: str | list[TResponseInputItem],
        model_settings: ModelSettings,
        tools: list[Tool],
        output_schema: Any,
        handoffs: list[Any],
        tracing: ModelTracing,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> AsyncIterator[Any]:
        raise NotImplementedError("behavioral evals use non-streaming Runner.run")

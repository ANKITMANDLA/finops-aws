"""Shared vocabulary for tool-using chat.

These types sit between three parties that all describe tools differently: MCP servers,
the four LLM providers, and the HTTP API. Keeping one neutral representation in the
middle means each provider only has to translate to and from its own wire format, and
the API can serialize a conversation without knowing which model produced it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Role = Literal["user", "assistant", "tool"]


class ToolSpec(BaseModel):
    """A tool the model may call, described in JSON Schema."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    # Where it came from: an MCP server key, or "finops" for our own scan lookups. Shown
    # in the UI so the reader can tell an AWS doc lookup from a local query.
    source: str = "finops"


class ToolCall(BaseModel):
    """The model's request to run a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Opaque per-provider material that must be replayed verbatim on the next turn.
    # Gemini 3 rejects a follow-up whose function call lost its thought signature.
    provider_data: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """What a tool returned, as the model will see it."""

    call_id: str
    name: str
    content: str
    is_error: bool = False
    duration_ms: int = 0


class ChatMessage(BaseModel):
    """One turn. Assistant turns may carry tool calls; tool turns carry their results."""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)


class ProviderTurn(BaseModel):
    """A single model response: prose, tool calls, or both."""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

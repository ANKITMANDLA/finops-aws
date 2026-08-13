"""The chat assistant: your scan on one side, AWS's own knowledge on the other.

The model is given two kinds of tools. Local ones read the stored scan, so it can name
the volume or cluster it is talking about. MCP ones reach AWS: documentation, Well-
Architected guidance, regional availability, and list pricing. Between them it can answer
"is this instance family still the right choice" without either of us guessing.

Nothing here writes anywhere. The tools read the SQLite scan and public AWS APIs, so the
worst a confused turn can do is waste tokens.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence

import anyio
from pydantic import BaseModel, Field

from finops.agent.mcp_hub import McpHub, McpServerStatus
from finops.agent.provider import LlmProvider, ProviderError, build_provider
from finops.agent.scan_tools import ScanTools
from finops.agent.types import ChatMessage, ToolCall, ToolResult, ToolSpec
from finops.config import Settings
from finops.store import ScanStore
from finops.tco import summarize_for_advisor

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the FinOps assistant for one AWS account. You have the results of a read-only \
scan of that account, and tools that reach AWS's own documentation and pricing.

How to work:
- Answer from the scan when the question is about this account: what it runs, what it \
costs, what was flagged. Use the finops_* tools to look things up rather than guessing, \
and name the resources you are talking about.
- Use the AWS documentation and pricing tools when the question needs facts you do not \
have: how a service is billed, what an instance family supports, whether something is \
available in a region, what an alternative would cost. Prefer looking it up to \
recalling it.
- Quote real numbers from tool output. Never invent a price or a saving. If you estimate, \
show the arithmetic and label it an estimate.
- If the scan lacks the data to answer, say which permission or capability is missing \
rather than guessing around it. The account summary lists what was unavailable.
- Be direct and specific. A short answer with a concrete resource id and a real figure \
beats a page of generic cost advice. Use markdown, keep tables small, and lead with the \
answer rather than your process.
- Write arithmetic as plain text, never as LaTeX or MathJax. No $$...$$, \\(...\\), \
\\text{}, \\times or \\frac: the dashboard does not render them, and the dollar signs \
collide with prices. Write "8 GiB x $0.08/GiB-month = $0.64/month" on its own line.
- You are read-only. Recommend changes, and give the command that would make them, but \
never claim to have made one."""


# Models answer cost arithmetic in LaTeX no matter how firmly the prompt asks them not to.
# The dashboard renders markdown, not math, so "$$\\text{Cost} = S \\times $0.10$$" reaches the
# reader verbatim. Flatten the handful of constructs that show up in arithmetic; leave
# anything else alone rather than mangling prose.
_FRACTION = re.compile(r"\\(?:d|t)?frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}")
_TEXT_WRAPPER = re.compile(r"\\(?:text|mathrm|mathbf|mathit|operatorname)\s*\{([^{}]*)\}")
_DELIMITERS = re.compile(r"\$\$|\\\[|\\\]|\\\(|\\\)")
_CODE_BLOCK = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
_SYMBOLS = {
    r"\times": "x",
    r"\cdot": "x",
    r"\div": "/",
    r"\approx": "~",
    r"\leq": "<=",
    r"\geq": ">=",
    r"\le": "<=",
    r"\ge": ">=",
    r"\pm": "+/-",
    r"\left": "",
    r"\right": "",
    r"\qquad": " ",
    r"\quad": " ",
    r"\,": " ",
    r"\;": " ",
    r"\:": " ",
    r"\!": "",
    r"\%": "%",
    r"\$": "$",
}
_MATH_MARKERS = re.compile(
    "|".join(re.escape(token) for token in ("$$", r"\(", r"\[", r"\text", r"\frac", *_SYMBOLS))
)


def flatten_math(text: str) -> str:
    """Rewrite LaTeX arithmetic as plain text the dashboard can display."""
    if not _MATH_MARKERS.search(text):
        return text
    # Code spans are quoted deliberately, so leave their contents untouched.
    parts = _CODE_BLOCK.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = _flatten_segment(parts[index])
    return "".join(parts)


def _flatten_segment(segment: str) -> str:
    segment = _FRACTION.sub(r"\1 / \2", segment)
    segment = _TEXT_WRAPPER.sub(r"\1", segment)
    segment = segment.replace("\\\\", "\n")
    for command in sorted(_SYMBOLS, key=len, reverse=True):
        segment = segment.replace(command, _SYMBOLS[command])
    segment = _DELIMITERS.sub("", segment)
    segment = re.sub(r"[ \t]{2,}", " ", segment)
    return "\n".join(line.rstrip() for line in segment.split("\n"))


class ToolInvocation(BaseModel):
    """One tool call, kept for the transcript so the answer can be audited."""

    id: str
    name: str
    source: str = "finops"
    arguments: dict = Field(default_factory=dict)
    duration_ms: int = 0
    is_error: bool = False
    preview: str = ""


class ChatReply(BaseModel):
    """The assistant's answer plus everything it did to get there."""

    message: str = ""
    provider: str = "none"
    model: str = ""
    tool_calls: list[ToolInvocation] = Field(default_factory=list)
    servers: list[McpServerStatus] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None


class ChatAgent:
    """Runs one question to completion, including any tool calls it needs."""

    def __init__(
        self,
        provider: LlmProvider,
        scan_tools: ScanTools,
        *,
        context: dict | None = None,
        max_tool_calls: int = 12,
        history_messages: int = 20,
    ) -> None:
        self.provider = provider
        self.context = context or {}
        self._scan_tools = scan_tools
        self._max_tool_calls = max_tool_calls
        self._history_messages = history_messages

    async def reply(self, messages: Sequence[ChatMessage], hub: McpHub | None = None) -> ChatReply:
        reply = ChatReply(
            provider=self.provider.name,
            model=self.provider.model,
            servers=list(hub.statuses) if hub else [],
        )
        if self.provider.name == "none":
            reply.error = (
                "No LLM provider is configured, so the assistant cannot answer. Set "
                "FINOPS_LLM_PROVIDER to bedrock, anthropic, openai, or gemini."
            )
            return reply
        if not self.provider.supports_tools:
            reply.error = f"The {self.provider.name} provider does not support tool use."
            return reply

        tools = self._scan_tools.tools + (hub.tools if hub else [])
        system = self._system_prompt(tools)
        transcript = list(messages)[-self._history_messages :]

        spent = 0
        while True:
            try:
                turn = await anyio.to_thread.run_sync(
                    lambda: self.provider.converse(system, transcript, tools)
                )
            except ProviderError as exc:
                logger.warning("Chat provider failed: %s", exc)
                reply.error = str(exc)
                return reply

            if not turn.wants_tools:
                reply.message = flatten_math(turn.text)
                return reply

            allowed = turn.tool_calls[: max(0, self._max_tool_calls - spent)]
            if not allowed:
                # Out of budget. Ask for an answer with what it already has rather than
                # returning the silence of an unfinished tool loop.
                reply.truncated = True
                transcript.append(ChatMessage(role="assistant", content=turn.text))
                transcript.append(
                    ChatMessage(
                        role="user",
                        content=(
                            "You have reached the tool call limit for this question. "
                            "Answer now with what you have, and say what is still unknown."
                        ),
                    )
                )
                continue

            spent += len(allowed)
            transcript.append(ChatMessage(role="assistant", content=turn.text, tool_calls=allowed))
            results = []
            for call in allowed:
                result, record = await self._run_tool(call, hub)
                results.append(result)
                reply.tool_calls.append(record)
            transcript.append(ChatMessage(role="tool", tool_results=results))

    async def _run_tool(
        self, call: ToolCall, hub: McpHub | None
    ) -> tuple[ToolResult, ToolInvocation]:
        started = time.perf_counter()
        if self._scan_tools.handles(call.name):
            source = "finops"
            content, failed = await anyio.to_thread.run_sync(
                lambda: self._scan_tools.call(call.name, call.arguments)
            )
        elif hub is not None:
            source = next((t.source for t in hub.tools if t.name == call.name), "mcp")
            content, failed = await hub.call_tool(call.name, call.arguments)
        else:
            source, content, failed = "unknown", f"No such tool: {call.name}", True

        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("chat tool %s (%s) %dms error=%s", call.name, source, elapsed, failed)
        return (
            ToolResult(
                call_id=call.id,
                name=call.name,
                content=content,
                is_error=failed,
                duration_ms=elapsed,
            ),
            ToolInvocation(
                id=call.id,
                name=call.name,
                source=source,
                arguments=call.arguments,
                duration_ms=elapsed,
                is_error=failed,
                preview=content[:280],
            ),
        )

    def _system_prompt(self, tools: Sequence[ToolSpec]) -> str:
        sections = [SYSTEM_PROMPT]
        if self.context:
            sections.append(
                "Here is the scan you are working from. Treat it as the current state of "
                "the account, and use the tools for anything it does not cover.\n"
                f"```json\n{json.dumps(self.context, indent=2, default=str)}\n```"
            )
        available = {tool.source for tool in tools}
        missing = [source for source in ("aws", "pricing") if source not in available]
        if missing:
            sections.append(
                "These tool sources are unavailable for this conversation: "
                + ", ".join(missing)
                + ". Say so if a question needs them, rather than answering from memory."
            )
        return "\n\n".join(sections)


def build_chat_agent(settings: Settings, store: ScanStore, scan_id: str, aws=None) -> ChatAgent:
    """Assemble the agent for one scan, with the account summary preloaded."""
    context: dict = {}
    report = store.get_tco(scan_id)
    meta = store.get_scan_meta(scan_id)
    if report is not None:
        findings, _ = store.query_findings(scan_id, limit=200)
        resources, _ = store.query_resources(scan_id, limit=5_000)
        context = summarize_for_advisor(report, findings, resources, max_findings=25)
    if meta is not None:
        context["account"] = {
            "account_id": meta.account_id,
            "alias": meta.account_alias,
            "scanned_at": meta.started_at.isoformat(),
            "regions": meta.regions,
            "resource_count": meta.resource_count,
            "is_demo_data": meta.dry_run,
        }
    notes = [note for note in store.get_notes(scan_id) if note.status != "ok"]
    if notes:
        context["unavailable_during_scan"] = [
            f"{note.capability}: {note.message}" for note in notes
        ]

    return ChatAgent(
        build_provider(settings, aws),
        ScanTools(store, scan_id),
        context=context,
        max_tool_calls=settings.chat_max_tool_calls,
        history_messages=settings.chat_history_messages,
    )

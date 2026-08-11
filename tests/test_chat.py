"""Tests for the tool-using chat assistant."""

from __future__ import annotations

import json

import pytest
from tests.factories import make_finding, make_resource, make_scan

from finops.agent.chat import ChatAgent, build_chat_agent
from finops.agent.mcp_hub import McpHub, McpServerStatus
from finops.agent.provider import LlmProvider, NullProvider, ProviderError
from finops.agent.scan_tools import ScanTools
from finops.agent.types import ChatMessage, ProviderTurn, ToolCall, ToolSpec
from finops.config import McpServer, Settings
from finops.store import ScanStore

# --- doubles ------------------------------------------------------------------------


class ScriptedProvider(LlmProvider):
    """Replays a fixed sequence of turns and records what it was asked."""

    name = "scripted"
    supports_tools = True

    def __init__(self, turns: list[ProviderTurn]) -> None:
        super().__init__("scripted-1")
        self._turns = list(turns)
        self.calls: list[dict] = []

    def complete(self, system: str, user: str) -> str:  # pragma: no cover - unused here
        return ""

    def converse(self, system, messages, tools=()):
        self.calls.append(
            {"system": system, "messages": list(messages), "tools": [t.name for t in tools]}
        )
        return self._turns.pop(0) if self._turns else ProviderTurn(text="done")


class FakeHub:
    """Stands in for McpHub without a server, and counts what was called."""

    def __init__(self, tools=None, result=("aws says hello", False)) -> None:
        self.tools = tools if tools is not None else [ToolSpec(name="aws_docs", source="aws")]
        self.statuses = [McpServerStatus(key="aws", connected=True, tool_count=len(self.tools))]
        self._result = result
        self.called: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.called.append((name, arguments))
        return self._result


@pytest.fixture
def store(tmp_path) -> ScanStore:
    store = ScanStore(tmp_path / "chat.db")
    store.initialize()
    store.save_scan(
        make_scan(
            resources=[make_resource("i-abc", monthly_cost=120.0)],
            findings=[make_finding(savings=75.0)],
        )
    )
    return store


@pytest.fixture
def scan_tools(store) -> ScanTools:
    return ScanTools(store, "scan-test-1")


# --- the loop -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_tool_call_is_executed_and_fed_back(scan_tools):
    provider = ScriptedProvider(
        [
            ProviderTurn(
                tool_calls=[ToolCall(id="c1", name="aws_docs", arguments={"q": "eks pricing"})]
            ),
            ProviderTurn(text="EKS control planes cost $0.10 per hour."),
        ]
    )
    hub = FakeHub()
    agent = ChatAgent(provider, scan_tools)

    reply = await agent.reply([ChatMessage(role="user", content="What does EKS cost?")], hub)

    assert reply.message == "EKS control planes cost $0.10 per hour."
    assert hub.called == [("aws_docs", {"q": "eks pricing"})]
    # The result must reach the model, otherwise it answers from memory.
    second_turn = provider.calls[1]["messages"]
    assert second_turn[-1].role == "tool"
    assert second_turn[-1].tool_results[0].content == "aws says hello"
    assert [call.name for call in reply.tool_calls] == ["aws_docs"]
    assert reply.tool_calls[0].source == "aws"


@pytest.mark.anyio
async def test_scan_tools_are_offered_alongside_mcp_tools(scan_tools):
    provider = ScriptedProvider([ProviderTurn(text="hi")])
    await ChatAgent(provider, scan_tools).reply([ChatMessage(role="user", content="hi")], FakeHub())

    offered = provider.calls[0]["tools"]
    assert "finops_search_findings" in offered
    assert "aws_docs" in offered


@pytest.mark.anyio
async def test_local_tool_calls_never_reach_the_hub(scan_tools):
    provider = ScriptedProvider(
        [
            ProviderTurn(
                tool_calls=[ToolCall(id="c1", name="finops_search_findings", arguments={})]
            ),
            ProviderTurn(text="You have one idle instance."),
        ]
    )
    hub = FakeHub()

    reply = await ChatAgent(provider, scan_tools).reply(
        [ChatMessage(role="user", content="what is wasteful?")], hub
    )

    assert hub.called == []
    assert reply.tool_calls[0].source == "finops"
    results = provider.calls[1]["messages"][-1].tool_results
    assert "Idle EC2 instance" in results[0].content


@pytest.mark.anyio
async def test_the_tool_budget_is_enforced_and_reported(scan_tools):
    # A model that only ever asks for tools must still produce an answer.
    provider = ScriptedProvider(
        [
            ProviderTurn(tool_calls=[ToolCall(id="a", name="aws_docs")]),
            ProviderTurn(tool_calls=[ToolCall(id="b", name="aws_docs")]),
            ProviderTurn(tool_calls=[ToolCall(id="c", name="aws_docs")]),
            ProviderTurn(text="Answering with what I have."),
        ]
    )
    agent = ChatAgent(provider, scan_tools, max_tool_calls=2)

    reply = await agent.reply([ChatMessage(role="user", content="go")], FakeHub())

    assert reply.truncated is True
    assert len(reply.tool_calls) == 2
    assert reply.message == "Answering with what I have."


@pytest.mark.anyio
async def test_a_failing_tool_is_reported_to_the_model_rather_than_raised(scan_tools):
    provider = ScriptedProvider(
        [
            ProviderTurn(tool_calls=[ToolCall(id="c1", name="aws_docs")]),
            ProviderTurn(text="AWS docs were unavailable, so here is what the scan shows."),
        ]
    )
    hub = FakeHub(result=("rate limited", True))

    reply = await ChatAgent(provider, scan_tools).reply(
        [ChatMessage(role="user", content="go")], hub
    )

    assert reply.error is None
    assert reply.tool_calls[0].is_error is True
    assert provider.calls[1]["messages"][-1].tool_results[0].is_error is True


@pytest.mark.anyio
async def test_provider_failure_becomes_an_error_not_an_exception(scan_tools):
    class Broken(ScriptedProvider):
        def converse(self, system, messages, tools=()):
            raise ProviderError("429 rate limited")

    reply = await ChatAgent(Broken([]), scan_tools).reply([ChatMessage(role="user", content="hi")])

    assert reply.message == ""
    assert "429" in (reply.error or "")


@pytest.mark.anyio
async def test_without_a_provider_the_assistant_says_so(scan_tools):
    reply = await ChatAgent(NullProvider(), scan_tools).reply(
        [ChatMessage(role="user", content="hi")]
    )

    assert "FINOPS_LLM_PROVIDER" in (reply.error or "")


@pytest.mark.anyio
async def test_missing_sources_are_declared_in_the_system_prompt(scan_tools):
    provider = ScriptedProvider([ProviderTurn(text="hi")])
    await ChatAgent(provider, scan_tools).reply([ChatMessage(role="user", content="hi")])

    system = provider.calls[0]["system"]
    assert "unavailable for this conversation: aws, pricing" in system


# --- grounding ----------------------------------------------------------------------


def test_the_agent_is_built_with_the_scan_already_in_context(store):
    agent = build_chat_agent(Settings(llm_provider="none", _env_file=None), store, "scan-test-1")

    context = agent.context
    assert context["account"]["account_id"] == "111122223333"
    assert context["monthly_run_rate"] == 942.0
    assert context["top_findings"][0]["title"] == "Idle EC2 instance"


# --- scan tools ---------------------------------------------------------------------


def test_search_findings_returns_ids_the_detail_tool_accepts(scan_tools):
    listing, failed = scan_tools.call("finops_search_findings", {"service": "EC2"})
    assert not failed

    first = json.loads(listing)["findings"][0]
    detail, failed = scan_tools.call("finops_get_finding", {"finding_id": first["id"]})

    assert not failed
    assert json.loads(detail)["remediation"]["cli"].startswith("aws ec2 terminate-instances")


def test_resource_lookup_needs_the_arn_and_says_so_when_it_is_wrong(scan_tools):
    listing = json.loads(scan_tools.call("finops_search_resources", {})[0])
    arn = listing["resources"][0]["arn"]

    found, failed = scan_tools.call("finops_get_resource", {"arn": arn})
    assert not failed
    assert json.loads(found)["resource_id"] == "i-abc"

    missing, failed = scan_tools.call("finops_get_resource", {"arn": "arn:aws:ec2:::nope"})
    assert not failed  # a miss is an answer, not a crash
    assert "No resource" in json.loads(missing)["error"]


def test_a_bad_argument_is_returned_as_an_error_string(scan_tools):
    content, failed = scan_tools.call("finops_nonexistent", {})
    assert failed
    assert "No such tool" in content


def test_row_limits_are_clamped(scan_tools):
    payload = json.loads(scan_tools.call("finops_search_resources", {"limit": 5_000})[0])
    assert payload["returned"] <= 50


# --- mcp hub ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_unreachable_server_is_a_note_not_a_failure():
    hub = McpHub(
        [McpServer(key="broken", transport="http", url="http://127.0.0.1:9/mcp")],
        startup_timeout=2.0,
    )
    async with hub:
        assert hub.tools == []
        assert hub.statuses[0].connected is False
        assert hub.statuses[0].error

        content, failed = await hub.call_tool("anything", {})
        assert failed
        assert "No such tool" in content


@pytest.mark.anyio
async def test_a_misconfigured_server_is_rejected_before_dialling():
    hub = McpHub([McpServer(key="empty", transport="http", url=None)])
    async with hub:
        assert "no url" in (hub.statuses[0].error or "")

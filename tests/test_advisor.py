from __future__ import annotations

import json
from datetime import date

import pytest
from tests.factories import make_finding, make_resource

from finops.agent.advisor import Advisor, build_advisor
from finops.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from finops.agent.provider import (
    AnthropicProvider,
    BedrockProvider,
    GeminiProvider,
    LlmProvider,
    NullProvider,
    OpenAiProvider,
    ProviderError,
    ProviderUnavailable,
    build_provider,
)
from finops.config import Settings
from finops.model import CapabilityNote, TcoReport

VALID_RESPONSE = {
    "executive_summary": "EC2 dominates spend and three NAT Gateways duplicate egress.",
    "quick_wins": ["Release the two unassociated Elastic IPs"],
    "recommendations": [
        {
            "title": "Consolidate egress through a shared NAT Gateway",
            "summary": "Replace three per-AZ NAT Gateways with one plus VPC endpoints.",
            "rationale": "NAT data processing is 8% of the bill and traffic is low.",
            "affected_services": ["VPC", "EC2"],
            "estimated_monthly_savings": 240.5,
            "implementation_effort": "medium",
            "risk": "medium",
            "steps": ["Add S3 and ECR gateway endpoints", "Reroute private subnets"],
            "related_finding_ids": ["known-id"],
            "tradeoffs": "Loses per-AZ egress isolation.",
        }
    ],
    "caveats": ["Resource-level cost data was unavailable."],
}


class ScriptedProvider(LlmProvider):
    name = "scripted"

    def __init__(self, response: str | Exception):
        super().__init__(model="test-model")
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def report() -> TcoReport:
    from finops.model import BreakdownItem

    return TcoReport(
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        monthly_run_rate=10_000.0,
        identified_monthly_savings=1_500.0,
        savings_percent=15.0,
        by_service=[BreakdownItem(key="Amazon EC2", amount=6000.0, share=60.0)],
        by_category=[BreakdownItem(key="Idle resources", amount=900.0)],
    )


def findings_with_known_id():
    finding = make_finding("idle_ec2", savings=900.0, resource_arn="arn:1")
    finding.id = "known-id"
    return [finding]


# --- provider selection -------------------------------------------------------------


def test_provider_selection_follows_config():
    class FakeAws:
        def client(self, service, region=None):
            return f"{service}@{region}"

    bedrock = build_provider(
        Settings(llm_provider="bedrock", bedrock_region="us-west-2"), FakeAws()
    )
    assert isinstance(bedrock, BedrockProvider)

    anthropic = build_provider(Settings(llm_provider="anthropic", anthropic_api_key="sk-x"))
    assert isinstance(anthropic, AnthropicProvider)

    openai = build_provider(Settings(llm_provider="openai", openai_api_key="sk-y"))
    assert isinstance(openai, OpenAiProvider)

    gemini = build_provider(Settings(llm_provider="gemini", gemini_api_key="key-z"))
    assert isinstance(gemini, GeminiProvider)

    assert isinstance(build_provider(Settings(llm_provider="none")), NullProvider)


def test_missing_credentials_disable_the_advisor_instead_of_crashing():
    # A forgotten API key should degrade to deterministic advice, not kill the scan.
    assert isinstance(build_provider(Settings(llm_provider="anthropic")), NullProvider)
    assert isinstance(build_provider(Settings(llm_provider="openai")), NullProvider)
    assert isinstance(build_provider(Settings(llm_provider="gemini")), NullProvider)
    assert isinstance(build_provider(Settings(llm_provider="bedrock"), None), NullProvider)


def test_bedrock_uses_the_converse_api_and_joins_text_blocks():
    class FakeBedrock:
        def __init__(self):
            self.kwargs = None

        def converse(self, **kwargs):
            self.kwargs = kwargs
            return {
                "output": {"message": {"content": [{"text": "part one "}, {"text": "part two"}]}}
            }

    client = FakeBedrock()
    provider = BedrockProvider(client, "model-x", max_tokens=1024, temperature=0.1)

    assert provider.complete("sys", "user") == "part one part two"
    assert client.kwargs["modelId"] == "model-x"
    assert client.kwargs["system"] == [{"text": "sys"}]
    assert client.kwargs["inferenceConfig"] == {"maxTokens": 1024, "temperature": 0.1}


def test_bedrock_access_denied_is_reported_as_a_setup_problem():
    from botocore.exceptions import ClientError

    class DeniedBedrock:
        def converse(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "Converse"
            )

    provider = BedrockProvider(DeniedBedrock(), "model-x")
    with pytest.raises(ProviderUnavailable, match="Bedrock access denied"):
        provider.complete("sys", "user")


class FakeHttp:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.requests = status, body or {}, []

    def post(self, url, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        outer = self

        class Response:
            status_code = outer.status

            def json(self):
                return outer.body

            text = "error text"

        return Response()


def test_anthropic_sends_the_versioned_header_and_reads_text_blocks():
    http = FakeHttp(body={"content": [{"type": "text", "text": "hello"}]})
    provider = AnthropicProvider("sk-test", "claude-x", http_client=http)

    assert provider.complete("sys", "user") == "hello"
    request = http.requests[0]
    assert request["headers"]["x-api-key"] == "sk-test"
    assert request["headers"]["anthropic-version"] == "2023-06-01"
    assert request["json"]["system"] == "sys"


def test_openai_posts_to_the_configured_base_url():
    http = FakeHttp(body={"choices": [{"message": {"content": "hello"}}]})
    provider = OpenAiProvider(
        "sk-test", "gpt-x", base_url="https://proxy.local/v1/", http_client=http
    )

    assert provider.complete("sys", "user") == "hello"
    assert http.requests[0]["url"] == "https://proxy.local/v1/chat/completions"
    assert http.requests[0]["json"]["messages"][0]["role"] == "system"


def test_gemini_sends_the_api_key_header_and_joins_parts():
    http = FakeHttp(
        body={"candidates": [{"content": {"parts": [{"text": "hel"}, {"text": "lo"}]}}]}
    )
    provider = GeminiProvider(
        "key-test", "gemini-x", base_url="https://gl.local/v1beta", http_client=http
    )

    assert provider.complete("sys", "user") == "hello"
    request = http.requests[0]
    assert request["url"] == "https://gl.local/v1beta/models/gemini-x:generateContent"
    assert request["headers"]["x-goog-api-key"] == "key-test"
    assert request["json"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert request["json"]["contents"][0]["parts"][0]["text"] == "user"


def test_gemini_caps_thinking_on_models_that_support_it():
    """Thought tokens come out of the output budget, so an uncapped model truncates."""
    http = FakeHttp(body={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    provider = GeminiProvider(
        "key", "gemini-3.6-flash", base_url="https://gl.local/v1beta", http_client=http
    )
    provider.complete("sys", "user")

    thinking = http.requests[0]["json"]["generationConfig"]["thinkingConfig"]
    assert thinking == {"thinkingLevel": "LOW"}


def test_gemini_omits_thinking_config_for_models_that_reject_it():
    # Gemini 2.x answers a thinkingLevel with a 400, so it must not be sent.
    http = FakeHttp(body={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    provider = GeminiProvider(
        "key", "gemini-2.5-pro", base_url="https://gl.local/v1beta", http_client=http
    )
    provider.complete("sys", "user")

    assert "thinkingConfig" not in http.requests[0]["json"]["generationConfig"]

    default = GeminiProvider(
        "key",
        "gemini-3.6-flash",
        base_url="https://gl.local/v1beta",
        thinking_level="default",
        http_client=http,
    )
    default.complete("sys", "user")
    assert "thinkingConfig" not in http.requests[1]["json"]["generationConfig"]


def test_gemini_reports_a_blocked_prompt_rather_than_returning_nothing():
    http = FakeHttp(body={"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})
    provider = GeminiProvider(
        "key", "gemini-x", base_url="https://gl.local/v1beta", http_client=http
    )

    with pytest.raises(ProviderError, match="SAFETY"):
        provider.complete("sys", "user")


@pytest.mark.parametrize("parts", [[], [{"text": '{"executive_summary": "half a doc'}]])
def test_gemini_truncation_names_the_token_limit(parts):
    """Truncated JSON must not reach the parser, which would blame the model's formatting."""
    http = FakeHttp(
        body={"candidates": [{"content": {"parts": parts}, "finishReason": "MAX_TOKENS"}]}
    )
    provider = GeminiProvider(
        "key", "gemini-x", base_url="https://gl.local/v1beta", http_client=http
    )

    with pytest.raises(ProviderError, match="FINOPS_LLM_MAX_OUTPUT_TOKENS"):
        provider.complete("sys", "user")


def test_http_errors_surface_as_provider_errors():
    http = FakeHttp(status=429, body={"error": "rate limited"})
    provider = AnthropicProvider("sk-test", "claude-x", http_client=http)
    with pytest.raises(ProviderError, match="429"):
        provider.complete("sys", "user")


# --- prompt -------------------------------------------------------------------------


def test_prompt_carries_the_summary_and_the_capability_gaps():
    prompt = build_user_prompt(
        {"monthly_run_rate": 10.0}, ["Trusted Advisor: needs Business support"]
    )
    assert '"monthly_run_rate": 10.0' in prompt
    assert "Trusted Advisor: needs Business support" in prompt
    assert "JSON" in SYSTEM_PROMPT


# --- advisor ------------------------------------------------------------------------


def test_valid_response_becomes_structured_recommendations():
    provider = ScriptedProvider(json.dumps(VALID_RESPONSE))
    advice = Advisor(provider).advise(report(), findings_with_known_id(), [make_resource("i-1")])

    assert advice.provider == "scripted"
    assert advice.model == "test-model"
    assert advice.error is None
    recommendation = advice.recommendations[0]
    assert recommendation.title.startswith("Consolidate egress")
    assert recommendation.estimated_monthly_savings == 240.5
    assert recommendation.related_finding_ids == ["known-id"]


def test_markdown_fenced_json_is_still_parsed():
    provider = ScriptedProvider("```json\n" + json.dumps(VALID_RESPONSE) + "\n```")
    advice = Advisor(provider).advise(report(), findings_with_known_id(), [])
    assert advice.recommendations


def test_json_wrapped_in_chatter_is_still_parsed():
    provider = ScriptedProvider(
        "Sure! Here you go:\n" + json.dumps(VALID_RESPONSE) + "\nHope that helps."
    )
    advice = Advisor(provider).advise(report(), findings_with_known_id(), [])
    assert advice.executive_summary.startswith("EC2 dominates")


def test_invented_finding_ids_are_dropped():
    payload = json.loads(json.dumps(VALID_RESPONSE))
    payload["recommendations"][0]["related_finding_ids"] = ["known-id", "made-up"]
    advice = Advisor(ScriptedProvider(json.dumps(payload))).advise(
        report(), findings_with_known_id(), []
    )
    assert advice.recommendations[0].related_finding_ids == ["known-id"]


def test_junk_values_are_coerced_rather_than_rejected():
    payload = json.loads(json.dumps(VALID_RESPONSE))
    payload["recommendations"][0]["implementation_effort"] = "trivial"
    payload["recommendations"][0]["estimated_monthly_savings"] = "$1,200.50"
    payload["quick_wins"] = "a single string, not a list"

    advice = Advisor(ScriptedProvider(json.dumps(payload))).advise(
        report(), findings_with_known_id(), []
    )

    assert advice.recommendations[0].implementation_effort == "medium"
    assert advice.recommendations[0].estimated_monthly_savings == 1200.5
    assert advice.quick_wins == ["a single string, not a list"]


def test_zero_savings_is_recorded_as_unquantified():
    payload = json.loads(json.dumps(VALID_RESPONSE))
    payload["recommendations"][0]["estimated_monthly_savings"] = 0
    advice = Advisor(ScriptedProvider(json.dumps(payload))).advise(report(), [], [])
    assert advice.recommendations[0].estimated_monthly_savings is None


def test_recommendations_are_capped():
    payload = {"recommendations": [dict(VALID_RESPONSE["recommendations"][0]) for _ in range(20)]}
    advice = Advisor(ScriptedProvider(json.dumps(payload))).advise(report(), [], [])
    assert len(advice.recommendations) == 6


def test_unparseable_output_falls_back_without_losing_the_scan():
    advice = Advisor(ScriptedProvider("I'm sorry, I can't help with that.")).advise(
        report(), findings_with_known_id(), []
    )

    assert advice.provider == "none"
    assert "Could not parse" in advice.error
    # The deterministic summary still quantifies the opportunity.
    assert "$10,000" in advice.executive_summary
    assert "$1,500" in advice.executive_summary


def test_provider_failure_falls_back_and_records_why():
    advice = Advisor(ScriptedProvider(ProviderError("throttled"))).advise(
        report(), findings_with_known_id(), []
    )
    assert advice.error == "throttled"
    assert any("throttled" in caveat for caveat in advice.caveats)


def test_no_provider_still_produces_quick_wins_from_the_findings():
    easy = make_finding("gp2_to_gp3", savings=50.0, resource_arn="arn:1")
    easy.implementation_effort = "low"
    hard = make_finding("graviton", savings=500.0, resource_arn="arn:2")
    hard.implementation_effort = "high"

    advice = Advisor(NullProvider()).advise(report(), [easy, hard], [])

    assert advice.provider == "none"
    assert advice.error is None
    assert len(advice.quick_wins) == 1
    assert "gp2" in advice.quick_wins[0].lower() or "$50" in advice.quick_wins[0]
    assert any("FINOPS_LLM_PROVIDER" in caveat for caveat in advice.caveats)


def test_capability_gaps_are_passed_to_the_model():
    provider = ScriptedProvider(json.dumps(VALID_RESPONSE))
    Advisor(provider).advise(
        report(),
        [],
        [],
        notes=[
            CapabilityNote(
                capability="trusted-advisor", status="denied", message="needs Business support"
            ),
            CapabilityNote(capability="ec2", status="ok", message="fine"),
        ],
    )

    _, user_prompt = provider.calls[0]
    assert "needs Business support" in user_prompt
    assert "fine" not in user_prompt


def test_build_advisor_wires_the_configured_provider():
    advisor = build_advisor(Settings(llm_provider="none"))
    assert isinstance(advisor.provider, NullProvider)

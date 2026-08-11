"""Pluggable LLM providers.

The advisor only needs one operation - send a system prompt plus a user prompt, get
text back - so the interface is deliberately tiny. Bedrock is the default because it
reuses the same AWS credentials the rest of the agent already has; Anthropic, OpenAI,
and Gemini are drop-in alternatives selected with ``FINOPS_LLM_PROVIDER``.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence

from finops.agent.types import ChatMessage, ProviderTurn, ToolCall, ToolSpec
from finops.config import Settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 120.0


class ProviderError(RuntimeError):
    """The provider was reachable but the call failed."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured, e.g. a missing API key."""


class LlmProvider(ABC):
    """Text completion, plus optional tool use, shared by every backend."""

    name: str = "none"
    supports_tools: bool = False

    def __init__(self, model: str, *, max_tokens: int = 4096, temperature: float = 0.2) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return the model's text response, or raise ``ProviderError``."""

    def converse(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderTurn:
        """Continue a conversation, optionally calling tools.

        The caller runs any returned tool calls and sends the results back as another
        message, so this stays a single request rather than owning the loop.
        """
        raise ProviderUnavailable(f"The {self.name} provider does not support conversations.")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} model={self.model}>"


class NullProvider(LlmProvider):
    """Used when no LLM is configured. Every scan still produces advice, just
    deterministic advice assembled from the findings themselves."""

    name = "none"

    def __init__(self) -> None:
        super().__init__(model="")

    def complete(self, system: str, user: str) -> str:
        raise ProviderUnavailable(
            "No LLM provider configured. Set FINOPS_LLM_PROVIDER to bedrock, anthropic, "
            "openai, or gemini."
        )


class BedrockProvider(LlmProvider):
    """Amazon Bedrock via the Converse API, which is uniform across model families."""

    name = "bedrock"
    supports_tools = True

    def __init__(self, client, model: str, **kwargs) -> None:
        super().__init__(model, **kwargs)
        self._client = client

    def complete(self, system: str, user: str) -> str:
        response = self._call([{"role": "user", "content": [{"text": user}]}], system, ())
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(block.get("text", "") for block in blocks).strip()

    def converse(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderTurn:
        response = self._call([_bedrock_message(message) for message in messages], system, tools)
        blocks = response.get("output", {}).get("message", {}).get("content", [])

        text_parts, calls = [], []
        for block in blocks:
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                use = block["toolUse"]
                calls.append(
                    ToolCall(
                        id=use.get("toolUseId", f"call-{len(calls)}"),
                        name=use.get("name", ""),
                        arguments=use.get("input") or {},
                    )
                )
        return ProviderTurn(text="".join(text_parts).strip(), tool_calls=calls)

    def _call(self, messages: list[dict], system: str, tools: Sequence[ToolSpec]) -> dict:
        request = {
            "modelId": self.model,
            "system": [{"text": system}],
            "messages": messages,
            "inferenceConfig": {"maxTokens": self.max_tokens, "temperature": self.temperature},
        }
        if tools:
            request["toolConfig"] = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": {"json": tool.input_schema},
                        }
                    }
                    for tool in tools
                ]
            }
        try:
            return self._client.converse(**request)
        except Exception as exc:  # boto3 raises ClientError and friends
            raise _translate_bedrock_error(exc) from exc


def _bedrock_message(message: ChatMessage) -> dict:
    if message.role == "tool":
        # Bedrock has no tool role: results are user content blocks.
        return {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": result.call_id,
                        "content": [{"text": result.content}],
                        "status": "error" if result.is_error else "success",
                    }
                }
                for result in message.tool_results
            ],
        }

    content: list[dict] = []
    if message.content:
        content.append({"text": message.content})
    for call in message.tool_calls:
        content.append(
            {"toolUse": {"toolUseId": call.id, "name": call.name, "input": call.arguments}}
        )
    return {"role": message.role, "content": content or [{"text": ""}]}


class AnthropicProvider(LlmProvider):
    """Anthropic's Messages API."""

    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"
    api_version = "2023-06-01"
    supports_tools = True

    def __init__(self, api_key: str, model: str, *, http_client=None, **kwargs) -> None:
        super().__init__(model, **kwargs)
        self._api_key = api_key
        self._http = http_client

    def complete(self, system: str, user: str) -> str:
        data = self._call(system, [{"role": "user", "content": user}], ())
        blocks = data.get("content", [])
        return "".join(
            block.get("text", "") for block in blocks if block.get("type", "text") == "text"
        ).strip()

    def converse(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderTurn:
        data = self._call(system, [_anthropic_message(message) for message in messages], tools)

        text_parts, calls = [], []
        for block in data.get("content", []):
            kind = block.get("type", "text")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.get("id", f"call-{len(calls)}"),
                        name=block.get("name", ""),
                        arguments=block.get("input") or {},
                    )
                )
        return ProviderTurn(text="".join(text_parts).strip(), tool_calls=calls)

    def _call(self, system: str, messages: list[dict], tools: Sequence[ToolSpec]) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": messages,
        }
        if tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in tools
            ]
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }
        return _post_json(self._http, self.endpoint, payload, headers)


def _anthropic_message(message: ChatMessage) -> dict:
    if message.role == "tool":
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": result.call_id,
                    "content": result.content,
                    "is_error": result.is_error,
                }
                for result in message.tool_results
            ],
        }

    if not message.tool_calls:
        return {"role": message.role, "content": message.content}

    content: list[dict] = []
    if message.content:
        content.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        content.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return {"role": message.role, "content": content}


class OpenAiProvider(LlmProvider):
    """OpenAI chat completions, and by extension any API-compatible endpoint."""

    name = "openai"
    supports_tools = True

    def __init__(
        self, api_key: str, model: str, *, base_url: str, http_client=None, **kwargs
    ) -> None:
        super().__init__(model, **kwargs)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http_client

    def complete(self, system: str, user: str) -> str:
        message = self._call(
            [{"role": "system", "content": system}, {"role": "user", "content": user}], ()
        )
        return (message.get("content") or "").strip()

    def converse(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderTurn:
        wire: list[dict] = [{"role": "system", "content": system}]
        for message in messages:
            wire.extend(_openai_messages(message))
        reply = self._call(wire, tools)

        calls = []
        for index, call in enumerate(reply.get("tool_calls") or []):
            function = call.get("function") or {}
            calls.append(
                ToolCall(
                    id=call.get("id", f"call-{index}"),
                    name=function.get("name", ""),
                    arguments=_loads_arguments(function.get("arguments")),
                )
            )
        return ProviderTurn(text=(reply.get("content") or "").strip(), tool_calls=calls)

    def _call(self, messages: list[dict], tools: Sequence[ToolSpec]) -> dict:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": messages,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        data = _post_json(self._http, f"{self._base_url}/chat/completions", payload, headers)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("OpenAI returned no choices")
        return choices[0].get("message", {})


def _openai_messages(message: ChatMessage) -> list[dict]:
    """OpenAI wants one message per tool result, so this can fan out."""
    if message.role == "tool":
        return [
            {"role": "tool", "tool_call_id": result.call_id, "content": result.content}
            for result in message.tool_results
        ]

    wire: dict = {"role": message.role, "content": message.content or None}
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    return [wire]


def _loads_arguments(raw) -> dict:
    """Tool arguments arrive as a JSON string, and a truncated one is not fatal."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class GeminiProvider(LlmProvider):
    """Google's Gemini API (``generateContent`` on generativelanguage.googleapis.com).

    Authenticated with an API key from AI Studio rather than gcloud credentials, which
    keeps it usable from a laptop with no Google Cloud project wired up.
    """

    name = "gemini"
    supports_tools = True

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str,
        thinking_level: str = "low",
        http_client=None,
        **kwargs,
    ) -> None:
        super().__init__(model, **kwargs)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._thinking_level = thinking_level
        self._http = http_client

    def complete(self, system: str, user: str) -> str:
        candidate = self._call(
            system,
            [{"role": "user", "parts": [{"text": user}]}],
            (),
            # The advisor asks for JSON, and saying so here stops Gemini wrapping the
            # object in prose or a markdown fence. Not valid alongside tools.
            response_mime_type="application/json",
        )
        text = _gemini_text(candidate)
        if not text:
            raise ProviderError(
                f"Gemini returned an empty response ({candidate.get('finishReason')})"
            )
        return text

    def converse(
        self,
        system: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
    ) -> ProviderTurn:
        candidate = self._call(system, [_gemini_content(m) for m in messages], tools)

        calls = []
        for index, part in enumerate((candidate.get("content") or {}).get("parts", [])):
            function_call = part.get("functionCall")
            if not function_call:
                continue
            calls.append(
                ToolCall(
                    id=function_call.get("id") or f"call-{index}",
                    name=function_call.get("name", ""),
                    arguments=function_call.get("args") or {},
                    # Gemini 3 rejects a follow-up turn whose function call came back
                    # without the signature it issued: MISSING_THOUGHT_SIGNATURE.
                    provider_data={key: part[key] for key in ("thoughtSignature",) if key in part},
                )
            )
        return ProviderTurn(text=_gemini_text(candidate), tool_calls=calls)

    def _call(
        self,
        system: str,
        contents: list[dict],
        tools: Sequence[ToolSpec],
        *,
        response_mime_type: str | None = None,
    ) -> dict:
        config: dict = {"temperature": self.temperature, "maxOutputTokens": self.max_tokens}
        if response_mime_type:
            config["responseMimeType"] = response_mime_type
        level = self._resolved_thinking_level()
        if level:
            # Thought tokens are billed against maxOutputTokens, and an unbounded reasoning
            # budget on a long inventory truncates the answer before it closes.
            config["thinkingConfig"] = {"thinkingLevel": level}

        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": config,
        }
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            # Full JSON Schema, rather than the OpenAPI subset `parameters`
                            # takes, so MCP schemas pass through unmodified.
                            "parametersJsonSchema": tool.input_schema,
                        }
                        for tool in tools
                    ]
                }
            ]

        headers = {"x-goog-api-key": self._api_key, "content-type": "application/json"}
        url = f"{self._base_url}/models/{self.model}:generateContent"
        data = _post_json(self._http, url, payload, headers)

        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise ProviderError(f"Gemini blocked the prompt ({blocked})")

        candidates = data.get("candidates") or []
        if not candidates:
            raise ProviderError("Gemini returned no candidates")
        candidate = candidates[0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            # Truncated output would otherwise reach the JSON parser and be reported as a
            # malformed response, which sends you looking in the wrong place.
            raise ProviderError(
                "Gemini hit the output token limit, so its answer is truncated. Raise "
                "FINOPS_LLM_MAX_OUTPUT_TOKENS or lower FINOPS_GEMINI_THINKING_LEVEL."
            )
        return candidate

    def _resolved_thinking_level(self) -> str | None:
        """The API enum to send, or None to leave the model's default alone.

        Thinking levels arrived with Gemini 3; sending one to an older model is a 400.
        """
        level = (self._thinking_level or "").strip().upper()
        if not level or level == "DEFAULT":
            return None
        family = re.match(r"gemini-(\d+)", self.model)
        if family and int(family.group(1)) < 3:
            return None
        return level


def _gemini_text(candidate: dict) -> str:
    """Visible prose only: thought parts carry text too, and are not the answer."""
    return "".join(
        part.get("text", "")
        for part in (candidate.get("content") or {}).get("parts", [])
        if not part.get("thought")
    ).strip()


def _gemini_content(message: ChatMessage) -> dict:
    if message.role == "tool":
        # Contents only take the user and model roles, so results ride back as user parts.
        return {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": result.name,
                        "response": (
                            {"error": result.content}
                            if result.is_error
                            else {"output": result.content}
                        ),
                    }
                }
                for result in message.tool_results
            ],
        }

    parts: list[dict] = []
    if message.content:
        parts.append({"text": message.content})
    for call in message.tool_calls:
        parts.append({"functionCall": {"name": call.name, "args": call.arguments}})
        parts[-1].update(call.provider_data)
    return {"role": "model" if message.role == "assistant" else "user", "parts": parts}


def build_provider(settings: Settings, aws=None, *, http_client=None) -> LlmProvider:
    """Instantiate the configured provider, falling back to ``NullProvider``.

    A missing API key is a configuration gap, not a crash: the scan should still finish
    and the UI should still show deterministic findings.
    """
    kwargs = {
        "max_tokens": settings.llm_max_output_tokens,
        "temperature": settings.llm_temperature,
    }
    choice = settings.llm_provider

    if choice == "bedrock":
        if aws is None:
            logger.warning("Bedrock advisor needs an AWS context; advice is disabled.")
            return NullProvider()
        client = aws.client("bedrock-runtime", region=settings.resolved_bedrock_region)
        return BedrockProvider(client, settings.bedrock_model_id, **kwargs)

    if choice == "anthropic":
        if not settings.anthropic_api_key:
            logger.warning("FINOPS_ANTHROPIC_API_KEY is not set; advice is disabled.")
            return NullProvider()
        return AnthropicProvider(
            settings.anthropic_api_key,
            settings.anthropic_model,
            http_client=http_client,
            **kwargs,
        )

    if choice == "openai":
        if not settings.openai_api_key:
            logger.warning("FINOPS_OPENAI_API_KEY is not set; advice is disabled.")
            return NullProvider()
        return OpenAiProvider(
            settings.openai_api_key,
            settings.openai_model,
            base_url=settings.openai_base_url,
            http_client=http_client,
            **kwargs,
        )

    if choice == "gemini":
        if not settings.gemini_api_key:
            logger.warning("FINOPS_GEMINI_API_KEY is not set; advice is disabled.")
            return NullProvider()
        return GeminiProvider(
            settings.gemini_api_key,
            settings.gemini_model,
            base_url=settings.gemini_base_url,
            thinking_level=settings.gemini_thinking_level,
            http_client=http_client,
            **kwargs,
        )

    return NullProvider()


def _post_json(http_client, url: str, payload: dict, headers: dict) -> dict:
    client = http_client
    if client is None:
        import httpx  # imported lazily so Bedrock-only users need no HTTP stack

        client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        close_after = True
    else:
        close_after = False

    try:
        response = client.post(url, json=payload, headers=headers)
        if response.status_code >= 400:
            raise ProviderError(f"HTTP {response.status_code}: {_error_body(response)}")
        return response.json()
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(str(exc)) from exc
    finally:
        if close_after:
            client.close()


def _error_body(response) -> str:
    try:
        return json.dumps(response.json())[:500]
    except Exception:
        return str(getattr(response, "text", ""))[:500]


def _translate_bedrock_error(exc: Exception) -> ProviderError:
    code = (
        getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if hasattr(exc, "response")
        else ""
    )
    if code in {"AccessDeniedException", "UnrecognizedClientException"}:
        return ProviderUnavailable(
            f"Bedrock access denied ({code}). Request model access in the Bedrock console "
            "or grant bedrock:InvokeModel."
        )
    if code == "ValidationException":
        return ProviderUnavailable(f"Bedrock rejected the request: {exc}")
    return ProviderError(str(exc))

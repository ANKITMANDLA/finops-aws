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

from finops.config import Settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 120.0


class ProviderError(RuntimeError):
    """The provider was reachable but the call failed."""


class ProviderUnavailable(ProviderError):
    """The provider is not configured, e.g. a missing API key."""


class LlmProvider(ABC):
    """Minimal text-completion interface shared by every backend."""

    name: str = "none"

    def __init__(self, model: str, *, max_tokens: int = 4096, temperature: float = 0.2) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """Return the model's text response, or raise ``ProviderError``."""

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

    def __init__(self, client, model: str, **kwargs) -> None:
        super().__init__(model, **kwargs)
        self._client = client

    def complete(self, system: str, user: str) -> str:
        try:
            response = self._client.converse(
                modelId=self.model,
                system=[{"text": system}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={
                    "maxTokens": self.max_tokens,
                    "temperature": self.temperature,
                },
            )
        except Exception as exc:  # boto3 raises ClientError and friends
            raise _translate_bedrock_error(exc) from exc
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(block.get("text", "") for block in blocks).strip()


class AnthropicProvider(LlmProvider):
    """Anthropic's Messages API."""

    name = "anthropic"
    endpoint = "https://api.anthropic.com/v1/messages"
    api_version = "2023-06-01"

    def __init__(self, api_key: str, model: str, *, http_client=None, **kwargs) -> None:
        super().__init__(model, **kwargs)
        self._api_key = api_key
        self._http = http_client

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }
        data = _post_json(self._http, self.endpoint, payload, headers)
        blocks = data.get("content", [])
        return "".join(
            block.get("text", "") for block in blocks if block.get("type", "text") == "text"
        ).strip()


class OpenAiProvider(LlmProvider):
    """OpenAI chat completions, and by extension any API-compatible endpoint."""

    name = "openai"

    def __init__(
        self, api_key: str, model: str, *, base_url: str, http_client=None, **kwargs
    ) -> None:
        super().__init__(model, **kwargs)
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http_client

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }
        data = _post_json(self._http, f"{self._base_url}/chat/completions", payload, headers)
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("OpenAI returned no choices")
        return (choices[0].get("message", {}).get("content") or "").strip()


class GeminiProvider(LlmProvider):
    """Google's Gemini API (``generateContent`` on generativelanguage.googleapis.com).

    Authenticated with an API key from AI Studio rather than gcloud credentials, which
    keeps it usable from a laptop with no Google Cloud project wired up.
    """

    name = "gemini"

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
        config = {
            "temperature": self.temperature,
            "maxOutputTokens": self.max_tokens,
            # The advisor asks for JSON, and saying so here stops Gemini wrapping the
            # object in prose or a markdown fence.
            "responseMimeType": "application/json",
        }
        level = self._resolved_thinking_level()
        if level:
            # Thought tokens are billed against maxOutputTokens, and an unbounded reasoning
            # budget on a long inventory truncates the JSON before it closes. The advisor's
            # input is already ranked and summarized, so it needs very little deliberation.
            config["thinkingConfig"] = {"thinkingLevel": level}

        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
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
        reason = candidate.get("finishReason") or "unknown"
        if reason == "MAX_TOKENS":
            # Truncated JSON would otherwise reach the parser and be reported as a malformed
            # response, which sends you looking in the wrong place.
            raise ProviderError(
                "Gemini hit the output token limit, so its answer is truncated. Raise "
                "FINOPS_LLM_MAX_OUTPUT_TOKENS or lower FINOPS_GEMINI_THINKING_LEVEL."
            )
        text = "".join(
            part.get("text", "") for part in (candidate.get("content") or {}).get("parts", [])
        ).strip()
        if not text:
            raise ProviderError(f"Gemini returned an empty response ({reason})")
        return text

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

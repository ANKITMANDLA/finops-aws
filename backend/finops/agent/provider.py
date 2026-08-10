"""Pluggable LLM providers.

The advisor only needs one operation - send a system prompt plus a user prompt, get
text back - so the interface is deliberately tiny. Bedrock is the default because it
reuses the same AWS credentials the rest of the agent already has; Anthropic and OpenAI
are drop-in alternatives selected with ``FINOPS_LLM_PROVIDER``.
"""

from __future__ import annotations

import json
import logging
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
            "No LLM provider configured. Set FINOPS_LLM_PROVIDER to bedrock, anthropic, or openai."
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

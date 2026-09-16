"""Chat-completions client shared by Microsoft Foundry and OpenRouter."""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from webagents.providers.base import ProviderCallResult


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
        )

    async def invoke(self, request_bytes: bytes) -> ProviderCallResult:
        request: dict[str, Any] = json.loads(request_bytes)
        response = await self._client.chat.completions.create(**request)
        if not response.choices:
            raise RuntimeError(f"{self.provider} returned no chat-completion choices")
        choice = response.choices[0]
        usage = response.usage
        return ProviderCallResult(
            text=choice.message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            metadata={
                "provider": self.provider,
                "model": response.model,
                "response_id": response.id,
                "finish_reason": choice.finish_reason,
            },
        )

    async def close(self) -> None:
        await self._client.close()

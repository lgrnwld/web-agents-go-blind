"""Provider boundary shared by non-framework-owned agent loops."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from webagents.providers.config import resolve_provider_settings


@dataclass(frozen=True, slots=True)
class ProviderCallResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    metadata: dict[str, Any] | None = None


class ProviderClient(Protocol):
    provider: str
    model: str

    async def invoke(self, request_bytes: bytes) -> ProviderCallResult: ...


def resolve_provider(model_id: str, *, environ: Mapping[str, str] | None = None) -> ProviderClient:
    """Resolve an OpenAI-compatible provider without importing Browser Use."""

    settings = resolve_provider_settings(model_id, environ=environ)
    from webagents.providers.openai_compatible import OpenAICompatibleProvider

    headers = (
        {"HTTP-Referer": settings.http_referer}
        if settings.provider == "openrouter" and settings.http_referer
        else None
    )
    assert settings.base_url is not None
    return OpenAICompatibleProvider(
        provider=settings.provider,
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        default_headers=headers,
    )

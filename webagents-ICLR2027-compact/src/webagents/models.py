"""Provider-qualified model resolution without exposing credentials to run data."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from webagents.providers import config as provider_config

ModelConfigurationError = provider_config.ProviderConfigurationError


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    requested_id: str
    provider: str
    provider_model: str
    client: Any


def split_model_id(model_id: str) -> tuple[str, str]:
    """Compatibility wrapper for the shared provider-qualified model parser."""

    return provider_config.split_model_id(model_id)


def resolve_model(model_id: str, *, environ: Mapping[str, str] | None = None) -> ResolvedModel:
    """Resolve a model ID using provider settings sourced exclusively from the environment."""

    resolved = provider_config.resolve_provider_settings(model_id, environ=environ)

    # Lazy import keeps schema/archive tooling usable without importing Browser Use.
    from webagents.dom.browser_use_adapter import make_browser_use_model

    if resolved.provider == "openrouter":
        settings: dict[str, Any] = {"api_key": resolved.api_key}
        if resolved.base_url:
            settings["base_url"] = resolved.base_url
        if resolved.http_referer:
            settings["http_referer"] = resolved.http_referer
    else:
        settings = {"api_key": resolved.api_key, "base_url": resolved.base_url}
        if provider_config.requires_prompt_only_json(resolved.model):
            settings.update(
                add_schema_to_system_prompt=True,
                dont_force_structured_output=True,
            )

    client = make_browser_use_model(provider=resolved.provider, model=resolved.model, settings=settings)
    return ResolvedModel(
        requested_id=model_id,
        provider=resolved.provider,
        provider_model=resolved.model,
        client=client,
    )

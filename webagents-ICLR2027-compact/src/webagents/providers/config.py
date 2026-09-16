"""Provider-qualified model configuration sourced only from environment variables."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

from webagents.schemas import MODEL_ID_PATTERN

ProviderName = Literal["foundry", "openrouter"]

# These Foundry deployments expose OpenAI-compatible chat completions but do not
# advertise JSON response formats. Keep the deployment names fixed in the
# committed benchmark roster so request construction remains reproducible.
PROMPT_ONLY_JSON_DEPLOYMENTS = frozenset({"kimi-k2.6"})


class ProviderConfigurationError(RuntimeError):
    """A provider cannot be configured safely from the current environment."""


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    requested_id: str
    provider: ProviderName
    model: str
    api_key: str
    base_url: str | None = None
    http_referer: str | None = None


def requires_prompt_only_json(model: str) -> bool:
    """Return whether structured JSON must be requested through the prompt."""

    return model.casefold() in PROMPT_ONLY_JSON_DEPLOYMENTS


def split_model_id(model_id: str) -> tuple[ProviderName, str]:
    match = MODEL_ID_PATTERN.fullmatch(model_id)
    if match is None:
        raise ProviderConfigurationError(
            "model must use foundry/<deployment> or openrouter/<model>"
        )
    return cast(ProviderName, match.group(1)), match.group(2)


def _required(environment: Mapping[str, str], names: tuple[str, ...], *, label: str) -> str:
    for name in names:
        value = environment.get(name)
        if value and value.strip():
            return value.strip()
    raise ProviderConfigurationError(f"{label} is required")


def normalize_foundry_openai_endpoint(endpoint: str) -> str:
    """Normalize a Foundry resource endpoint to its OpenAI-compatible v1 base URL."""

    value = endpoint.strip()
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ProviderConfigurationError(
            "FOUNDRY_OPENAI_ENDPOINT must be an HTTPS Foundry resource endpoint without credentials or query parameters"
        )
    if not (
        parts.hostname.endswith(".openai.azure.com") or parts.hostname.endswith(".services.ai.azure.com")
    ):
        raise ProviderConfigurationError(
            "FOUNDRY_OPENAI_ENDPOINT must use a .openai.azure.com or .services.ai.azure.com resource host"
        )
    path = parts.path.rstrip("/")
    if path not in {"", "/openai/v1"}:
        raise ProviderConfigurationError(
            "FOUNDRY_OPENAI_ENDPOINT must be the resource root or end with /openai/v1"
        )
    return urlunsplit((parts.scheme, parts.netloc, "/openai/v1/", "", ""))


def resolve_provider_settings(
    model_id: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> ProviderSettings:
    """Resolve credentials and endpoints without constructing an SDK client."""

    environment = os.environ if environ is None else environ
    provider, model = split_model_id(model_id)
    if provider == "openrouter":
        return ProviderSettings(
            requested_id=model_id,
            provider=provider,
            model=model,
            api_key=_required(environment, ("OPENROUTER_API_KEY",), label="OPENROUTER_API_KEY"),
            base_url=environment.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            http_referer=environment.get("OPENROUTER_HTTP_REFERER"),
        )

    api_key = _required(
        environment,
        ("FOUNDRY_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY"),
        label="FOUNDRY_API_KEY (or AZURE_OPENAI_API_KEY)",
    )
    endpoint = _required(
        environment,
        ("FOUNDRY_OPENAI_ENDPOINT", "AZURE_OPENAI_ENDPOINT"),
        label="FOUNDRY_OPENAI_ENDPOINT (or AZURE_OPENAI_ENDPOINT)",
    )
    return ProviderSettings(
        requested_id=model_id,
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=normalize_foundry_openai_endpoint(endpoint),
    )


def validate_provider_environment(
    model_ids: Iterable[str],
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Validate every unique provider/model configuration without making network calls."""

    for model_id in dict.fromkeys(model_ids):
        resolve_provider_settings(model_id, environ=environ)

from __future__ import annotations

import sys

import pytest

from webagents.providers import ProviderConfigurationError, resolve_provider


def test_ax_provider_resolution_does_not_import_browser_use() -> None:
    provider = resolve_provider(
        "openrouter/openai/gpt-5.2",
        environ={"OPENROUTER_API_KEY": "test-only"},
    )
    assert provider.provider == "openrouter"
    assert not any(name == "browser_use" or name.startswith("browser_use.") for name in sys.modules)


def test_ax_provider_credentials_are_environment_only() -> None:
    with pytest.raises(ProviderConfigurationError):
        resolve_provider("foundry/deployment", environ={})


def test_ax_foundry_provider_uses_deployment_from_model_id() -> None:
    provider = resolve_provider(
        "foundry/gpt-5-mini",
        environ={
            "FOUNDRY_API_KEY": "test-only",
            "FOUNDRY_OPENAI_ENDPOINT": "https://anonymous-resource.services.ai.azure.com",
        },
    )
    assert provider.provider == "foundry"
    assert provider.model == "gpt-5-mini"

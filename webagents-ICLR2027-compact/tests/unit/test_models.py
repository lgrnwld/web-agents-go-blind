from __future__ import annotations

from types import SimpleNamespace

import pytest

from webagents.dom import browser_use_adapter
from webagents.models import ModelConfigurationError, resolve_model, split_model_id
from webagents.providers.config import normalize_foundry_openai_endpoint


def test_split_model_id_preserves_provider_model() -> None:
    assert split_model_id("openrouter/openai/gpt-5.2") == ("openrouter", "openai/gpt-5.2")
    assert split_model_id("foundry/research-deployment") == ("foundry", "research-deployment")


def test_openrouter_resolution_reads_credentials_from_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_factory(*, provider: str, model: str, settings: dict[str, object]) -> object:
        captured.update(provider=provider, model=model, settings=settings)
        return SimpleNamespace()

    monkeypatch.setattr(browser_use_adapter, "make_browser_use_model", fake_factory)
    resolved = resolve_model(
        "openrouter/openai/gpt-5.2",
        environ={"OPENROUTER_API_KEY": "secret", "OPENROUTER_BASE_URL": "https://router.invalid/v1"},
    )
    assert resolved.requested_id == "openrouter/openai/gpt-5.2"
    assert captured["settings"] == {"api_key": "secret", "base_url": "https://router.invalid/v1"}


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        (
            "https://anonymous-resource.services.ai.azure.com",
            "https://anonymous-resource.services.ai.azure.com/openai/v1/",
        ),
        (
            "https://anonymous-resource.services.ai.azure.com/openai/v1/",
            "https://anonymous-resource.services.ai.azure.com/openai/v1/",
        ),
    ],
)
def test_foundry_resolution_uses_openai_v1_endpoint(
    endpoint: str,
    expected: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_factory(*, provider: str, model: str, settings: dict[str, object]) -> object:
        captured.update(provider=provider, model=model, settings=settings)
        return SimpleNamespace()

    monkeypatch.setattr(browser_use_adapter, "make_browser_use_model", fake_factory)
    resolved = resolve_model(
        "foundry/gpt-5-mini",
        environ={"FOUNDRY_API_KEY": "secret", "FOUNDRY_OPENAI_ENDPOINT": endpoint},
    )
    assert resolved.provider == "foundry"
    assert captured == {
        "provider": "foundry",
        "model": "gpt-5-mini",
        "settings": {"api_key": "secret", "base_url": expected},
    }


def test_kimi_resolution_uses_prompt_only_structured_output(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_factory(*, provider: str, model: str, settings: dict[str, object]) -> object:
        captured.update(provider=provider, model=model, settings=settings)
        return SimpleNamespace()

    monkeypatch.setattr(browser_use_adapter, "make_browser_use_model", fake_factory)
    resolve_model(
        "foundry/Kimi-K2.6",
        environ={
            "FOUNDRY_API_KEY": "secret",
            "FOUNDRY_OPENAI_ENDPOINT": "https://anonymous-resource.services.ai.azure.com",
        },
    )
    assert captured["settings"] == {
        "api_key": "secret",
        "base_url": "https://anonymous-resource.services.ai.azure.com/openai/v1/",
        "add_schema_to_system_prompt": True,
        "dont_force_structured_output": True,
    }


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://research.openai.azure.com",
        "https://user:secret@research.openai.azure.com",
        "https://research.example.com",
        "https://anonymous-resource.services.ai.azure.com/openai/deployments/model",
    ],
)
def test_foundry_endpoint_rejects_unsafe_or_non_v1_values(endpoint: str) -> None:
    with pytest.raises(ModelConfigurationError):
        normalize_foundry_openai_endpoint(endpoint)


@pytest.mark.parametrize(
    "model_id",
    ["openrouter/openai/gpt-5.2", "foundry/deployment"],
)
def test_resolution_rejects_missing_credentials(model_id: str) -> None:
    with pytest.raises(ModelConfigurationError):
        resolve_model(model_id, environ={})

from __future__ import annotations

import json

from webagents import config_cli
from webagents.providers.base import ProviderCallResult


def test_config_check_is_credential_safe(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FOUNDRY_API_KEY", "never-print-this-key")
    monkeypatch.setenv("FOUNDRY_OPENAI_ENDPOINT", "https://anonymous-resource.services.ai.azure.com")
    assert config_cli.main(["--model", "foundry/gpt-5-mini"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["configured"] is True
    assert payload["models"][0]["provider_model"] == "gpt-5-mini"
    assert "never-print-this-key" not in output
    assert "research.services.ai.azure.com" not in output


def test_config_check_reports_missing_environment(monkeypatch, capsys) -> None:
    for name in (
        "FOUNDRY_API_KEY",
        "FOUNDRY_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_KEY",
        "AZURE_OPENAI_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert config_cli.main(["--model", "foundry/gpt-5-mini"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["configured"] is False


def test_config_probe_checks_provider_without_exposing_credentials(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FOUNDRY_API_KEY", "never-print-this-key")
    monkeypatch.setenv("FOUNDRY_OPENAI_ENDPOINT", "https://anonymous-resource.services.ai.azure.com")
    requests: list[bytes] = []

    class FakeProvider:
        async def invoke(self, request: bytes) -> ProviderCallResult:
            requests.append(request)
            return ProviderCallResult(
                text="OK",
                input_tokens=12,
                output_tokens=3,
                metadata={"model": "gpt-5.6-terra-2026-07-09"},
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(config_cli, "resolve_provider", lambda model_id: FakeProvider())
    assert config_cli.main(["--probe", "--model", "foundry/gpt-5.6-terra"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["probes"][0]["status"] == "reachable"
    assert payload["probes"][0]["response_model"] == "gpt-5.6-terra-2026-07-09"
    assert json.loads(requests[0])["reasoning_effort"] == "low"
    assert "never-print-this-key" not in output

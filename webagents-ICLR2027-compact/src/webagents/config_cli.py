"""Credential-safe provider configuration diagnostics."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.control.config import ControlSpecError, load_control_spec
from webagents.providers import resolve_provider
from webagents.providers.config import ProviderConfigurationError, ProviderSettings, resolve_provider_settings

EXIT_SUCCESS = 0
EXIT_CONFIGURATION = 2
EXIT_PROVIDER = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="webagent-config")
    parser.add_argument(
        "--model",
        action="append",
        help="provider-qualified model ID; repeat to check a roster (defaults to the control spec)",
    )
    parser.add_argument("--spec", type=Path, default=Path("benchmarks/control/control.yaml"))
    parser.add_argument("--probe", action="store_true", help="make one small paid request to each model")
    return parser


def _probe_request(settings: ProviderSettings) -> bytes:
    request: dict[str, Any] = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": "This is a deployment health check."},
            {"role": "user", "content": "Reply with exactly OK and no other text."},
        ],
        "max_completion_tokens": 256,
    }
    if settings.model.casefold().startswith("gpt-5"):
        request["reasoning_effort"] = "low"
    return canonical_json_bytes(request)


async def _probe_models(settings_by_id: dict[str, ProviderSettings]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for model_id, settings in settings_by_id.items():
        provider = resolve_provider(model_id)
        try:
            response = await provider.invoke(_probe_request(settings))
        finally:
            close = getattr(provider, "close", None)
            if close is not None:
                await close()
        results.append(
            {
                "requested_model": model_id,
                "status": "reachable",
                "response_model": str((response.metadata or {}).get("model", settings.model)),
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configured: list[dict[str, str]] = []
    settings_by_id: dict[str, ProviderSettings] = {}
    try:
        model_ids = args.model
        if not model_ids:
            model_ids = [model.runner_model for model in load_control_spec(args.spec).models]
        for model_id in dict.fromkeys(model_ids):
            settings = resolve_provider_settings(model_id)
            settings_by_id[model_id] = settings
            configured.append(
                {
                    "requested_model": settings.requested_id,
                    "provider": settings.provider,
                    "provider_model": settings.model,
                    "status": "configured",
                }
            )
    except (ControlSpecError, ProviderConfigurationError) as exc:
        print(json.dumps({"configured": False, "error": str(exc)}, sort_keys=True))
        return EXIT_CONFIGURATION
    payload: dict[str, Any] = {"configured": True, "models": configured}
    if args.probe:
        try:
            payload["probes"] = asyncio.run(_probe_models(settings_by_id))
        except Exception as exc:
            detail = str(exc)
            for settings in settings_by_id.values():
                detail = detail.replace(settings.api_key, "<redacted>")
            payload["probe_error"] = {"type": type(exc).__name__, "detail": detail}
            print(json.dumps(payload, sort_keys=True))
            return EXIT_PROVIDER
    print(json.dumps(payload, sort_keys=True))
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())

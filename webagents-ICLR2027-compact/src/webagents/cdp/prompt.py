"""Versioned prompt for normalized CDP DOMSnapshot observations."""

from __future__ import annotations

from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.providers.config import requires_prompt_only_json

PROMPT_VERSION = "cdp-dom-snapshot-action-v1"

_SYSTEM_TEMPLATE = """You are a web agent whose only page observation is a normalized Chromium
DOMSnapshot captured explicitly across page and iframe targets. The user task is: {task}

The user message is exactly one canonical JSON capture envelope. Each document contains DOM nodes with names,
values, attributes, parent indexes, optional layout bounds, and step-local cdp-N element references. You have
no screenshot and no accessibility tree. Return exactly one JSON action and no markdown:
{{"action":"click","ref":"cdp-17"}}
{{"action":"type","ref":"cdp-22","text":"value"}}
{{"action":"press","key":"Enter"}}
{{"action":"scroll","direction":"down","amount":600}}
{{"action":"wait","milliseconds":500}}
{{"action":"finish","answer":"...","success":true}}
Do not invent actions or fields. A type action replaces the current field value. References expire after this
decision. Inspect the next snapshot after every action. When the requested state is complete, the URL reflects
submission, or a confirmation such as Saved is present, immediately finish successfully. Prompt version:
{version}.{previous}
"""


def build_model_request(
    *,
    model: str,
    task_prompt: str,
    observation_bytes: bytes,
    previous_action_result: dict[str, Any] | None,
) -> tuple[dict[str, Any], bytes]:
    observation_text = observation_bytes.decode("utf-8", errors="strict")
    previous = ""
    if previous_action_result is not None:
        previous = "\nPrevious typed action result (not page content): " + canonical_json_bytes(
            previous_action_result
        ).decode("utf-8")
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": _SYSTEM_TEMPLATE.format(task=task_prompt, version=PROMPT_VERSION, previous=previous),
            },
            {"role": "user", "content": observation_text},
        ],
    }
    if not requires_prompt_only_json(model):
        request["response_format"] = {"type": "json_object"}
    return request, canonical_json_bytes(request)

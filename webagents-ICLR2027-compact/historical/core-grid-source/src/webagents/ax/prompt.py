"""Versioned AX-only prompt and provider request construction."""

from __future__ import annotations

from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.providers.config import requires_prompt_only_json

PROMPT_VERSION = "ax-cdp-full-action-v3"

_SYSTEM_TEMPLATE = """You are a web agent whose only page observation is Chromium's raw accessibility tree.
The user task is: {task}

The user message is exactly one canonical JSON AX capture envelope. It contains Chromium's full raw protocol
results plus an action_references catalog derived only from those AX nodes. Interpret AX roles, accessible names,
values, states, and properties. You have no screenshot and no DOM text. Use the catalog's explicit ax-N ref for
the intended node. Never calculate a reference from a nodeId, backendDOMNodeId, or array position.
References expire after this decision. Return exactly one JSON action and no markdown:
{{"action":"click","ref":"ax-17"}}
{{"action":"type","ref":"ax-22","text":"value"}}
{{"action":"press","key":"Enter"}}
{{"action":"scroll","direction":"down","amount":600}}
{{"action":"wait","milliseconds":500}}
{{"action":"finish","answer":"...","success":true}}
Do not invent actions or fields. A type action replaces the current field value. After every action, inspect
the next AX capture before deciding again. When the requested state is complete, the URL reflects submission,
or a confirmation such as Saved is present, immediately return finish with success true. Do not repeat an
already completed input or submission action. Use finish with success false only when the task cannot be completed.
Prompt version: {version}.{previous}
"""


def build_model_request(
    *,
    model: str,
    task_prompt: str,
    observation_bytes: bytes,
    previous_action_result: dict[str, Any] | None,
) -> tuple[dict[str, Any], bytes]:
    """Build a request whose user-content value is the exact canonical AX JSON."""

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

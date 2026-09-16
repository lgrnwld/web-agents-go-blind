"""Versioned request construction for screenshot-only perception."""

from __future__ import annotations

import base64
from typing import Any

from webagents.capture.archive import canonical_json_bytes
from webagents.providers.config import requires_prompt_only_json

PROMPT_VERSION = "vision-screenshot-coordinate-v1"

_SYSTEM_TEMPLATE = """You are a web agent whose only page-content observation is one current browser screenshot.
The user task is: {task}

You receive no DOM, accessibility tree, OCR transcript, element list, or hidden page metadata. Coordinates use
the screenshot's pixel coordinate system with origin (0,0) at the top left. Return exactly one JSON action and
no markdown:
{{"action":"click","x":420,"y":315}}
{{"action":"type","x":420,"y":315,"text":"value"}}
{{"action":"press","key":"Enter"}}
{{"action":"scroll","x":640,"y":450,"direction":"down","amount":600}}
{{"action":"wait","milliseconds":500}}
{{"action":"finish","answer":"...","success":true}}
Do not invent actions or fields. A type action replaces the current field value. Put the pointer over the
scrollable region when scrolling nested frames. After every action, inspect the next screenshot. When the task
is complete, the URL reflects submission, or a visible confirmation such as Saved appears, immediately finish.
Use finish with success false only when the task cannot be completed. Prompt version: {version}.{previous}
"""


def build_model_request(
    *,
    model: str,
    task_prompt: str,
    screenshot_bytes: bytes,
    previous_action_result: dict[str, Any] | None,
) -> tuple[dict[str, Any], bytes]:
    previous = ""
    if previous_action_result is not None:
        previous = "\nPrevious typed action result (not page content): " + canonical_json_bytes(
            previous_action_result
        ).decode("utf-8")
    data_url = "data:image/png;base64," + base64.b64encode(screenshot_bytes).decode("ascii")
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": _SYSTEM_TEMPLATE.format(task=task_prompt, version=PROMPT_VERSION, previous=previous),
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Current viewport screenshot:"},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            },
        ],
    }
    if not requires_prompt_only_json(model):
        request["response_format"] = {"type": "json_object"}
    return request, canonical_json_bytes(request)

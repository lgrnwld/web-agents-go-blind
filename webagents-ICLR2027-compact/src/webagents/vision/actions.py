"""Closed coordinate-action protocol for the screenshot-only runner."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from webagents.schemas import ActionRecord, ActionResultRecord


class ActionParseError(ValueError):
    """The provider returned something outside the vision action protocol."""


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: str


class ClickAction(_Action):
    action: Literal["click"]
    x: int = Field(ge=0, le=10_000)
    y: int = Field(ge=0, le=10_000)


class TypeAction(_Action):
    action: Literal["type"]
    x: int = Field(ge=0, le=10_000)
    y: int = Field(ge=0, le=10_000)
    text: str = Field(max_length=10_000)


class PressAction(_Action):
    action: Literal["press"]
    key: str = Field(min_length=1, max_length=80)


class ScrollAction(_Action):
    action: Literal["scroll"]
    x: int = Field(ge=0, le=10_000)
    y: int = Field(ge=0, le=10_000)
    direction: Literal["up", "down"]
    amount: int = Field(gt=0, le=5000)


class WaitAction(_Action):
    action: Literal["wait"]
    milliseconds: int = Field(ge=0, le=5000)


class FinishAction(_Action):
    action: Literal["finish"]
    answer: str = Field(max_length=100_000)
    success: bool


VisionAction = Annotated[
    ClickAction | TypeAction | PressAction | ScrollAction | WaitAction | FinishAction,
    Field(discriminator="action"),
]
_ACTION_ADAPTER: TypeAdapter[VisionAction] = TypeAdapter(VisionAction)
_SAFE_KEY = re.compile(
    r"^(?:(?:Control|Alt|Meta|Shift)\+)*(?:[A-Za-z0-9]|Enter|Tab|Escape|Backspace|Delete|Space|"
    r"Arrow(?:Up|Down|Left|Right)|Home|End|PageUp|PageDown)$"
)


def parse_action(text: str) -> VisionAction:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionParseError(f"model response is not one JSON value: {exc}") from exc
    try:
        action = _ACTION_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ActionParseError(f"invalid vision action: {exc}") from exc
    if isinstance(action, PressAction) and not _SAFE_KEY.fullmatch(action.key):
        raise ActionParseError(f"unsupported key chord: {action.key!r}")
    return action


def action_record(action: VisionAction) -> ActionRecord:
    payload = action.model_dump(mode="json")
    return ActionRecord(name=str(payload.pop("action")), parameters=payload)


async def execute_action(action: VisionAction, *, page: Any) -> ActionResultRecord:
    """Execute one validated action without deriving any extra page observation."""

    try:
        if isinstance(action, FinishAction):
            return ActionResultRecord(is_done=True, success=action.success, metadata={"code": "finished"})
        if isinstance(action, WaitAction):
            await page.wait_for_timeout(action.milliseconds)
        elif isinstance(action, ScrollAction):
            await page.mouse.move(action.x, action.y)
            delta = action.amount if action.direction == "down" else -action.amount
            await page.mouse.wheel(0, delta)
        elif isinstance(action, PressAction):
            await page.keyboard.press(action.key)
        elif isinstance(action, ClickAction):
            await page.mouse.click(action.x, action.y)
        elif isinstance(action, TypeAction):
            await page.mouse.click(action.x, action.y)
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.insert_text(action.text)
        return ActionResultRecord(success=True, metadata={"code": "ok"})
    except Exception as exc:
        return ActionResultRecord(error=str(exc), success=False, metadata={"code": "action_error"})

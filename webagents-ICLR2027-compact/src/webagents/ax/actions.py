"""Strict AX action parsing and non-perceptual execution."""

from __future__ import annotations

import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from webagents.ax.refs import ReferenceMap
from webagents.ax.targets import CDPConnection, CDPError
from webagents.schemas import ActionRecord, ActionResultRecord


class ActionParseError(ValueError):
    """The provider returned something outside the closed AX action protocol."""


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: str


class ClickAction(_Action):
    action: Literal["click"]
    ref: str = Field(pattern=r"^ax-[0-9]+$")


class TypeAction(_Action):
    action: Literal["type"]
    ref: str = Field(pattern=r"^ax-[0-9]+$")
    text: str = Field(max_length=10_000)


class PressAction(_Action):
    action: Literal["press"]
    key: str = Field(min_length=1, max_length=80)


class ScrollAction(_Action):
    action: Literal["scroll"]
    direction: Literal["up", "down"]
    amount: int = Field(gt=0, le=5000)


class WaitAction(_Action):
    action: Literal["wait"]
    milliseconds: int = Field(ge=0, le=5000)


class FinishAction(_Action):
    action: Literal["finish"]
    answer: str = Field(max_length=100_000)
    success: bool


AXAction = Annotated[
    ClickAction | TypeAction | PressAction | ScrollAction | WaitAction | FinishAction,
    Field(discriminator="action"),
]
_ACTION_ADAPTER: TypeAdapter[AXAction] = TypeAdapter(AXAction)
_SAFE_KEY = re.compile(
    r"^(?:(?:Control|Alt|Meta|Shift)\+)*(?:[A-Za-z0-9]|Enter|Tab|Escape|Backspace|Delete|Space|"
    r"Arrow(?:Up|Down|Left|Right)|Home|End|PageUp|PageDown)$"
)


def parse_action(text: str) -> AXAction:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionParseError(f"model response is not one JSON value: {exc}") from exc
    try:
        action = _ACTION_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise ActionParseError(f"invalid AX action: {exc}") from exc
    if isinstance(action, PressAction) and not _SAFE_KEY.fullmatch(action.key):
        raise ActionParseError(f"unsupported key chord: {action.key!r}")
    return action


def action_record(action: AXAction) -> ActionRecord:
    payload = action.model_dump(mode="json")
    return ActionRecord(name=str(payload.pop("action")), parameters=payload)


async def _resolve_object(
    connection: CDPConnection,
    refs: ReferenceMap,
    ref: str,
    generation: int,
) -> tuple[str, str]:
    resolved = refs.resolve(ref, generation=generation)
    result = await connection.send(
        "DOM.resolveNode",
        {"backendNodeId": resolved.backend_dom_node_id},
        session_id=resolved.session_id,
    )
    object_id = result.get("object", {}).get("objectId")
    if not isinstance(object_id, str):
        raise CDPError(f"DOM.resolveNode returned no object for {ref}")
    return resolved.session_id, object_id


async def execute_action(
    action: AXAction,
    *,
    refs: ReferenceMap,
    generation: int,
    connection: CDPConnection,
    page: Any,
) -> ActionResultRecord:
    """Execute one validated action and return only typed, non-perceptual status."""

    try:
        if isinstance(action, FinishAction):
            return ActionResultRecord(is_done=True, success=action.success, metadata={"code": "finished"})
        if isinstance(action, WaitAction):
            await page.wait_for_timeout(action.milliseconds)
        elif isinstance(action, ScrollAction):
            delta = action.amount if action.direction == "down" else -action.amount
            await page.mouse.wheel(0, delta)
        elif isinstance(action, PressAction):
            await page.keyboard.press(action.key)
        elif isinstance(action, (ClickAction, TypeAction)):
            session_id, object_id = await _resolve_object(connection, refs, action.ref, generation)
            function = (
                "function(){this.click()}"
                if isinstance(action, ClickAction)
                else "function(){this.focus();if(typeof this.select==='function'){this.select()}}"
            )
            await connection.send(
                "Runtime.callFunctionOn",
                {"objectId": object_id, "functionDeclaration": function, "returnByValue": True},
                session_id=session_id,
            )
            if isinstance(action, TypeAction):
                await page.keyboard.insert_text(action.text)
        return ActionResultRecord(success=True, metadata={"code": "ok"})
    except Exception as exc:
        code = getattr(exc, "code", "action_error")
        return ActionResultRecord(error=str(exc), success=False, metadata={"code": code})
    finally:
        refs.invalidate()

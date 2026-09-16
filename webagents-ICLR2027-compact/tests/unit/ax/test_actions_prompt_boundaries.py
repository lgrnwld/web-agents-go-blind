from __future__ import annotations

import json
from pathlib import Path

import pytest

from webagents.ax import actions as actions_module
from webagents.ax.actions import ActionParseError, FinishAction, execute_action, parse_action
from webagents.ax.prompt import build_model_request


def test_prompt_contains_exact_ax_json_as_only_user_content() -> None:
    observation = b'{"protocol_method":"Accessibility.getFullAXTree","targets":[]}'
    request, encoded = build_model_request(
        model="openai/gpt-5.2",
        task_prompt="Read the accessible label",
        observation_bytes=observation,
        previous_action_result={"success": True, "error": None, "code": "ok"},
    )
    assert request["messages"][1] == {"role": "user", "content": observation.decode()}
    assert json.loads(encoded) == request
    assert "serialized DOM" not in request["messages"][0]["content"]
    assert "action_references" in request["messages"][0]["content"]
    assert "Never calculate a reference" in request["messages"][0]["content"]
    assert "immediately return finish" in request["messages"][0]["content"]


def test_kimi_prompt_uses_text_json_without_response_format() -> None:
    request, encoded = build_model_request(
        model="Kimi-K2.6",
        task_prompt="Read the accessible label",
        observation_bytes=b'{"targets":[]}',
        previous_action_result=None,
    )
    assert "response_format" not in request
    assert json.loads(encoded) == request


@pytest.mark.parametrize(
    "response",
    [
        '{"action":"evaluate","code":"document.body.innerText"}',
        '{"action":"click","ref":"ax-1","javascript":"alert(1)"}',
        '```json\n{"action":"wait","milliseconds":1}\n```',
        '{"action":"press","key":"NotARealKey"}',
        '{"action":"wait","milliseconds":5001}',
    ],
)
def test_malformed_or_expansive_actions_are_rejected(response: str) -> None:
    with pytest.raises(ActionParseError):
        parse_action(response)


def test_finish_action_is_strictly_parsed() -> None:
    action = parse_action('{"action":"finish","answer":"done","success":true}')
    assert isinstance(action, FinishAction)
    assert action.success is True


@pytest.mark.asyncio
async def test_type_selects_existing_value_before_inserting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object], str | None]] = []

    async def resolve_object(*args: object, **kwargs: object) -> tuple[str, str]:
        del args, kwargs
        return "session-1", "object-1"

    class Connection:
        async def send(
            self,
            method: str,
            params: dict[str, object],
            *,
            session_id: str | None = None,
        ) -> dict[str, object]:
            calls.append((method, params, session_id))
            return {}

    class Keyboard:
        def __init__(self) -> None:
            self.inserted: list[str] = []

        async def insert_text(self, text: str) -> None:
            self.inserted.append(text)

    class Page:
        def __init__(self) -> None:
            self.keyboard = Keyboard()

    class Refs:
        def __init__(self) -> None:
            self.invalidated = False

        def invalidate(self) -> None:
            self.invalidated = True

    monkeypatch.setattr(actions_module, "_resolve_object", resolve_object)
    action = parse_action('{"action":"type","ref":"ax-13","text":"BRAVO-4826"}')
    page = Page()
    refs = Refs()

    result = await execute_action(
        action,
        refs=refs,  # type: ignore[arg-type]
        generation=1,
        connection=Connection(),  # type: ignore[arg-type]
        page=page,
    )

    assert result.success is True
    assert page.keyboard.inserted == ["BRAVO-4826"]
    assert refs.invalidated is True
    assert calls[0][0] == "Runtime.callFunctionOn"
    assert "this.select()" in str(calls[0][1]["functionDeclaration"])


def test_ax_source_has_no_dom_or_browser_use_perception_path() -> None:
    source_root = Path(__file__).parents[3] / "src" / "webagents" / "ax"
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.py"))
    prohibited = (
        "browser_use",
        "page.content(",
        ".inner_text(",
        "document.body.innerText",
        "document.body.textContent",
    )
    assert all(token not in source for token in prohibited)


def test_importing_ax_does_not_load_browser_use(monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import webagents.ax.runner; "
            "assert not any(x == 'browser_use' or x.startswith('browser_use.') for x in sys.modules)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

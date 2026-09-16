"""Loopback-only, matched structural-containment fixture harness."""

from __future__ import annotations

import hashlib
import html
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

from webagents.capture.archive import canonical_json_bytes
from webagents.control.harness import (
    HarnessError,
    render_copy_form,
    render_fixture_style,
    render_level0_page,
    render_submission_confirmation,
)
from webagents.control.schemas import ResolvedTask
from webagents.sweeps.schemas import StructuralCondition, SweepHarnessSpec


def _request(url: str, *, method: str = "GET") -> bytes:
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != HTTPStatus.OK:
                raise HarnessError(f"{method} {url} returned HTTP {response.status}")
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise HarnessError(f"{method} {url} failed: {exc}") from exc


class StructuralFixtureHarness:
    """Serve identical task semantics through one structural wrapper at a time."""

    def __init__(
        self,
        spec: SweepHarnessSpec,
        tasks: Iterable[ResolvedTask],
        conditions: Iterable[StructuralCondition],
    ) -> None:
        self.spec = spec
        self.tasks = list(tasks)
        self.conditions = list(conditions)
        self.primary_base_url = ""
        self.cross_origin_base_url = ""
        self._servers: list[ThreadingHTTPServer] = []
        self._threads: list[threading.Thread] = []
        self._state: dict[tuple[str, int], str | None] = {
            (task.revision.task_id, task.revision.revision): None for task in self.tasks
        }

    def __enter__(self) -> StructuralFixtureHarness:
        try:
            primary = ThreadingHTTPServer((self.spec.host, self.spec.primary_port), self._handler_type("primary"))
            self._servers.append(primary)
            secondary = ThreadingHTTPServer((self.spec.host, self.spec.cross_origin_port), self._handler_type("cross"))
            self._servers.append(secondary)
            primary_host, primary_port = primary.server_address[:2]
            secondary_host, secondary_port = secondary.server_address[:2]
            if primary_port == secondary_port:
                raise HarnessError("fixture servers did not receive distinct origins")
            self.primary_base_url = f"http://{primary_host}:{primary_port}"
            self.cross_origin_base_url = f"http://{secondary_host}:{secondary_port}"
            for server in self._servers:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self._threads.append(thread)
            self.health()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        for server in self._servers[: len(self._threads)]:
            server.shutdown()
        for server in self._servers:
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._servers.clear()
        self._threads.clear()

    def health(self) -> None:
        for base_url in (self.primary_base_url, self.cross_origin_base_url):
            payload = json.loads(_request(f"{base_url}/health"))
            if payload.get("status") != "ok":
                raise HarnessError(f"fixture health response is not ok for {base_url}")

    def condition_path(self, task: ResolvedTask, condition: StructuralCondition) -> str:
        if condition.id == "level-0":
            return task.revision.level0.page_path
        return f"/sweeps/ax-full/{task.revision.task_id}/r{task.revision.revision}/{condition.id}"

    def condition_url(self, task: ResolvedTask, condition: StructuralCondition) -> str:
        return urllib.parse.urljoin(f"{self.primary_base_url}/", self.condition_path(task, condition).lstrip("/"))

    def reset(self, task: ResolvedTask) -> None:
        _request(
            urllib.parse.urljoin(f"{self.primary_base_url}/", task.revision.level0.reset_path.lstrip("/")),
            method="POST",
        )

    def read_json_checker(self, task: ResolvedTask) -> Any:
        checker = task.revision.level0.checker
        if checker.endpoint_path is None or checker.json_field is None:
            raise HarnessError("task does not define a JSON endpoint checker")
        value: Any = json.loads(
            _request(urllib.parse.urljoin(f"{self.primary_base_url}/", checker.endpoint_path.lstrip("/")))
        )
        for part in checker.json_field.split("."):
            if not isinstance(value, dict) or part not in value:
                raise HarnessError(f"checker field {checker.json_field!r} is absent")
            value = value[part]
        return value

    def semantic_payload_sha256(self, task: ResolvedTask) -> str:
        level0 = task.revision.level0
        fixture = level0.fixture
        payload = {
            "task_id": task.revision.task_id,
            "task_revision": task.revision.revision,
            "prompt": level0.prompt,
            "marker": level0.reachability_marker,
            "checker": level0.checker.model_dump(mode="json"),
            "fixture": fixture.model_dump(mode="json"),
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def preflight(self) -> None:
        if (
            urllib.parse.urlsplit(self.primary_base_url).netloc
            == urllib.parse.urlsplit(self.cross_origin_base_url).netloc
        ):
            raise HarnessError("primary and cross-origin harness servers share an origin")
        for task in self.tasks:
            self.reset(task)
            semantic_hashes: set[str] = set()
            for condition in self.conditions:
                semantic_hashes.add(self.semantic_payload_sha256(task))
                page = _request(self.condition_url(task, condition)).decode("utf-8")
                if condition.kind == "iframe":
                    target_url = self._frame_url(
                        task,
                        condition,
                        condition.iframe_depth,
                        self._frame_role(condition, condition.iframe_depth),
                    )
                    page = _request(target_url).decode("utf-8")
                evidence_source = page
                if condition.kind == "canvas":
                    canvas_data_url = f"{self.primary_base_url}{self._root_path(task, condition)}/canvas-data"
                    evidence_source = _request(canvas_data_url).decode("utf-8")
                if task.revision.level0.reachability_marker not in evidence_source:
                    raise HarnessError(f"fixture source omits marker for {task.revision.task_id} {condition.id}")
            if len(semantic_hashes) != 1:
                raise HarnessError(f"condition semantics diverged for {task.revision.task_id}")
            checker = task.revision.level0.checker
            if checker.name == "json_endpoint_equals" and self.read_json_checker(task) == checker.expected:
                raise HarnessError("reset checker already contains the successful value")

    def _root_path(self, task: ResolvedTask, condition: StructuralCondition) -> str:
        return f"/sweeps/ax-full/{task.revision.task_id}/r{task.revision.revision}/{condition.id}"

    def _frame_path(self, task: ResolvedTask, condition: StructuralCondition, frame_index: int) -> str:
        return f"{self._root_path(task, condition)}/frame/{frame_index}"

    def _frame_role(self, condition: StructuralCondition, frame_index: int) -> Literal["primary", "cross"]:
        if condition.iframe_origin == "same" or frame_index % 2 == 0:
            return "primary"
        return "cross"

    def _base_for_role(self, role: Literal["primary", "cross"]) -> str:
        return self.primary_base_url if role == "primary" else self.cross_origin_base_url

    def _frame_url(
        self,
        task: ResolvedTask,
        condition: StructuralCondition,
        frame_index: int,
        role: Literal["primary", "cross"],
    ) -> str:
        return f"{self._base_for_role(role)}{self._frame_path(task, condition, frame_index)}"

    def _handler_type(self, role: Literal["primary", "cross"]) -> type[BaseHTTPRequestHandler]:
        harness = self
        tasks_by_page = {task.revision.level0.page_path: task for task in self.tasks}
        tasks_by_reset = {task.revision.level0.reset_path: task for task in self.tasks}
        tasks_by_check = {
            task.revision.level0.checker.endpoint_path: task
            for task in self.tasks
            if task.revision.level0.checker.endpoint_path is not None
        }
        tasks_by_control_submit = {f"{task.revision.level0.page_path.rstrip('/')}/submit": task for task in self.tasks}
        structural_roots = {
            harness._root_path(task, condition): (task, condition)
            for task in self.tasks
            for condition in self.conditions
            if condition.id != "level-0"
        }
        structural_submits = {f"{path}/submit": pair for path, pair in structural_roots.items()}
        structural_canvas_data = {
            f"{path}/canvas-data": pair for path, pair in structural_roots.items() if pair[1].kind == "canvas"
        }
        structural_frames = {
            harness._frame_path(task, condition, frame_index): (task, condition, frame_index)
            for task in self.tasks
            for condition in self.conditions
            if condition.kind == "iframe"
            for frame_index in range(1, condition.iframe_depth + 1)
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                if path == "/health":
                    self._json({"status": "ok", "role": role})
                elif task := tasks_by_page.get(path):
                    self._html(render_level0_page(task))
                elif task := tasks_by_check.get(path):
                    value = harness._state[(task.revision.task_id, task.revision.revision)]
                    self._json({"submitted_value": value})
                elif pair := structural_roots.get(path):
                    task, condition = pair
                    self._html(harness._render_structural(task, condition, frame_index=0, role=role))
                elif pair := structural_canvas_data.get(path):
                    task, _ = pair
                    self._text(harness._canvas_text(task))
                elif triple := structural_frames.get(path):
                    task, condition, frame_index = triple
                    self._html(harness._render_structural(task, condition, frame_index=frame_index, role=role))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                if task := tasks_by_reset.get(path):
                    harness._state[(task.revision.task_id, task.revision.revision)] = None
                    self._json({"status": "reset"})
                elif task := tasks_by_control_submit.get(path):
                    harness._record_submission(task, self)
                    self._html(render_submission_confirmation(task))
                elif pair := structural_submits.get(path):
                    task, condition = pair
                    harness._record_submission(task, self)
                    self._html(render_submission_confirmation(task, condition_id=condition.id))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _json(self, value: object) -> None:
                self._send("application/json; charset=utf-8", json.dumps(value).encode("utf-8"))

            def _html(self, value: str) -> None:
                self._send("text/html; charset=utf-8", value.encode("utf-8"))

            def _text(self, value: str) -> None:
                self._send("text/plain; charset=utf-8", value.encode("utf-8"))

            def _send(self, content_type: str, body: bytes) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def _record_submission(self, task: ResolvedTask, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        values = urllib.parse.parse_qs(handler.rfile.read(length).decode("utf-8"))
        self._state[(task.revision.task_id, task.revision.revision)] = values.get("destination", [""])[0]

    def _document(self, task: ResolvedTask, body: str, condition_id: str) -> str:
        title = html.escape(f"Structural benchmark: {task.revision.task_id} r{task.revision.revision} {condition_id}")
        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<title>{title}</title><style>body{{font-family:sans-serif}}iframe{{width:95%;height:360px}}"
            "canvas{border:1px solid #777}</style></head>"
            f"<body><main><h1>{title}</h1>{body}</main></body></html>"
        )

    def _target_markup(self, task: ResolvedTask, submit_path: str, *, saved: bool = False) -> str:
        level0 = task.revision.level0
        fixture = level0.fixture
        source_label = html.escape(fixture.source_label)
        source_value = html.escape(fixture.source_value)
        marker = html.escape(level0.reachability_marker)
        source = f'<p><strong>{source_label}</strong>: <span id="source">{source_value}</span></p>'
        marker_html = f'<p><small data-reachability-marker="true">Target marker: {marker}</small></p>'
        if fixture.kind == "read_value":
            return f'<section data-benchmark-target="true">{source}{marker_html}</section>'
        if saved:
            return "<section data-benchmark-target='true'><p role='status'>Saved</p></section>"
        form = render_copy_form(task, submit_path)
        body = f"{render_fixture_style(task)}{source}{form}{marker_html}"
        return f'<section data-benchmark-target="true">{body}</section>'

    def _canvas_text(self, task: ResolvedTask) -> str:
        level0 = task.revision.level0
        fixture = level0.fixture
        return f"{fixture.source_label}: {fixture.source_value} | {level0.reachability_marker}"

    def _canvas_markup(self, task: ResolvedTask, submit_path: str) -> str:
        level0 = task.revision.level0
        fixture = level0.fixture
        data_path = f"{submit_path.removesuffix('/submit')}/canvas-data"
        canvas = '<canvas id="target-canvas" width="900" height="160" aria-label="Rendered target"></canvas>'
        script = (
            "<script>const c=document.getElementById('target-canvas');const x=c.getContext('2d');"
            "x.font='24px sans-serif';x.fillStyle='#111';fetch("
            f"{json.dumps(data_path)}).then(r=>r.text()).then(t=>x.fillText(t,20,80));</script>"
        )
        if fixture.kind == "read_value":
            return f'<section data-benchmark-target="true">{canvas}{script}</section>'
        form = render_copy_form(task, submit_path)
        style = render_fixture_style(task)
        return f'<section data-benchmark-target="true">{style}{canvas}{script}{form}</section>'

    def _shadow_markup(self, task: ResolvedTask, condition: StructuralCondition, submit_path: str) -> str:
        target = self._target_markup(task, submit_path)
        statements = ["let container=document.getElementById('shadow-host-0');"]
        for index, mode in enumerate(condition.shadow_modes):
            statements.append(f"const root{index}=container.attachShadow({{mode:{json.dumps(mode)}}});")
            if index == len(condition.shadow_modes) - 1:
                statements.append(f"root{index}.innerHTML={json.dumps(target)};")
            else:
                statements.append(
                    f"root{index}.innerHTML='<div id=\"shadow-host-{index + 1}\"></div>';"
                    f"container=root{index}.getElementById('shadow-host-{index + 1}');"
                )
        return '<div id="shadow-host-0"></div><script>' + "".join(statements) + "</script>"

    def _render_structural(
        self,
        task: ResolvedTask,
        condition: StructuralCondition,
        *,
        frame_index: int,
        role: Literal["primary", "cross"],
    ) -> str:
        root_path = self._root_path(task, condition)
        submit_path = f"{root_path}/submit"
        if condition.kind == "iframe" and frame_index < condition.iframe_depth:
            next_index = frame_index + 1
            next_role = self._frame_role(condition, next_index)
            frame_url = self._frame_url(task, condition, next_index, next_role)
            label = html.escape(f"Containment frame {next_index} of {condition.iframe_depth}")
            body = f'<iframe title="{label}" src="{html.escape(frame_url)}"></iframe>'
            return self._document(task, body, condition.id)
        if condition.kind == "shadow":
            body = self._shadow_markup(task, condition, submit_path)
        elif condition.kind == "canvas":
            body = self._canvas_markup(task, submit_path)
        else:
            body = self._target_markup(task, submit_path)
        return self._document(task, body, condition.id)

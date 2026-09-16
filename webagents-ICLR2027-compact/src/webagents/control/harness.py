"""Deterministic built-in and external level-0 fixture harnesses."""

from __future__ import annotations

import html
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from webagents.control.schemas import HarnessSpec, ResolvedTask

_LARGE_TARGET_STYLE = """
<style data-fixture-presentation="large-target-v1">
  .large-target-form {
    display: grid;
    grid-template-columns: minmax(320px, 420px);
    gap: 12px;
    align-items: start;
    margin-top: 16px;
  }
  .large-target-form label {
    font: 600 20px/1.4 system-ui, sans-serif;
  }
  .large-target-form input {
    box-sizing: border-box;
    width: 100%;
    min-height: 56px;
    padding: 10px 12px;
    font: 20px/1.4 system-ui, sans-serif;
  }
  .large-target-form button {
    box-sizing: border-box;
    width: 160px;
    min-height: 56px;
    padding: 10px 18px;
    font: 600 20px/1.4 system-ui, sans-serif;
  }
</style>
""".strip()


def render_fixture_style(task: ResolvedTask) -> str:
    """Return the presentation CSS declared by an immutable task revision."""

    if task.revision.level0.fixture.kind == "copy_value_form_large_target":
        return _LARGE_TARGET_STYLE
    return ""


def render_copy_form(task: ResolvedTask, action: str) -> str:
    """Render one copy form with the task revision's declared presentation."""

    fixture = task.revision.level0.fixture
    if fixture.kind == "read_value":
        raise ValueError("read-value fixtures do not define a copy form")
    destination = html.escape(fixture.destination_label)
    submit = html.escape(fixture.submit_label)
    escaped_action = html.escape(action)
    form_class = ' class="large-target-form"' if fixture.kind == "copy_value_form_large_target" else ""
    return (
        f'<form{form_class} method="post" action="{escaped_action}">'
        f'<label for="destination">{destination}</label>'
        '<input id="destination" name="destination" autocomplete="off">'
        f'<button type="submit">{submit}</button></form>'
    )


class HarnessError(RuntimeError):
    """The synthetic fixture harness is unavailable or inconsistent."""


def _request(url: str, *, method: str = "GET") -> bytes:
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != HTTPStatus.OK:
                raise HarnessError(f"{method} {url} returned HTTP {response.status}")
            return response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise HarnessError(f"{method} {url} failed: {exc}") from exc


class FixtureHarness:
    def __init__(self, spec: HarnessSpec, tasks: Iterable[ResolvedTask]) -> None:
        self.spec = spec
        self.tasks = list(tasks)
        self.base_url = spec.base_url or ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._state: dict[tuple[str, int], str | None] = {
            (task.revision.task_id, task.revision.revision): None for task in self.tasks
        }

    def __enter__(self) -> FixtureHarness:
        try:
            if self.spec.kind == "builtin":
                handler = self._handler_type()
                self._server = ThreadingHTTPServer((self.spec.host, self.spec.port), handler)
                host, port = self._server.server_address[:2]
                self.base_url = f"http://{host}:{port}"
                self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
                self._thread.start()
            self.health()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def url(self, path: str) -> str:
        return urllib.parse.urljoin(f"{self.base_url}/", path.lstrip("/"))

    def health(self) -> None:
        payload = json.loads(_request(self.url(self.spec.health_path)))
        if payload.get("status") != "ok":
            raise HarnessError("fixture health response is not ok")

    def preflight_task(self, task: ResolvedTask) -> None:
        self.reset(task)
        page = _request(self.url(task.revision.level0.page_path)).decode("utf-8")
        if task.revision.level0.reachability_marker not in page:
            raise HarnessError(f"level-0 page omits marker for {task.revision.task_id} r{task.revision.revision}")
        checker = task.revision.level0.checker
        if checker.name == "json_endpoint_equals" and self.read_json_checker(task) == checker.expected:
            raise HarnessError("reset checker already contains the successful value")

    def reset(self, task: ResolvedTask) -> None:
        _request(self.url(task.revision.level0.reset_path), method="POST")

    def read_json_checker(self, task: ResolvedTask) -> Any:
        checker = task.revision.level0.checker
        if checker.endpoint_path is None or checker.json_field is None:
            raise HarnessError("task does not define a JSON endpoint checker")
        value: Any = json.loads(_request(self.url(checker.endpoint_path)))
        for part in checker.json_field.split("."):
            if not isinstance(value, dict) or part not in value:
                raise HarnessError(f"checker field {checker.json_field!r} is absent")
            value = value[part]
        return value

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        harness = self
        by_page = {task.revision.level0.page_path: task for task in self.tasks}
        by_reset = {task.revision.level0.reset_path: task for task in self.tasks}
        by_check = {
            task.revision.level0.checker.endpoint_path: task
            for task in self.tasks
            if task.revision.level0.checker.endpoint_path is not None
        }
        by_submit = {f"{task.revision.level0.page_path.rstrip('/')}/submit": task for task in self.tasks}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                if path == harness.spec.health_path:
                    self._json({"status": "ok"})
                elif task := by_page.get(path):
                    self._html(render_level0_page(task))
                elif task := by_check.get(path):
                    key = (task.revision.task_id, task.revision.revision)
                    self._json({"submitted_value": harness._state[key]})
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                path = urllib.parse.urlsplit(self.path).path
                if task := by_reset.get(path):
                    harness._state[(task.revision.task_id, task.revision.revision)] = None
                    self._json({"status": "reset"})
                elif task := by_submit.get(path):
                    length = int(self.headers.get("Content-Length", "0"))
                    values = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
                    harness._state[(task.revision.task_id, task.revision.revision)] = values.get("destination", [""])[0]
                    self._html(render_submission_confirmation(task))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

            def _json(self, value: object) -> None:
                self._send("application/json; charset=utf-8", json.dumps(value).encode("utf-8"))

            def _html(self, value: str) -> None:
                self._send("text/html; charset=utf-8", value.encode("utf-8"))

            def _send(self, content_type: str, body: bytes) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        return Handler


def render_level0_page(task: ResolvedTask, *, saved: bool = False) -> str:
    """Render the canonical flat control used by admission and eligible sweep reuse."""
    if saved:
        return render_submission_confirmation(task)
    level0 = task.revision.level0
    fixture = level0.fixture
    marker = html.escape(level0.reachability_marker)
    source_value = html.escape(fixture.source_value)
    source_label = html.escape(fixture.source_label)
    title = html.escape(f"Level-0 control: {task.revision.task_id} r{task.revision.revision}")
    if fixture.kind == "read_value":
        body = f"<p><strong>{source_label}</strong>: <span id=source>{source_value}</span></p>"
    else:
        action = f"{level0.page_path.rstrip('/')}/submit"
        source = f'<p><strong>{source_label}</strong>: <span id="source">{source_value}</span></p>'
        body = f"{source}{render_copy_form(task, action)}"
    style = render_fixture_style(task)
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{title}</title>{style}</head><body><main><h1>{title}</h1>{body}"
        f'<p><small data-reachability-marker="true">Control marker: {marker}</small></p>'
        "</main></body></html>"
    )


def render_submission_confirmation(task: ResolvedTask, *, condition_id: str | None = None) -> str:
    """Render terminal feedback without repeating target evidence."""

    suffix = "" if condition_id is None else f" {condition_id}"
    title = html.escape(f"Submission confirmation: {task.revision.task_id} r{task.revision.revision}{suffix}")
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<title>{title}</title></head><body><main><h1>{title}</h1>"
        "<p role='status'>Saved</p></main></body></html>"
    )


# Kept for compatibility with older tests and analysis notebooks.
_render_page = render_level0_page

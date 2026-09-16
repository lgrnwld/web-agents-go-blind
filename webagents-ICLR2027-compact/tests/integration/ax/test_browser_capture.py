from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from webagents.ax import runner
from webagents.ax.observer import capture_ax_tree
from webagents.ax.runner import _reserve_debug_port
from webagents.ax.targets import CDPConnection, TargetCollector, wait_for_debugger_url
from webagents.capture.archive import verify_run
from webagents.providers import ProviderCallResult

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("WEBAGENT_RUN_BROWSER_INTEGRATION") != "1",
        reason="set WEBAGENT_RUN_BROWSER_INTEGRATION=1 to run pinned-browser AX fixtures",
    ),
]


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        port = self.server.server_port  # type: ignore[attr-defined]
        if self.path == "/same":
            body = b'<input aria-label="SAME FRAME LABEL">'
        elif self.path == "/cross":
            body = b'<button aria-label="OOPIF LABEL"></button>'
        else:
            body = f"""
                <button aria-label="ACCESSIBLE LABEL"></button>
                <div aria-hidden="true">AX_HIDDEN_TOKEN</div>
                <canvas id="canvas"></canvas>
                <script>canvas.getContext('2d').fillText('CANVAS_ONLY_TOKEN', 0, 10)</script>
                <iframe src="/same"></iframe>
                <iframe src="http://localhost:{port}/cross"></iframe>
            """.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_raw_ax_differential_and_iframe_target_identity() -> None:
    from playwright.async_api import async_playwright

    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await async_playwright().start()
    port = _reserve_debug_port()
    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--site-per-process",
        ],
    )
    context = await browser.new_context()
    page = await context.new_page()
    connection: CDPConnection | None = None
    collector: TargetCollector | None = None
    try:
        await page.goto(f"http://127.0.0.1:{server.server_port}/")
        await page.wait_for_timeout(300)
        connection = await CDPConnection.open(await wait_for_debugger_url(port))
        collector = TargetCollector(connection)
        await collector.initialize()
        envelope, encoded, complete, missing = await capture_ax_tree(collector)
        assert complete, missing
        assert b"ACCESSIBLE LABEL" in encoded
        assert b"SAME FRAME LABEL" in encoded
        assert b"OOPIF LABEL" in encoded
        assert b"AX_HIDDEN_TOKEN" not in encoded
        assert b"CANVAS_ONLY_TOKEN" not in encoded
        assert {target["target_type"] for target in envelope["targets"]} == {"page", "iframe"}
        assert all(target["target_id"] and target["session_id"] for target in envelope["targets"])
    finally:
        if collector is not None:
            await collector.close()
        if connection is not None:
            await connection.close()
        await context.close()
        await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()


class FinishingProvider:
    provider = "openrouter"
    model = "fixture/model"

    async def invoke(self, request_bytes: bytes) -> ProviderCallResult:
        request = json.loads(request_bytes)
        assert request["messages"][1]["role"] == "user"
        observation = request["messages"][1]["content"]
        assert '"protocol_method":"Accessibility.getFullAXTree"' in observation
        return ProviderCallResult(text='{"action":"finish","answer":"ACCESSIBLE LABEL","success":true}')


@pytest.mark.asyncio
async def test_level_zero_runner_archives_and_finishes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(runner, "resolve_provider", lambda model: FinishingProvider())
    try:
        transcript = await runner._run_ax_agent(
            f"http://127.0.0.1:{server.server_port}/",
            "Read the accessible label",
            "openrouter/fixture/model",
            run_id=None,
            max_steps=1,
            archive_root=tmp_path,
            timeout_seconds=30,
        )
        assert transcript.status == "success"
        assert transcript.final.answer == "ACCESSIBLE LABEL"
        assert transcript.framework.name == "playwright"
        assert transcript.framework.commit == "chromium-1228"
        run_dir = tmp_path / "runs" / transcript.run_id
        assert (run_dir / "observations/step-000/observation.json").is_file()
        assert verify_run(run_dir) == []
    finally:
        server.shutdown()
        server.server_close()

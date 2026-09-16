"""Raw CDP transport and target/session lifecycle for AX capture."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect


class CDPError(RuntimeError):
    """A transport or protocol error from Chromium CDP."""


class CDPConnection:
    """Minimal flattened-session CDP client over Chromium's debugger socket."""

    def __init__(self, websocket: ClientConnection) -> None:
        self._websocket = websocket
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._receiver = asyncio.create_task(self._receive())

    @classmethod
    async def open(cls, websocket_url: str) -> CDPConnection:
        websocket = await connect(websocket_url, max_size=None, open_timeout=10)
        return cls(websocket)

    async def _receive(self) -> None:
        failure: BaseException | None = None
        try:
            async for raw in self._websocket:
                message = json.loads(raw)
                message_id = message.get("id")
                if not isinstance(message_id, int):
                    continue
                future = self._pending.pop(message_id, None)
                if future is None or future.done():
                    continue
                if "error" in message:
                    error = message["error"]
                    future.set_exception(CDPError(f"CDP error {error.get('code')}: {error.get('message')}"))
                else:
                    future.set_result(message.get("result", {}))
        except BaseException as exc:
            failure = exc
        finally:
            error = CDPError(f"CDP connection closed: {failure}" if failure else "CDP connection closed")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if self._receiver.done():
            raise CDPError("CDP connection is not open")
        message_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[message_id] = future
        message: dict[str, Any] = {"id": message_id, "method": method}
        if params:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        try:
            await self._websocket.send(json.dumps(message, separators=(",", ":")))
            return await future
        except BaseException:
            self._pending.pop(message_id, None)
            raise

    async def close(self) -> None:
        await self._websocket.close()
        if not self._receiver.done():
            self._receiver.cancel()
        await asyncio.gather(self._receiver, return_exceptions=True)


def _read_debugger_url(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:  # noqa: S310
        payload = json.load(response)
    url = payload.get("webSocketDebuggerUrl")
    if not isinstance(url, str):
        raise CDPError("Chromium debugger endpoint returned no webSocketDebuggerUrl")
    return url


async def wait_for_debugger_url(port: int, *, timeout_seconds: float = 10) -> str:
    """Wait for a locally launched Chromium debugger endpoint."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error: BaseException | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            return await asyncio.to_thread(_read_debugger_url, port)
        except (OSError, ValueError, CDPError) as exc:
            last_error = exc
            await asyncio.sleep(0.05)
    raise CDPError(f"Chromium debugger endpoint did not start: {last_error}")


@dataclass(frozen=True, slots=True)
class TargetHandle:
    target_id: str
    session_id: str | None
    target_type: str
    url: str
    error: str | None = None


class TargetCollector:
    """Discover and retain flattened CDP sessions for page and OOPIF targets."""

    def __init__(self, connection: CDPConnection) -> None:
        self.connection = connection
        self._sessions: dict[str, str] = {}

    async def initialize(self) -> None:
        await self.connection.send("Target.setDiscoverTargets", {"discover": True})

    async def refresh(self) -> list[TargetHandle]:
        response = await self.connection.send("Target.getTargets")
        infos = [
            info
            for info in response.get("targetInfos", [])
            if info.get("type") in {"page", "iframe"} and not str(info.get("url", "")).startswith("devtools:")
        ]
        infos.sort(key=lambda info: (str(info.get("type")), str(info.get("targetId"))))
        current_ids = {str(info.get("targetId")) for info in infos}
        for target_id in list(self._sessions):
            if target_id not in current_ids:
                self._sessions.pop(target_id, None)

        handles: list[TargetHandle] = []
        for info in infos:
            target_id = str(info.get("targetId"))
            session_id = self._sessions.get(target_id)
            error: str | None = None
            if session_id is None:
                try:
                    attached = await self.connection.send(
                        "Target.attachToTarget", {"targetId": target_id, "flatten": True}
                    )
                    session_id = str(attached["sessionId"])
                    self._sessions[target_id] = session_id
                except (CDPError, KeyError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
            handles.append(
                TargetHandle(
                    target_id=target_id,
                    session_id=session_id,
                    target_type=str(info.get("type")),
                    url=str(info.get("url", "")),
                    error=error,
                )
            )
        return handles

    async def close(self) -> None:
        for session_id in list(self._sessions.values()):
            try:
                await self.connection.send("Target.detachFromTarget", {"sessionId": session_id})
            except CDPError:
                pass
        self._sessions.clear()

    def invalidate(self, target_id: str) -> None:
        """Forget a detached/crashed session so the next snapshot reattaches it."""

        self._sessions.pop(target_id, None)

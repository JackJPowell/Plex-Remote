"""Resilient Home Assistant WebSocket state monitor."""

import asyncio
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

import websockets


logger = logging.getLogger("plex-remote.home-assistant")

CHAIR_ENTITY_ID = "binary_sensor.dads_chair_zone_stable"
BED_ENTITY_ID = "binary_sensor.dads_bed_zone_stable"
ENTITY_IDS = (CHAIR_ENTITY_ID, BED_ENTITY_ID)


def websocket_url(base_url: str) -> str:
    """Convert a Home Assistant HTTP or WebSocket URL to its API endpoint."""
    value = base_url.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path = f"{path}/api/websocket"
    return urlunparse(parsed._replace(scheme=scheme, path=path))


def api_url(base_url: str, path: str) -> str:
    """Build a Home Assistant REST API URL from its configured base URL."""
    value = base_url.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlparse(value)
    scheme = {"ws": "http", "wss": "https"}.get(parsed.scheme, parsed.scheme)
    base_path = parsed.path.rstrip("/")
    return urlunparse(parsed._replace(scheme=scheme, path=f"{base_path}{path}"))


class HomeAssistantNotifier:
    """Send mobile notifications through Home Assistant's REST API."""

    def __init__(self, base_url: str, access_token: str, notify_entity_id: str) -> None:
        self.url = api_url(base_url, "/api/services/notify/send_message")
        self.access_token = access_token.strip()
        self.notify_entity_id = notify_entity_id.strip()

    @property
    def configured(self) -> bool:
        return bool(
            self.url
            and self.access_token
            and self.notify_entity_id.startswith("notify.")
        )

    async def notify_help_requested(self) -> None:
        """Send the dashboard help request to the configured mobile device."""
        if not self.configured:
            raise RuntimeError("Home Assistant notifications are not configured")
        payload = json.dumps({
            "entity_id": self.notify_entity_id,
            "title": "Plex Remote: Help requested",
            "message": "Someone pressed the help button on the Plex Remote dashboard.",
        }).encode("utf-8")
        await asyncio.to_thread(self._send, payload)

    def _send(self, payload: bytes) -> None:
        request = Request(
            self.url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise ConnectionError(
                        f"Home Assistant notification failed with HTTP {response.status}"
                    )
        except HTTPError as exc:
            raise ConnectionError(
                f"Home Assistant notification failed with HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise ConnectionError("Could not reach Home Assistant for notification") from exc


class HomeAssistantStateMonitor:
    """Cache selected entity states from Home Assistant's WebSocket API."""

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        reconnect_max_seconds: float = 30.0,
    ) -> None:
        self.url = websocket_url(base_url)
        self.access_token = access_token.strip()
        self.reconnect_max_seconds = reconnect_max_seconds
        self.connected = False
        self.last_error: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._states: dict[str, dict[str, Any]] = {
            entity_id: {
                "state": None,
                "occupied": None,
                "last_updated": None,
            }
            for entity_id in ENTITY_IDS
        }

    @property
    def configured(self) -> bool:
        return bool(self.url and self.access_token)

    def snapshot(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "connected": self.connected,
            "last_error": self.last_error,
            "zones": {
                "chair": {
                    "entity_id": CHAIR_ENTITY_ID,
                    **self._states[CHAIR_ENTITY_ID],
                },
                "bed": {
                    "entity_id": BED_ENTITY_ID,
                    **self._states[BED_ENTITY_ID],
                },
            },
        }

    def start(self) -> None:
        if self.configured and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self.run(), name="home-assistant-monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connect_once()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.connected:
                    # A completed authenticated session means connectivity had
                    # recovered, so the next retry should be prompt again.
                    delay = 1.0
                self.last_error = str(exc)
                logger.warning("Home Assistant WebSocket disconnected: %s", exc)
            finally:
                self.connected = False

            await asyncio.sleep(delay)
            delay = min(delay * 2, self.reconnect_max_seconds)

    async def _connect_once(self) -> None:
        async with websockets.connect(
            self.url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as socket:
            await self._authenticate(socket)
            await socket.send(json.dumps({"id": 1, "type": "get_states"}))
            initial = await self._receive_result(socket, 1)
            for state in initial.get("result") or []:
                self._update_state(state)

            await socket.send(json.dumps({
                "id": 2,
                "type": "subscribe_trigger",
                "trigger": {
                    "platform": "state",
                    "entity_id": list(ENTITY_IDS),
                },
            }))
            await self._receive_result(socket, 2)
            self.connected = True
            self.last_error = None

            async for raw_message in socket:
                message = json.loads(raw_message)
                if message.get("id") != 2 or message.get("type") != "event":
                    continue
                trigger = (
                    message.get("event", {})
                    .get("variables", {})
                    .get("trigger", {})
                )
                self._update_state(trigger.get("to_state"))

        raise ConnectionError("Home Assistant closed the WebSocket connection")

    async def _authenticate(self, socket) -> None:
        request = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        if request.get("type") != "auth_required":
            raise ConnectionError("Home Assistant did not request authentication")
        await socket.send(json.dumps({"type": "auth", "access_token": self.access_token}))
        response = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        if response.get("type") != "auth_ok":
            raise PermissionError(response.get("message", "Home Assistant authentication failed"))

    async def _receive_result(self, socket, command_id: int) -> dict[str, Any]:
        while True:
            message = json.loads(await asyncio.wait_for(socket.recv(), timeout=15))
            if message.get("id") != command_id or message.get("type") != "result":
                continue
            if not message.get("success"):
                error = message.get("error", {})
                raise ConnectionError(
                    error.get("message", f"Home Assistant command {command_id} failed")
                )
            return message

    def _update_state(self, state: Any) -> None:
        if not isinstance(state, dict):
            return
        entity_id = state.get("entity_id")
        if entity_id not in self._states:
            return
        value = state.get("state")
        self._states[entity_id] = {
            "state": value,
            "occupied": value == "on" if value in ("on", "off") else None,
            "last_updated": state.get("last_updated") or datetime.now(timezone.utc).isoformat(),
        }

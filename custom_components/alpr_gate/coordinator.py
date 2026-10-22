"""Coordinator for the ALPR Gate local service."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, EVENT_ALPR_GATE

_LOGGER = logging.getLogger(__name__)


def normalize_plate(value: str | None) -> str:
    return "".join(char for char in (value or "").upper() if char.isalnum())


def display_plate(value: str | None) -> str:
    plate = normalize_plate(value)
    if len(plate) >= 5 and plate[:-4].isalpha() and plate[-4:].isdigit():
        return f"{plate[:-4]}-{plate[-4:]}"
    return plate


class AlprGateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll status/history and receive webhook events from the service."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.host = entry.data["host"]
        self.port = entry.data["port"]
        self.base_url = f"http://{self.host}:{self.port}"
        self.session = async_get_clientsession(hass)
        self.last_event_key: tuple[Any, ...] | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    async def _get(self, path: str) -> dict[str, Any]:
        async with self.session.get(f"{self.base_url}{path}", timeout=10) as response:
            if response.status != 200:
                raise UpdateFailed(f"HTTP {response.status} from {path}")
            return await response.json()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self._get("/status")
            history = await self._get("/api/events?limit=1")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(str(err)) from err

        event = (history.get("events") or [None])[0]
        if event:
            self._emit_event(
                event_id=event.get("id"),
                direction=event.get("direction"),
                plate=event.get("plate"),
                confidence=event.get("confidence"),
                camera=status.get("camera", ""),
                snapshot=f"{self.base_url}{event.get('snapshot_url', '')}" if event.get("snapshot_url") else "",
                seen_at=event.get("ts"),
            )
        return {"status": status, "event": event}

    def _emit_event(
        self,
        *,
        event_id: Any,
        direction: str | None,
        plate: str | None,
        confidence: float | None,
        camera: str | None,
        snapshot: str | None,
        seen_at: str | None,
    ) -> None:
        key = (seen_at, plate, direction)
        if key == self.last_event_key:
            return
        self.last_event_key = key
        self.hass.bus.async_fire(
            EVENT_ALPR_GATE,
            {
                "event_id": event_id,
                "direction": direction,
                "plate": display_plate(plate),
                "plate_normalized": normalize_plate(plate),
                "confidence": confidence,
                "camera": camera,
                "snapshot": snapshot or "",
                "seen_at": seen_at,
            },
        )

    async def async_handle_push(self, payload: dict[str, Any]) -> None:
        """Handle an immediate event posted by the ALPR service webhook."""
        self._emit_event(
            event_id=payload.get("track_id"),
            direction=payload.get("event"),
            plate=payload.get("plate"),
            confidence=payload.get("confidence"),
            camera=payload.get("camera"),
            snapshot=payload.get("snapshot"),
            seen_at=payload.get("seen_at"),
        )
        data = dict(self.data or {})
        status = dict(data.get("status") or {})
        status.update(
            {
                "last_plate": payload.get("plate"),
                "last_event": payload.get("event"),
                "last_seen": payload.get("seen_at"),
                "last_snapshot": payload.get("snapshot"),
            }
        )
        data["status"] = status
        data["event"] = {
            "plate": payload.get("plate"),
            "direction": payload.get("event"),
            "confidence": payload.get("confidence"),
            "ts": payload.get("seen_at"),
            "snapshot_url": payload.get("snapshot"),
        }
        self.async_set_updated_data(data)

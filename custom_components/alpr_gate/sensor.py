"""Sensors for ALPR-123."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import display_plate, normalize_plate
from .entity import AlprGateEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            AlprLastPlateSensor(coordinator, entry),
            AlprLastEventSensor(coordinator, entry),
            AlprLastSeenSensor(coordinator, entry),
            AlprEventsTodaySensor(coordinator, entry),
            AlprCameraSensor(coordinator, entry),
            AlprConfidenceSensor(coordinator, entry),
        ]
    )


class AlprSensor(AlprGateEntity, SensorEntity):
    _attr_should_poll = False


class AlprLastPlateSensor(AlprSensor):
    _attr_name = "Last plate"
    _attr_icon = "mdi:car-search"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_plate"

    @property
    def native_value(self):
        plate = self.event.get("plate") or self.status.get("last_plate")
        return display_plate(plate) or None

    @property
    def extra_state_attributes(self):
        plate = self.event.get("plate") or self.status.get("last_plate")
        return {
            "plate_normalized": normalize_plate(plate),
            "direction": self.event.get("direction") or self.status.get("last_event"),
            "confidence": self.event.get("confidence"),
            "camera": self.event.get("camera") or self.status.get("camera"),
            "snapshot": self.event.get("snapshot") or self.status.get("last_snapshot"),
            "seen_at": self.event.get("seen_at") or self.event.get("ts") or self.status.get("last_seen"),
        }


class AlprLastEventSensor(AlprSensor):
    _attr_name = "Last event"
    _attr_icon = "mdi:car-arrow-right"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_event"

    @property
    def native_value(self):
        return self.event.get("direction") or self.status.get("last_event")

    @property
    def icon(self):
        return "mdi:car-arrow-left" if self.native_value == "exit" else "mdi:car-arrow-right"


class AlprLastSeenSensor(AlprSensor):
    _attr_name = "Last seen"
    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_last_seen"

    @property
    def native_value(self) -> datetime | None:
        value = self.event.get("seen_at") or self.event.get("ts") or self.status.get("last_seen")
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None


class AlprEventsTodaySensor(AlprSensor):
    _attr_name = "Events today"
    _attr_icon = "mdi:counter"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_events_today"

    @property
    def native_value(self):
        return int(self.status.get("events_today") or 0)


class AlprCameraSensor(AlprSensor):
    _attr_name = "Camera"
    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_camera"

    @property
    def native_value(self):
        return self.status.get("camera_label") or self.status.get("camera")

    @property
    def extra_state_attributes(self):
        return {
            "camera_id": self.status.get("camera"),
            "fps": self.status.get("fps"),
            "last_frame_age": self.status.get("last_frame_age"),
            "motion_diff": self.status.get("motion_diff"),
        }


class AlprConfidenceSensor(AlprSensor):
    _attr_name = "Last confidence"
    _attr_icon = "mdi:gauge"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_confidence"

    @property
    def native_value(self):
        value = self.event.get("confidence")
        return round(float(value) * 100, 1) if value is not None else None

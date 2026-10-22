"""Binary sensors for ALPR-123."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import AlprGateEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [AlprCameraOnlineSensor(coordinator, entry), AlprServiceOnlineSensor(coordinator, entry)]
    )


class AlprCameraOnlineSensor(AlprGateEntity, BinarySensorEntity):
    _attr_name = "Camera online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_camera_online"

    @property
    def is_on(self):
        return bool(self.status.get("camera_ok"))


class AlprServiceOnlineSensor(AlprGateEntity, BinarySensorEntity):
    _attr_name = "Service online"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_service_online"

    @property
    def is_on(self):
        return self.status.get("state") == "running"

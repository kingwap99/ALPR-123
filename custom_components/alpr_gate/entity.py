"""Shared entities for ALPR-123."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_NAME
from .coordinator import AlprGateCoordinator


class AlprGateEntity(CoordinatorEntity[AlprGateCoordinator]):
    """Base entity with a shared device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AlprGateCoordinator, entry) -> None:
        super().__init__(coordinator)
        self.entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=INTEGRATION_NAME,
            manufacturer=INTEGRATION_NAME,
            model="Local ALPR service",
            sw_version="0.3.0",
            configuration_url=f"http://{coordinator.host}:{coordinator.port}/",
        )

    @property
    def status(self) -> dict:
        return (self.coordinator.data or {}).get("status") or {}

    @property
    def event(self) -> dict:
        return (self.coordinator.data or {}).get("event") or {}

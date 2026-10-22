"""ALPR-123 integration."""

from __future__ import annotations

import logging

import aiohttp
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, INTEGRATION_NAME, PLATFORMS
from .coordinator import AlprGateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host = entry.data.get("host")
    if host and entry.title != f"{INTEGRATION_NAME} ({host})":
        hass.config_entries.async_update_entry(
            entry, title=f"{INTEGRATION_NAME} ({host})"
        )

    coordinator = AlprGateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    webhook_id = entry.data.get("webhook_id")
    if webhook_id:
        async def _handle_webhook(hass: HomeAssistant, webhook_id: str, request):
            try:
                payload = await request.json()
            except Exception:
                return web.Response(status=400)
            await coordinator.async_handle_push(payload)
            return web.Response(text="OK")

        try:
            webhook.async_register(
                hass,
                DOMAIN,
                entry.title,
                webhook_id,
                _handle_webhook,
                allowed_methods=["POST"],
                local_only=True,
            )
            entry.async_on_unload(lambda: webhook.async_unregister(hass, webhook_id))
        except Exception as err:
            _LOGGER.warning("Could not register ALPR-123 webhook: %s", err)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear the service webhook when the integration is removed."""
    host = entry.data.get("host")
    port = entry.data.get("port")
    if host and port:
        try:
            session = async_get_clientsession(hass)
            async with session.delete(f"http://{host}:{port}/api/integration/webhook", timeout=5):
                pass
        except (aiohttp.ClientError, TimeoutError):
            pass

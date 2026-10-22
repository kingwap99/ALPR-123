"""Config flow for ALPR-123."""

from __future__ import annotations

import logging
import secrets
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DEFAULT_PORT, DOMAIN, INTEGRATION_NAME

_LOGGER = logging.getLogger(__name__)


class AlprGateConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add an ALPR-123 service to Home Assistant."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input["host"].strip()
            port = int(user_input["port"])
            if not host:
                errors["base"] = "invalid_host"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    async with session.get(f"http://{host}:{port}/status", timeout=10) as response:
                        if response.status != 200:
                            raise ValueError(f"HTTP {response.status}")
                        await response.json()
                except (aiohttp.ClientError, TimeoutError, ValueError):
                    errors["base"] = "cannot_connect"

            if not errors:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                webhook_id = f"alpr_gate_{secrets.token_hex(12)}"
                webhook_enabled = False
                try:
                    ha_url = self._ha_url()
                    if ha_url:
                        async with session.post(
                            f"http://{host}:{port}/api/integration/webhook",
                            json={"url": f"{ha_url}/api/webhook/{webhook_id}"},
                            timeout=10,
                        ) as response:
                            webhook_enabled = response.status == 200
                except (aiohttp.ClientError, TimeoutError, NoURLAvailableError):
                    webhook_enabled = False
                return self.async_create_entry(
                    title=f"{INTEGRATION_NAME} ({host})",
                    data={
                        "host": host,
                        "port": port,
                        "webhook_id": webhook_id,
                        "webhook_enabled": webhook_enabled,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("host", default="192.168.1.10"): str,
                    vol.Required("port", default=DEFAULT_PORT): int,
                }
            ),
            errors=errors,
        )

    def _ha_url(self) -> str | None:
        """Return an HA URL reachable from the ALPR host."""
        try:
            return get_url(self.hass)
        except (NoURLAvailableError, TypeError):
            pass
        return self.hass.config.internal_url or self.hass.config.external_url

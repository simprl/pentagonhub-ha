"""Diagnostics for PentagonHub HA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_CODE, CONF_INSTALLATION_TOKEN, DOMAIN

TO_REDACT = {
    CONF_DEVICE_CODE,
    CONF_INSTALLATION_TOKEN,
    "authorization",
    "device_code",
    "installation_token",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    coordinator_data = getattr(getattr(runtime, "coordinator", None), "data", None)

    return async_redact_data(
        {
            "entry": {
                "title": entry.title,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "coordinator": {
                "last_update_success": getattr(
                    getattr(runtime, "coordinator", None),
                    "last_update_success",
                    None,
                ),
                "data": coordinator_data,
            },
        },
        TO_REDACT,
    )

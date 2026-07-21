"""Home Assistant metadata helpers for PentagonHub Core payloads."""

from __future__ import annotations

from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import CONF_HA_BASE_URL, INTEGRATION_VERSION


def build_ha_metadata(
    hass: HomeAssistant,
    ha_instance_id: str,
    *,
    ha_base_url: str | None = None,
) -> dict[str, Any]:
    """Build non-secret HA metadata for pairing and heartbeat payloads."""

    payload: dict[str, Any] = {
        "ha_instance_id": ha_instance_id,
        "ha_version": HA_VERSION,
        "integration_version": INTEGRATION_VERSION,
    }

    if hass.config.time_zone:
        payload["timezone"] = str(hass.config.time_zone)

    if ha_base_url:
        payload["base_url"] = ha_base_url

    return payload


def build_ha_metadata_from_entry_data(
    hass: HomeAssistant,
    entry_data: dict[str, Any],
) -> dict[str, Any]:
    """Build metadata from a config entry data payload."""

    return build_ha_metadata(
        hass,
        str(entry_data["ha_instance_id"]),
        ha_base_url=entry_data.get(CONF_HA_BASE_URL),
    )

"""Sensors for PentagonHub HA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CONNECTION_STATE,
    ATTR_INSTALLATION_ID,
    ATTR_PAIRING_STATE,
    ATTR_SERVER_TIME,
    CONF_INSTALLATION_ID,
    DOMAIN,
    NAME,
)
from .coordinator import PentagonHubDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PentagonHub HA sensors."""

    coordinator: PentagonHubDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id].coordinator
    runtime = hass.data[DOMAIN][entry.entry_id].sandbox
    entities: list[SensorEntity] = [PentagonHubConnectionSensor(coordinator, entry)]
    if runtime is not None:
        from .sandbox_entities import PentagonHubSandboxSensor

        entities.extend(
            [
                PentagonHubSandboxSensor(hass, runtime, entity)
                for entity in runtime.entities_for_domain("sensor")
            ]
        )
    async_add_entities(entities)


class PentagonHubConnectionSensor(CoordinatorEntity[PentagonHubDataUpdateCoordinator], SensorEntity):
    """Expose the current PentagonHub connection state."""

    _attr_has_entity_name = True
    _attr_name = "Connection"

    def __init__(self, coordinator: PentagonHubDataUpdateCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connection"

    @property
    def native_value(self) -> str:
        """Return the current connection state."""

        data = self.coordinator.data or {}
        if not self.coordinator.last_update_success:
            return "unavailable"
        return str(data.get(ATTR_CONNECTION_STATE, "unknown"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional connection attributes."""

        data = self.coordinator.data or {}
        return {
            ATTR_INSTALLATION_ID: data.get(ATTR_INSTALLATION_ID) or self._entry.data.get(CONF_INSTALLATION_ID),
            ATTR_PAIRING_STATE: data.get(ATTR_PAIRING_STATE),
            ATTR_SERVER_TIME: data.get(ATTR_SERVER_TIME),
        }

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device metadata."""

        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": NAME,
            "manufacturer": "PentagonHub",
        }

"""Runtime coordinator for PentagonHub HA."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PentagonHubApiClient, PentagonHubApiError
from .commands import async_execute_command
from .const import (
    CONF_API_BASE_URL,
    CONF_HEARTBEAT_INTERVAL,
    CONF_INSTALLATION_TOKEN,
    DEFAULT_HEARTBEAT_INTERVAL,
    DOMAIN,
)
from .metadata import build_ha_metadata_from_entry_data

_LOGGER = logging.getLogger(__name__)


class PentagonHubDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch and expose PentagonHub HA connection state."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self._client = PentagonHubApiClient(
            async_get_clientsession(hass),
            entry.data[CONF_API_BASE_URL],
        )
        self._command_lock = asyncio.Lock()
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=int(entry.options.get(CONF_HEARTBEAT_INTERVAL, DEFAULT_HEARTBEAT_INTERVAL))
            ),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        payload = build_ha_metadata_from_entry_data(self.hass, dict(self.entry.data))
        try:
            heartbeat = await self._client.send_heartbeat(
                self.entry.data[CONF_INSTALLATION_TOKEN],
                payload,
            )
            await self._async_poll_command()
            return heartbeat
        except PentagonHubApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_poll_command(self) -> None:
        if self._command_lock.locked():
            return

        async with self._command_lock:
            response = await self._client.next_command(self.entry.data[CONF_INSTALLATION_TOKEN])
            command = response.get("command")
            if isinstance(command, dict):
                await async_execute_command(
                    self.hass,
                    self._client,
                    self.entry.data[CONF_INSTALLATION_TOKEN],
                    command,
                )

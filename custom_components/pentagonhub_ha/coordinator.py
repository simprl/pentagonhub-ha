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
        self._event_task: asyncio.Task[None] | None = None
        self._event_stop = asyncio.Event()
        self._commands_started = False
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
            if self._commands_started:
                await self._async_drain_commands()
            return heartbeat
        except PentagonHubApiError as err:
            raise UpdateFailed(str(err)) from err

    def async_start_event_stream(self) -> None:
        """Start the outbound Core notification stream."""

        if self._event_task is not None and not self._event_task.done():
            return
        self._event_stop.clear()
        self._commands_started = True
        self._event_task = self.hass.async_create_background_task(
            self._async_run_event_stream(),
            f"{DOMAIN}-{self.entry.entry_id}-events",
        )

    async def async_stop_event_stream(self) -> None:
        """Stop the outbound Core notification stream."""

        self._event_stop.set()
        self._commands_started = False
        task = self._event_task
        self._event_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _async_run_event_stream(self) -> None:
        retry_delay = 1
        token = self.entry.data[CONF_INSTALLATION_TOKEN]
        while not self._event_stop.is_set():
            try:
                async for event_type, _data in self._client.command_events(token):
                    retry_delay = 1
                    if event_type in {"connected", "commands_available"}:
                        await self._async_drain_commands()
                if not self._event_stop.is_set():
                    _LOGGER.warning("PentagonHub event stream closed; reconnecting")
            except asyncio.CancelledError:
                raise
            except PentagonHubApiError as err:
                _LOGGER.warning("PentagonHub event stream unavailable: %s", err)
            except Exception:
                _LOGGER.exception("Unexpected PentagonHub event stream failure")

            try:
                await asyncio.wait_for(self._event_stop.wait(), timeout=retry_delay)
            except TimeoutError:
                pass
            retry_delay = min(retry_delay * 2, 60)

    async def _async_drain_commands(self) -> None:
        if self._command_lock.locked():
            return

        async with self._command_lock:
            while True:
                response = await self._client.next_command(
                    self.entry.data[CONF_INSTALLATION_TOKEN]
                )
                command = response.get("command")
                if not isinstance(command, dict):
                    return
                await async_execute_command(
                    self.hass,
                    self.entry,
                    self._client,
                    self.entry.data[CONF_INSTALLATION_TOKEN],
                    command,
                )

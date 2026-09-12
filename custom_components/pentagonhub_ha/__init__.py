"""PentagonHub HA integration."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .build_profile import BUILD_PROFILE
from .const import (
    BOOTSTRAP_FILE_NAME,
    CONF_INSTALLATION_MODE,
    DOMAIN,
    STORAGE_DIR_NAME,
)
from .coordinator import PentagonHubDataUpdateCoordinator
from .entry_mode import resolve_installation_mode
from .status_view import PentagonHubStatusView

BASE_PLATFORMS: tuple[Platform, ...] = (Platform.SENSOR,)
DEV_PLATFORMS: tuple[Platform, ...] = (
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.TEXT,
    Platform.UPDATE,
    Platform.CLIMATE,
    Platform.MEDIA_PLAYER,
    Platform.ALARM_CONTROL_PANEL,
)
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PentagonHubEntryRuntime:
    """Runtime objects for a PentagonHub HA config entry."""

    coordinator: PentagonHubDataUpdateCoordinator
    sandbox: Any | None
    platforms: tuple[Platform, ...]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the PentagonHub HA domain."""

    hass.data.setdefault(DOMAIN, {})
    if getattr(hass, "http", None) is not None:
        hass.http.register_view(PentagonHubStatusView)
    await _async_start_managed_bootstrap(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PentagonHub HA from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    await hass.async_add_executor_job(_ensure_storage_dir, Path(hass.config.path(STORAGE_DIR_NAME)))

    coordinator = PentagonHubDataUpdateCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        raise
    except Exception as err:
        raise ConfigEntryNotReady(f"PentagonHub Core is not reachable: {err}") from err

    try:
        installation_mode = resolve_installation_mode(
            entry.data.get(CONF_INSTALLATION_MODE),
            BUILD_PROFILE,
        )
    except ValueError as err:
        raise ConfigEntryNotReady(str(err)) from err
    if installation_mode != BUILD_PROFILE:
        raise ConfigEntryNotReady(
            f"PentagonHub HA {installation_mode.title()} installation requires "
            f"the {installation_mode.title()} integration artifact"
        )
    is_dev = installation_mode == "dev"

    sandbox = None
    platforms = BASE_PLATFORMS
    if is_dev:
        from .modbus_sandbox_client import async_restore_modbus_manifest
        from .sandbox_runtime import async_load_sandbox_runtime
        from .sandbox_services import async_register_sandbox_services

        try:
            await async_restore_modbus_manifest(hass, Path(hass.config.path()))
        except ValueError as err:
            raise ConfigEntryNotReady(
                f"PentagonHub Modbus sandbox is not reachable: {err}"
            ) from err
        async_register_sandbox_services(hass)
        sandbox = await async_load_sandbox_runtime(hass, entry.entry_id)
        platforms = (*BASE_PLATFORMS, *DEV_PLATFORMS)

    hass.data[DOMAIN][entry.entry_id] = PentagonHubEntryRuntime(
        coordinator=coordinator,
        sandbox=sandbox,
        platforms=platforms,
    )
    try:
        await hass.config_entries.async_forward_entry_setups(entry, platforms)
    except Exception:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        raise
    await _async_start_event_stream_after_startup(hass, entry)
    return True


async def _async_start_event_stream_after_startup(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Start command transport outside Home Assistant's startup phase."""

    async def start_event_stream(_: Event | None = None) -> None:
        runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if runtime is not None:
            runtime.coordinator.async_start_event_stream()

    if hass.state == CoreState.running:
        await start_event_stream()
        return

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start_event_stream)
    )


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate legacy config entries created before release profiles existed."""

    if entry.version > 2:
        _LOGGER.error(
            "Cannot migrate PentagonHub HA config entry from unsupported version %s",
            entry.version,
        )
        return False

    if entry.version == 2:
        return True

    try:
        installation_mode = resolve_installation_mode(
            entry.data.get(CONF_INSTALLATION_MODE),
            BUILD_PROFILE,
        )
    except ValueError as err:
        _LOGGER.error("Cannot migrate PentagonHub HA config entry: %s", err)
        return False

    data = dict(entry.data)
    data[CONF_INSTALLATION_MODE] = installation_mode
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    _LOGGER.info(
        "Migrated PentagonHub HA config entry to version 2 with %s mode",
        installation_mode,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PentagonHub HA config entry."""

    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    platforms = runtime.platforms if runtime else BASE_PLATFORMS
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        if runtime:
            await runtime.coordinator.async_stop_event_stream()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


def _ensure_storage_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


async def _async_start_managed_bootstrap(hass: HomeAssistant) -> None:
    """Start config-entry import from a managed bootstrap file when present."""

    if hass.config_entries.async_entries(DOMAIN):
        return

    bootstrap_path = Path(hass.config.path(STORAGE_DIR_NAME, BOOTSTRAP_FILE_NAME))
    bootstrap = await hass.async_add_executor_job(_pop_bootstrap_file, bootstrap_path)
    if not bootstrap:
        return

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=bootstrap,
        )
    )


def _pop_bootstrap_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        _LOGGER.warning("Could not read PentagonHub HA bootstrap file: %s", err)
        return None
    try:
        path.unlink()
    except OSError as err:
        _LOGGER.warning("Could not remove PentagonHub HA bootstrap file: %s", err)
    return data if isinstance(data, dict) else None

"""PentagonHub HA integration."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import BOOTSTRAP_FILE_NAME, DOMAIN, STORAGE_DIR_NAME
from .coordinator import PentagonHubDataUpdateCoordinator
from .status_view import PentagonHubStatusView

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PentagonHubEntryRuntime:
    """Runtime objects for a PentagonHub HA config entry."""

    coordinator: PentagonHubDataUpdateCoordinator


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

    hass.data[DOMAIN][entry.entry_id] = PentagonHubEntryRuntime(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PentagonHub HA config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
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

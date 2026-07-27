"""Config flow for PentagonHub HA."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.instance_id import async_get as async_get_instance_id

from .api import PentagonHubApiClient, PentagonHubApiError, normalize_api_base_url, normalize_optional_url
from .const import (
    CONF_API_BASE_URL,
    CONF_HA_BASE_URL,
    CONF_HA_INSTANCE_ID,
    CONF_HEARTBEAT_INTERVAL,
    CONF_INSTALLATION_ID,
    CONF_INSTALLATION_MODE,
    CONF_INSTALLATION_TOKEN,
    DEFAULT_API_BASE_URL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DOMAIN,
    MAX_HEARTBEAT_INTERVAL,
    MIN_HEARTBEAT_INTERVAL,
    NAME,
    PAIRING_STATUS_APPROVED,
    PAIRING_STATUS_AUTHORIZATION_PENDING,
    PAIRING_STATUS_EXPIRED,
    PAIRING_STATUS_REJECTED,
)
from .metadata import build_ha_metadata

_LOGGER = logging.getLogger(__name__)


class PentagonHubHaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the PentagonHub HA config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._api_base_url: str | None = None
        self._ha_base_url: str | None = None
        self._ha_instance_id: str | None = None
        self._pairing: dict[str, Any] | None = None
        self._device_code: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""

        return PentagonHubHaOptionsFlow(config_entry)

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Start HA-first device-code pairing."""

        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                self._api_base_url = normalize_api_base_url(user_input[CONF_API_BASE_URL])
                self._ha_base_url = normalize_optional_url(user_input.get(CONF_HA_BASE_URL))
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                try:
                    self._ha_instance_id = await async_get_instance_id(self.hass)
                    await self.async_set_unique_id(self._ha_instance_id)
                    if self.source == config_entries.SOURCE_REAUTH:
                        self._abort_if_unique_id_mismatch()
                    else:
                        self._abort_if_unique_id_configured()

                    client = PentagonHubApiClient(
                        async_get_clientsession(self.hass),
                        self._api_base_url,
                    )
                    self._pairing = await client.create_pairing_session(
                        build_ha_metadata(
                            self.hass,
                            self._ha_instance_id,
                            ha_base_url=self._ha_base_url,
                        ),
                    )
                    self._device_code = _required_string(self._pairing, "device_code")
                except PentagonHubApiError as err:
                    _LOGGER.warning("Could not create PentagonHub pairing session: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    return await self.async_step_approve()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_BASE_URL, default=DEFAULT_API_BASE_URL): str,
                    vol.Optional(CONF_HA_BASE_URL, default=""): str,
                }
            ),
            errors=errors,
        )

    async def async_step_approve(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Wait for the user to approve the pairing request in PentagonHub Center."""

        if not self._pairing or not self._device_code or not self._api_base_url or not self._ha_instance_id:
            return await self.async_step_user()

        errors: dict[str, str] = {}

        if user_input is not None:
            client = PentagonHubApiClient(async_get_clientsession(self.hass), self._api_base_url)
            try:
                result = await client.exchange_pairing_token(self._device_code)
            except PentagonHubApiError as err:
                _LOGGER.warning("Could not exchange PentagonHub pairing token: %s", err)
                errors["base"] = "cannot_connect"
            else:
                status = result.get("status")
                if status == PAIRING_STATUS_APPROVED:
                    data = {
                        CONF_API_BASE_URL: _required_string(result, "api_base_url"),
                        CONF_HA_BASE_URL: self._ha_base_url,
                        CONF_HA_INSTANCE_ID: self._ha_instance_id,
                        CONF_INSTALLATION_ID: _required_string(result, "installation_id"),
                        CONF_INSTALLATION_MODE: _required_string(result, "installation_mode"),
                        CONF_INSTALLATION_TOKEN: _required_string(result, "installation_token"),
                    }
                    if self.source == config_entries.SOURCE_REAUTH:
                        return self.async_update_reload_and_abort(
                            self._get_reauth_entry(),
                            data_updates=data,
                        )
                    return self.async_create_entry(title=NAME, data=data)
                if status == PAIRING_STATUS_AUTHORIZATION_PENDING:
                    errors["base"] = "authorization_pending"
                elif status == PAIRING_STATUS_EXPIRED:
                    return self.async_abort(reason="pairing_expired")
                elif status == PAIRING_STATUS_REJECTED:
                    return self.async_abort(reason="pairing_rejected")
                else:
                    errors["base"] = "invalid_response"

        return self.async_show_form(
            step_id="approve",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "user_code": _required_string(self._pairing, "user_code"),
                "verification_uri_complete": _required_string(
                    self._pairing,
                    "verification_uri_complete",
                ),
                "expires_in": str(self._pairing.get("expires_in", "")),
            },
        )

    async def async_step_import(
        self,
        import_config: dict[str, Any],
    ):
        """Import a managed bootstrap pairing file."""

        try:
            self._api_base_url = normalize_api_base_url(import_config[CONF_API_BASE_URL])
            self._ha_base_url = normalize_optional_url(import_config.get(CONF_HA_BASE_URL))
            bootstrap_token = _required_string(import_config, "bootstrap_token")
            self._ha_instance_id = await async_get_instance_id(self.hass)
            await self.async_set_unique_id(self._ha_instance_id)
            self._abort_if_unique_id_configured()
            client = PentagonHubApiClient(
                async_get_clientsession(self.hass),
                self._api_base_url,
            )
            result = await client.exchange_bootstrap_token(
                {
                    **build_ha_metadata(
                        self.hass,
                        self._ha_instance_id,
                        ha_base_url=self._ha_base_url,
                    ),
                    "bootstrap_token": bootstrap_token,
                }
            )
        except (KeyError, ValueError, PentagonHubApiError) as err:
            _LOGGER.warning("Could not import PentagonHub managed bootstrap: %s", err)
            return self.async_abort(reason="cannot_connect")

        status = result.get("status")
        if status != PAIRING_STATUS_APPROVED:
            return self.async_abort(reason="pairing_rejected")

        data = {
            CONF_API_BASE_URL: _required_string(result, "api_base_url"),
            CONF_HA_BASE_URL: self._ha_base_url,
            CONF_HA_INSTANCE_ID: self._ha_instance_id,
            CONF_INSTALLATION_ID: _required_string(result, "installation_id"),
            CONF_INSTALLATION_MODE: _required_string(result, "installation_mode"),
            CONF_INSTALLATION_TOKEN: _required_string(result, "installation_token"),
        }
        return self.async_create_entry(title=NAME, data=data)

    async def async_step_reauth(
        self,
        entry_data: dict[str, Any],
    ):
        """Run pairing again to replace a revoked or expired installation token."""

        self._api_base_url = entry_data.get(CONF_API_BASE_URL)
        self._ha_base_url = entry_data.get(CONF_HA_BASE_URL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Ask the user to confirm reauthentication."""

        if user_input is not None:
            return await self.async_step_user(
                {
                    CONF_API_BASE_URL: self._api_base_url or DEFAULT_API_BASE_URL,
                    CONF_HA_BASE_URL: self._ha_base_url or "",
                }
            )

        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Reconfigure non-secret endpoint settings."""

        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                api_base_url = normalize_api_base_url(user_input[CONF_API_BASE_URL])
                ha_base_url = normalize_optional_url(user_input.get(CONF_HA_BASE_URL))
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                await self.async_set_unique_id(entry.data[CONF_HA_INSTANCE_ID])
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_API_BASE_URL: api_base_url,
                        CONF_HA_BASE_URL: ha_base_url,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_API_BASE_URL,
                        default=entry.data.get(CONF_API_BASE_URL, DEFAULT_API_BASE_URL),
                    ): str,
                    vol.Optional(CONF_HA_BASE_URL, default=entry.data.get(CONF_HA_BASE_URL) or ""): str,
                }
            ),
            errors=errors,
        )


class PentagonHubHaOptionsFlow(config_entries.OptionsFlowWithReload):
    """Handle options for PentagonHub HA."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ):
        """Update heartbeat options."""

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        heartbeat_interval = self._config_entry.options.get(
            CONF_HEARTBEAT_INTERVAL,
            DEFAULT_HEARTBEAT_INTERVAL,
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HEARTBEAT_INTERVAL, default=heartbeat_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_HEARTBEAT_INTERVAL, max=MAX_HEARTBEAT_INTERVAL),
                    )
                }
            ),
        )


def _required_string(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise PentagonHubApiError(f"PentagonHub Core response is missing {key}")
    return value

"""Local authenticated status endpoint for PentagonHub HA."""

from __future__ import annotations

from typing import Any

from homeassistant.components.http import HomeAssistantView

from .const import CONF_INSTALLATION_ID, DOMAIN


class PentagonHubStatusView(HomeAssistantView):
    """Expose a redacted local status view for authenticated HA users."""

    url = "/api/pentagonhub_ha/status"
    name = "api:pentagonhub_ha:status"
    requires_auth = True

    async def get(self, request):
        """Return current integration status."""

        hass = request.app["hass"]
        entries = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            coordinator = getattr(runtime, "coordinator", None)
            data: dict[str, Any] = getattr(coordinator, "data", None) or {}
            entries.append(
                {
                    "entry_id": entry.entry_id,
                    "title": entry.title,
                    "installation_id": data.get("installation_id")
                    or entry.data.get(CONF_INSTALLATION_ID),
                    "pairing_state": data.get("pairing_state"),
                    "connection_state": data.get("connection_state"),
                    "server_time": data.get("server_time"),
                    "last_update_success": getattr(coordinator, "last_update_success", None),
                }
            )

        return self.json({"entries": entries})

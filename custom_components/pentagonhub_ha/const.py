"""Constants for the PentagonHub HA integration."""

from __future__ import annotations

DOMAIN = "pentagonhub_ha"
NAME = "PentagonHub HA"

INTEGRATION_VERSION = "0.1.0"
PROTOCOL_VERSION = 1

DEFAULT_API_BASE_URL = "https://center.pentagonhub.com/api"
DEFAULT_HEARTBEAT_INTERVAL = 60
MIN_HEARTBEAT_INTERVAL = 15
MAX_HEARTBEAT_INTERVAL = 300

CONF_API_BASE_URL = "api_base_url"
CONF_DEVICE_CODE = "device_code"
CONF_HA_BASE_URL = "ha_base_url"
CONF_HA_INSTANCE_ID = "ha_instance_id"
CONF_INSTALLATION_ID = "installation_id"
CONF_INSTALLATION_TOKEN = "installation_token"
CONF_HEARTBEAT_INTERVAL = "heartbeat_interval"

STORAGE_DIR_NAME = ".pentagonhub_ha"
BOOTSTRAP_FILE_NAME = "bootstrap.json"

ATTR_CONNECTION_STATE = "connection_state"
ATTR_INSTALLATION_ID = "installation_id"
ATTR_PAIRING_STATE = "pairing_state"
ATTR_SERVER_TIME = "server_time"

PAIRING_STATUS_APPROVED = "approved"
PAIRING_STATUS_AUTHORIZATION_PENDING = "authorization_pending"
PAIRING_STATUS_EXPIRED = "expired"
PAIRING_STATUS_REJECTED = "rejected"

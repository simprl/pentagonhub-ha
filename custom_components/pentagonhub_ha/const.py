"""Constants for the PentagonHub HA integration."""

from __future__ import annotations

DOMAIN = "pentagonhub_ha"
NAME = "PentagonHub"

INTEGRATION_VERSION = "0.2.7"
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
CONF_INSTALLATION_MODE = "installation_mode"
CONF_INSTALLATION_TOKEN = "installation_token"
CONF_HEARTBEAT_INTERVAL = "heartbeat_interval"

STORAGE_DIR_NAME = ".pentagonhub_ha"
BOOTSTRAP_FILE_NAME = "bootstrap.json"
SANDBOX_DIR_NAME = "sandbox"
SANDBOX_MANIFEST_FILE_NAME = "runtime.json"
SANDBOX_STATE_FILE_NAME = "state.json"
SANDBOX_STATUS_FILE_NAME = "status.json"
SANDBOX_SCHEMA_VERSION = "sandbox_runtime_v1"
SANDBOX_UNIQUE_ID_PREFIX = "ph_sandbox:"
SANDBOX_MARKER_ATTRIBUTE = "pentagonhub_sandbox_marker"

SERVICE_SET_SANDBOX_ENTITY = "set_sandbox_entity"
SERVICE_RESET_SANDBOX_ENTITY = "reset_sandbox_entity"
SERVICE_RESET_SANDBOX = "reset_sandbox"
SERVICE_ADVANCE_SANDBOX_TIME = "advance_sandbox_time"

ATTR_CONNECTION_STATE = "connection_state"
ATTR_INSTALLATION_ID = "installation_id"
ATTR_PAIRING_STATE = "pairing_state"
ATTR_SERVER_TIME = "server_time"

PAIRING_STATUS_APPROVED = "approved"
PAIRING_STATUS_AUTHORIZATION_PENDING = "authorization_pending"
PAIRING_STATUS_EXPIRED = "expired"
PAIRING_STATUS_REJECTED = "rejected"

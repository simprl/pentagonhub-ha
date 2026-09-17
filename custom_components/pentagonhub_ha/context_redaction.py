"""Redaction helpers for Home Assistant context exports."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

REDACTED = "<redacted>"
REDACTED_URL = "<redacted-url>"

_SECRET_KEY_PARTS = (
    "access_key",
    "access_token",
    "apikey",
    "api_key",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "id_token",
    "password",
    "passwd",
    "private_key",
    "refresh_token",
    "secret",
    "session_token",
    "signature",
    "token",
    "webhook_token",
)
_SECRET_KEY_EXACT = {"auth", "cookie"}
_LOCATION_KEYS = {
    "address",
    "formatted_address",
    "gps_accuracy",
    "latitude",
    "location_accuracy",
    "longitude",
}
_NETWORK_KEYS = {
    "bssid",
    "bt_address",
    "connections",
    "host",
    "hostname",
    "host_name",
    "identifiers",
    "ieee",
    "ip_address",
    "mac",
    "mac_address",
    "serial",
    "serial_number",
    "ssid",
}
_SECRET_LABEL_PARTS = (
    "access_token",
    "api_key",
    "auth",
    "bearer",
    "client_secret",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)
_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^/?#\s@]+@)", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(
    r"([?&](?:access_token|auth|id_token|key|password|refresh_token|signature|sig|token|api_key)=)[^&#\s]+",
    re.IGNORECASE,
)
_SECRET_FRAGMENT_RE = re.compile(
    r"#.*(?:access_token|auth|id_token|password|refresh_token|token)=",
    re.IGNORECASE,
)
_WEBHOOK_URL_RE = re.compile(r"^https?://\S*/(?:api/)?webhook/", re.IGNORECASE)
_BEARER_VALUE_RE = re.compile(r"\bbearer\s+[a-z0-9._~+/=-]+", re.IGNORECASE)
_JWT_VALUE_RE = re.compile(r"\beyJ[a-z0-9_-]*\.[a-z0-9_-]+\.[a-z0-9_-]+\b", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


@dataclass(slots=True)
class RedactionStats:
    """Non-secret counters included in context metadata."""

    redacted_keys: int = 0
    redacted_values: int = 0
    redacted_urls: int = 0
    redacted_locations: int = 0
    redacted_network_identifiers: int = 0
    redacted_entity_states: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "redacted_keys": self.redacted_keys,
            "redacted_values": self.redacted_values,
            "redacted_urls": self.redacted_urls,
            "redacted_locations": self.redacted_locations,
            "redacted_network_identifiers": self.redacted_network_identifiers,
            "redacted_entity_states": self.redacted_entity_states,
        }


def sanitize_secret_json(value: Any, stats: RedactionStats | None = None) -> Any:
    """Remove token/password-like data while preserving functional values."""

    return _sanitize_json(value, stats or RedactionStats(), privacy=False)


def sanitize_context_json(value: Any, stats: RedactionStats | None = None) -> Any:
    """Sanitize a git-safe HA context export."""

    return _sanitize_json(value, stats or RedactionStats(), privacy=True)


def _sanitize_json(value: Any, stats: RedactionStats, *, privacy: bool, key: str | None = None) -> Any:
    if key and _is_secret_key(key):
        stats.redacted_keys += 1
        return REDACTED
    if privacy and key and _is_location_key(key):
        stats.redacted_locations += 1
        return REDACTED
    if privacy and key and _is_network_key(key):
        stats.redacted_network_identifiers += 1
        return REDACTED

    if isinstance(value, str):
        return _sanitize_string(value, stats)
    if isinstance(value, dict):
        sanitized = {
            str(item_key): _sanitize_json(
                item_value,
                stats,
                privacy=privacy,
                key=str(item_key),
            )
            for item_key, item_value in value.items()
        }
        if privacy:
            _redact_state_if_label_requests_secret(sanitized, stats)
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_json(item, stats, privacy=privacy)
            for item in value
        ]
    return value


def _sanitize_string(value: str, stats: RedactionStats) -> str:
    if _PRIVATE_KEY_RE.search(value) or _BEARER_VALUE_RE.search(value) or _JWT_VALUE_RE.search(value):
        stats.redacted_values += 1
        return REDACTED
    if _WEBHOOK_URL_RE.search(value):
        stats.redacted_urls += 1
        return REDACTED_URL

    sanitized = _URL_USERINFO_RE.sub(r"\1" + REDACTED + "@", value)
    sanitized = _SECRET_QUERY_RE.sub(r"\1" + REDACTED, sanitized)
    if _SECRET_FRAGMENT_RE.search(sanitized):
        sanitized = sanitized.split("#", 1)[0]

    if sanitized != value:
        stats.redacted_urls += 1
    return sanitized


def _redact_state_if_label_requests_secret(value: dict[str, Any], stats: RedactionStats) -> None:
    if "state" not in value or value.get("state") in {REDACTED, REDACTED_URL}:
        return

    labels = [
        value.get("entity_id"),
        value.get("unique_id"),
        value.get("name"),
    ]
    attributes = value.get("attributes")
    if isinstance(attributes, dict):
        labels.append(attributes.get("friendly_name"))

    if any(_looks_secret_label(label) for label in labels):
        value["state"] = REDACTED
        stats.redacted_entity_states += 1


def _is_secret_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return (
        normalized in _SECRET_KEY_EXACT
        or normalized.startswith("auth_")
        or normalized.endswith("_auth")
        or any(part in normalized for part in _SECRET_KEY_PARTS)
    )


def _is_location_key(key: str) -> bool:
    return _normalize_key(key) in _LOCATION_KEYS


def _is_network_key(key: str) -> bool:
    return _normalize_key(key) in _NETWORK_KEYS


def _looks_secret_label(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    normalized = _normalize_key(value)
    return any(part in normalized for part in _SECRET_LABEL_PARTS)


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")

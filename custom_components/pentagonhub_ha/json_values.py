"""Convert Home Assistant values into plain JSON-compatible values."""

from __future__ import annotations

from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def jsonify(value: Any) -> Any:
    """Return a detached JSON-compatible representation without deep-copying."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _datetime_iso(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonify(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((jsonify(item) for item in value), key=_stable_sort_key)

    fragment = _jsonify_orjson_fragment(value)
    if fragment is not _NOT_A_FRAGMENT:
        return fragment

    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return jsonify(as_dict())
    as_partial_dict = getattr(value, "as_partial_dict", None)
    if callable(as_partial_dict):
        return jsonify(as_partial_dict())
    if is_dataclass(value):
        return {
            field.name: jsonify(getattr(value, field.name))
            for field in dataclass_fields(value)
        }

    attrs_fields = getattr(type(value), "__attrs_attrs__", None)
    if attrs_fields is not None:
        return {
            field.name: jsonify(getattr(value, field.name))
            for field in attrs_fields
        }
    if hasattr(value, "__dict__"):
        return {
            key: jsonify(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return str(value)


_NOT_A_FRAGMENT = object()


def _jsonify_orjson_fragment(value: Any) -> Any:
    value_type = type(value)
    if value_type.__module__ != "orjson" or value_type.__name__ != "Fragment":
        return _NOT_A_FRAGMENT

    import orjson

    return jsonify(orjson.loads(orjson.dumps(value)))


def _stable_sort_key(value: Any) -> str:
    return repr(value)


def _datetime_iso(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat()
    normalized = value.astimezone(timezone.utc).isoformat()
    return normalized.replace("+00:00", "Z")

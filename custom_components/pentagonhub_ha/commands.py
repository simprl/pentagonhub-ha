"""Command execution for PentagonHub HA."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any
import zipfile

from homeassistant.const import Platform
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .api import PentagonHubApiClient
from .build_profile import BUILD_PROFILE
from .const import (
    CONF_INSTALLATION_ID,
    CONF_INSTALLATION_MODE,
    DOMAIN,
    SANDBOX_MARKER_ATTRIBUTE,
    SANDBOX_UNIQUE_ID_PREFIX,
    STORAGE_DIR_NAME,
)
from .json_values import jsonify as _jsonify
from .managed_configuration import (
    delete_managed_file as _delete_managed_file,
    managed_file_bytes as _managed_file_bytes,
    managed_file_hash as _managed_file_hash,
    write_managed_file as _write_managed_file,
)

_LOGGER = logging.getLogger(__name__)
_SANDBOX_PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.CLIMATE,
    Platform.MEDIA_PLAYER,
    Platform.ALARM_CONTROL_PANEL,
]

_ALLOWED_EXACT = {
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
}
_ALLOWED_DIRS = (
    "packages/",
    "includes/",
    "lovelace/",
    "themes/",
    "www/managed/",
)
_DENIED_EXACT = {
    "secrets.yaml",
    ".uuid",
    ".ha_version",
    "home-assistant.log",
}
_DENIED_DIRS = (
    ".storage/",
    ".cloud/",
    "deps/",
    "tts/",
    ".pentagonhub_ha/",
)
_SECRET_FIELD_RE = re.compile(r"(token|password|secret|credential|authorization|auth)", re.IGNORECASE)
_SECRET_QUERY_RE = re.compile(r"([?&](?:token|auth|access_token|password)=)[^&\s]+", re.IGNORECASE)


@dataclass(slots=True)
class ExportResult:
    """Export artifact metadata."""

    path: Path
    sha256: str
    size_bytes: int
    files_count: int


async def async_execute_command(
    hass: HomeAssistant,
    entry: ConfigEntry,
    client: PentagonHubApiClient,
    installation_token: str,
    command: dict[str, Any],
) -> None:
    """Execute one command received from PentagonHub Core."""

    command_id = _required_string(command, "command_id")
    command_type = _required_string(command, "command_type")
    payload = command.get("payload")
    if not isinstance(payload, dict):
        await client.fail_command(
            installation_token,
            command_id,
            "Command payload is invalid",
            error_code="invalid_payload",
        )
        return

    try:
        await client.send_command_progress(
            installation_token,
            command_id,
            {"progress": 10, "message": f"Executing {command_type}"},
        )
        if command_type == "ha.files.export":
            result = await hass.async_add_executor_job(
                _export_managed_files,
                Path(hass.config.path()),
                command_id,
                payload,
            )
            await client.upload_artifact(
                installation_token,
                _required_string(payload, "artifact_upload_url"),
                result.path,
            )
            await client.complete_command(
                installation_token,
                command_id,
                {
                    "artifact_id": _required_string(payload, "artifact_id"),
                    "artifact_sha256": result.sha256,
                    "artifact_size_bytes": result.size_bytes,
                    "files_count": result.files_count,
                },
            )
            return

        if command_type == "ha.files.apply_export":
            download_path = _tmp_dir(Path(hass.config.path())) / f"apply-{command_id}.zip"
            await client.download_artifact_to_path(
                installation_token,
                _required_string(payload, "artifact_download_url"),
                download_path,
            )
            result = await hass.async_add_executor_job(
                _apply_export_artifact,
                Path(hass.config.path()),
                command_id,
                payload,
                download_path,
            )
            await client.complete_command(installation_token, command_id, result)
            return

        if command_type == "ha.release.apply":
            download_path = _tmp_dir(Path(hass.config.path())) / f"release-{command_id}.zip"
            await client.download_artifact_to_path(
                installation_token,
                _required_string(payload, "artifact_download_url"),
                download_path,
            )
            result = await hass.async_add_executor_job(
                _apply_release_artifact,
                Path(hass.config.path()),
                command_id,
                payload,
                download_path,
            )
            await client.complete_command(installation_token, command_id, result)
            return

        if command_type == "ha.release.rollback":
            download_path = _tmp_dir(Path(hass.config.path())) / f"rollback-{command_id}.zip"
            artifact_url = payload.get("artifact_download_url")
            if isinstance(artifact_url, str) and artifact_url:
                await client.download_artifact_to_path(
                    installation_token,
                    artifact_url,
                    download_path,
                )
            result = await hass.async_add_executor_job(
                _rollback_release,
                Path(hass.config.path()),
                command_id,
                payload,
                download_path if download_path.exists() else None,
            )
            await client.complete_command(installation_token, command_id, result)
            return

        if command_type == "ha.files.apply_delta":
            result = await hass.async_add_executor_job(
                _apply_delta_files,
                Path(hass.config.path()),
                command_id,
                payload,
            )
            if payload.get("refresh_lovelace") is True:
                result["lovelace_reload"] = await _async_reload_lovelace(hass)
            await client.complete_command(installation_token, command_id, result)
            return

        if command_type == "ha.context.export":
            result = await _async_export_context(hass, command_id, payload)
            await _async_upload_json_result(
                hass,
                client,
                installation_token,
                command_id,
                payload,
                result,
                "context-export",
                completion_extra={"context_summary": _context_result_summary(result)},
            )
            return

        if command_type == "ha.sandbox.export":
            result = await _async_export_sandbox(hass, entry)
            await _async_upload_json_result(
                hass,
                client,
                installation_token,
                command_id,
                payload,
                result,
                "sandbox-export",
            )
            return

        if command_type == "ha.sandbox.inventory":
            _require_dev_installation(entry)
            result = await _async_sandbox_inventory(hass, entry)
            await _async_upload_json_result(
                hass,
                client,
                installation_token,
                command_id,
                payload,
                result,
                "sandbox-inventory",
            )
            return

        if command_type == "ha.sandbox.status":
            _require_dev_installation(entry)
            from .sandbox_runtime import sandbox_runtime_status

            result = await hass.async_add_executor_job(
                sandbox_runtime_status,
                Path(hass.config.path()),
            )
            await client.complete_command(installation_token, command_id, result)
            return

        if command_type == "ha.sandbox.suspend":
            _require_dev_installation(entry)
            runtime = hass.data[DOMAIN][entry.entry_id].sandbox
            await runtime.async_set_suspended(True)
            await client.complete_command(
                installation_token,
                command_id,
                {"suspended": True},
            )
            return

        if command_type == "ha.sandbox.apply":
            _require_dev_installation(entry)
            from .sandbox_runtime import apply_sandbox_manifest

            download_path = _tmp_dir(Path(hass.config.path())) / f"sandbox-{command_id}.json"
            await client.download_artifact_to_path(
                installation_token,
                _required_string(payload, "artifact_download_url"),
                download_path,
            )
            manifest = await hass.async_add_executor_job(
                _read_sandbox_manifest_artifact,
                download_path,
                payload,
            )
            _remove_stale_sandbox_registry_entries(hass, manifest)
            result = await hass.async_add_executor_job(
                apply_sandbox_manifest,
                Path(hass.config.path()),
                manifest,
                _required_string(dict(entry.data), CONF_INSTALLATION_ID),
            )
            await _async_reload_sandbox_platforms(hass, entry)
            await client.complete_command(installation_token, command_id, result)
            return

        if command_type == "ha.sandbox.clear":
            _require_dev_installation(entry)
            from .sandbox_runtime import clear_sandbox_runtime

            _remove_stale_sandbox_registry_entries(
                hass,
                {"entities": []},
            )
            result = await hass.async_add_executor_job(
                clear_sandbox_runtime,
                Path(hass.config.path()),
            )
            await _async_reload_sandbox_platforms(hass, entry)
            await client.complete_command(installation_token, command_id, result)
            return

        await client.fail_command(
            installation_token,
            command_id,
            f"Unsupported command type {command_type}",
            error_code="unsupported_command",
        )
    except Exception as err:  # noqa: BLE001 - command errors must be reported to Core
        _LOGGER.exception("PentagonHub command %s failed", command_id)
        await client.fail_command(
            installation_token,
            command_id,
            str(err),
            error_code=err.__class__.__name__,
        )


def _export_managed_files(config_dir: Path, command_id: str, payload: dict[str, Any]) -> ExportResult:
    artifact_path = _tmp_dir(config_dir) / f"export-{command_id}.zip"
    files = list(_iter_managed_files(config_dir))
    manifest_files: dict[str, dict[str, Any]] = {}

    with zipfile.ZipFile(artifact_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_path, absolute_path in files:
            content = _managed_file_bytes(relative_path, absolute_path)
            if relative_path == "configuration.yaml" and not content:
                continue
            file_sha256 = hashlib.sha256(content).hexdigest()
            size_bytes = len(content)
            manifest_files[relative_path] = {
                "sha256": file_sha256,
                "size": size_bytes,
            }
            archive.writestr(f"files/{relative_path}", content)

        manifest = {
            "schema_version": 1,
            "artifact_type": "export_temp",
            "built_at": datetime.now(timezone.utc).isoformat(),
            "command_id": command_id,
            "artifact_id": payload.get("artifact_id"),
            "managed_paths_contract": payload.get("managed_paths_contract"),
            "files": manifest_files,
            "summary": {
                "files_count": len(manifest_files),
                "total_size": sum(item["size"] for item in manifest_files.values()),
            },
        }
        archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))

    return ExportResult(
        path=artifact_path,
        sha256=_sha256_file(artifact_path),
        size_bytes=artifact_path.stat().st_size,
        files_count=len(manifest_files),
    )


def _apply_export_artifact(
    config_dir: Path,
    command_id: str,
    payload: dict[str, Any],
    artifact_path: Path,
) -> dict[str, Any]:
    expected_sha256 = payload.get("artifact_sha256")
    if isinstance(expected_sha256, str) and expected_sha256 and _sha256_file(artifact_path) != expected_sha256:
        raise ValueError("Artifact hash mismatch")

    backup_id = f"backup-{command_id}"
    backup_path = _backup_dir(config_dir) / f"{backup_id}.zip"
    changed_files: list[str] = []
    applied_files = 0

    with zipfile.ZipFile(artifact_path, "r") as archive:
        manifest_raw = archive.read("manifest.json")
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        expected_manifest_sha256 = payload.get("manifest_sha256")
        if (
            isinstance(expected_manifest_sha256, str)
            and expected_manifest_sha256
            and expected_manifest_sha256 != manifest_sha256
        ):
            raise ValueError("Release manifest hash mismatch")
        manifest = _read_manifest(archive)
        members = [
            item
            for item in archive.infolist()
            if not item.is_dir() and item.filename.startswith("files/")
        ]
        planned_files: list[tuple[zipfile.ZipInfo, str]] = []
        for member in members:
            relative_path = _normalize_zip_member(member.filename)
            if not _is_managed_path(relative_path):
                raise ValueError(f"Artifact contains unmanaged path: {relative_path}")
            planned_files.append((member, relative_path))

        _write_backup(config_dir, backup_path, [relative_path for _member, relative_path in planned_files])

        for member, relative_path in planned_files:
            manifest_hash = _manifest_file_hash(manifest, relative_path)
            content = archive.read(member)
            if manifest_hash and hashlib.sha256(content).hexdigest() != manifest_hash:
                raise ValueError(f"Artifact file hash mismatch: {relative_path}")

            target_path = config_dir / relative_path
            previous_hash = _managed_file_hash(relative_path, target_path)
            next_hash = hashlib.sha256(content).hexdigest()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            _write_managed_file(config_dir, relative_path, content)
            applied_files += 1
            if previous_hash != next_hash:
                changed_files.append(relative_path)

    return {
        "backup_id": backup_id,
        "backup_path": str(backup_path),
        "applied_files": applied_files,
        "changed_files": changed_files,
    }


def _apply_release_artifact(
    config_dir: Path,
    command_id: str,
    payload: dict[str, Any],
    artifact_path: Path,
) -> dict[str, Any]:
    release_artifact = _read_release_artifact(artifact_path, payload)
    manifest_raw = release_artifact["manifest_raw"]
    manifest_sha256 = release_artifact["manifest_sha256"]
    manifest_files = release_artifact["manifest_files"]
    planned_files = release_artifact["planned_files"]
    version = release_artifact["version"]

    state = _read_release_state(config_dir)
    previous_files = _state_files(state)
    drift_before, drift_paths = _detect_release_drift(config_dir, previous_files, manifest_files)
    force = payload.get("force") is True
    adopt_matching_drift = payload.get("adopt_matching_drift") is True
    if drift_before != "clean" and not force:
        if not (adopt_matching_drift and drift_before == "matching_target"):
            raise ValueError(f"Live managed files have drift: {', '.join(drift_paths[:20])}")

    previous_release = _state_string(state, "current_release")
    previous_release_files = set(previous_files)
    next_release_files = {relative_path for relative_path, _content, _hash in planned_files}
    deleted_paths = sorted(path for path in previous_release_files - next_release_files if _is_managed_path(path))

    backup_id = f"release-backup-{command_id}"
    backup_path = _backup_dir(config_dir) / f"{backup_id}.zip"
    _write_backup(
        config_dir,
        backup_path,
        sorted(next_release_files | set(deleted_paths)),
    )

    changed_files: list[str] = []
    for relative_path, content, next_hash in planned_files:
        target_path = config_dir / relative_path
        previous_hash = _managed_file_hash(relative_path, target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _write_managed_file(config_dir, relative_path, content)
        if previous_hash != next_hash:
            changed_files.append(relative_path)

    actually_deleted: list[str] = []
    for relative_path in deleted_paths:
        if _delete_managed_file(config_dir, relative_path):
            actually_deleted.append(relative_path)

    release_storage_dir = _release_storage_dir(config_dir, version)
    release_storage_dir.mkdir(parents=True, exist_ok=True)
    stored_artifact_path = release_storage_dir / "release.zip"
    stored_manifest_path = release_storage_dir / "manifest.json"
    stored_artifact_path.write_bytes(artifact_path.read_bytes())
    stored_manifest_path.write_bytes(manifest_raw)
    state_next = {
        "schema_version": 1,
        "current_release": version,
        "previous_release": previous_release,
        "release_id": payload.get("release_id"),
        "artifact_sha256": _sha256_file(stored_artifact_path),
        "manifest_sha256": manifest_sha256,
        "applied_at": _utc_now_iso(),
        "last_backup_id": backup_id,
        "files": manifest_files,
    }
    _write_release_state(config_dir, state_next)

    return {
        "release_id": payload.get("release_id"),
        "version": version,
        "current_release": version,
        "previous_release": previous_release,
        "backup_id": backup_id,
        "backup_path": str(backup_path),
        "applied_files": len(planned_files),
        "changed_files": changed_files,
        "deleted_files": actually_deleted,
        "drift_before": drift_before,
        "drift_paths": drift_paths,
        "drift_after": "clean",
        "stored_release_path": str(stored_artifact_path),
    }


def _rollback_release(
    config_dir: Path,
    command_id: str,
    payload: dict[str, Any],
    downloaded_artifact_path: Path | None,
) -> dict[str, Any]:
    target_version = _required_string(payload, "release_version")
    local_artifact_path = _release_storage_dir(config_dir, target_version) / "release.zip"
    artifact_path = _select_rollback_artifact(local_artifact_path, downloaded_artifact_path, payload)
    release_artifact = _read_release_artifact(artifact_path, payload)
    manifest_raw = release_artifact["manifest_raw"]
    manifest_sha256 = release_artifact["manifest_sha256"]
    manifest_files = release_artifact["manifest_files"]
    planned_files = release_artifact["planned_files"]
    version = release_artifact["version"]
    if version != target_version:
        raise ValueError("Rollback release version mismatch")

    state = _read_release_state(config_dir)
    current_release = _state_string(state, "current_release")
    current_files = _state_files(state)
    drift_before, drift_paths = _detect_release_drift(config_dir, current_files, manifest_files)
    if drift_before != "clean" and payload.get("force") is not True:
        raise ValueError(f"Live managed files have drift: {', '.join(drift_paths[:20])}")

    current_release_files = set(current_files)
    target_release_files = {relative_path for relative_path, _content, _hash in planned_files}
    deleted_paths = sorted(path for path in current_release_files - target_release_files if _is_managed_path(path))

    backup_id = f"release-rollback-backup-{command_id}"
    backup_path = _backup_dir(config_dir) / f"{backup_id}.zip"
    _write_backup(
        config_dir,
        backup_path,
        sorted(current_release_files | target_release_files),
    )

    changed_files: list[str] = []
    for relative_path, content, next_hash in planned_files:
        target_path = config_dir / relative_path
        previous_hash = _managed_file_hash(relative_path, target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _write_managed_file(config_dir, relative_path, content)
        if previous_hash != next_hash:
            changed_files.append(relative_path)

    actually_deleted: list[str] = []
    for relative_path in deleted_paths:
        if _delete_managed_file(config_dir, relative_path):
            actually_deleted.append(relative_path)

    release_storage_dir = _release_storage_dir(config_dir, version)
    release_storage_dir.mkdir(parents=True, exist_ok=True)
    stored_artifact_path = release_storage_dir / "release.zip"
    stored_manifest_path = release_storage_dir / "manifest.json"
    if artifact_path != stored_artifact_path:
        stored_artifact_path.write_bytes(artifact_path.read_bytes())
    stored_manifest_path.write_bytes(manifest_raw)
    state_next = {
        "schema_version": 1,
        "current_release": version,
        "previous_release": current_release,
        "release_id": payload.get("release_id"),
        "artifact_sha256": _sha256_file(stored_artifact_path),
        "manifest_sha256": manifest_sha256,
        "applied_at": _utc_now_iso(),
        "last_backup_id": backup_id,
        "files": manifest_files,
    }
    _write_release_state(config_dir, state_next)

    return {
        "release_id": payload.get("release_id"),
        "version": version,
        "current_release": version,
        "previous_release": current_release,
        "backup_id": backup_id,
        "backup_path": str(backup_path),
        "applied_files": len(planned_files),
        "changed_files": changed_files,
        "deleted_files": actually_deleted,
        "drift_before": drift_before,
        "drift_paths": drift_paths,
        "drift_after": "clean",
        "stored_release_path": str(stored_artifact_path),
    }


def _apply_delta_files(
    config_dir: Path,
    command_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Delta payload is missing files")

    planned_files: list[tuple[str, bytes, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Delta file entry is invalid")
        relative_path = _normalize_delta_path(_required_string(item, "path"))
        expected_sha256 = _required_string(item, "sha256")
        expected_size = item.get("size_bytes")
        content_base64 = item.get("content_base64")
        if not isinstance(content_base64, str):
            raise ValueError(f"Delta file content is invalid: {relative_path}")
        content = base64.b64decode(content_base64, validate=True)

        if not isinstance(expected_size, int) or expected_size < 0:
            raise ValueError(f"Delta file size is invalid: {relative_path}")
        if len(content) != expected_size:
            raise ValueError(f"Delta file size mismatch: {relative_path}")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(f"Delta file hash mismatch: {relative_path}")
        planned_files.append((relative_path, content, actual_sha256))

    backup_id = f"backup-delta-{command_id}"
    backup_path = _backup_dir(config_dir) / f"{backup_id}.zip"
    _write_backup(config_dir, backup_path, [relative_path for relative_path, _content, _hash in planned_files])

    changed_files: list[str] = []
    for relative_path, content, next_hash in planned_files:
        target_path = config_dir / relative_path
        previous_hash = _managed_file_hash(relative_path, target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _write_managed_file(config_dir, relative_path, content)
        if previous_hash != next_hash:
            changed_files.append(relative_path)

    return {
        "backup_id": backup_id,
        "backup_path": str(backup_path),
        "applied_files": len(planned_files),
        "changed_files": changed_files,
    }


async def _async_export_context(
    hass: HomeAssistant,
    command_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    exported_at = _utc_now_iso()
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    entities = [
        _sanitize_json(
            {
                **_jsonify(state.as_dict()),
                "domain": state.entity_id.split(".", 1)[0],
            },
        )
        for state in hass.states.async_all()
    ]
    entity_entries = [
        _sanitize_json(_jsonify(entry))
        for entity_id in entity_registry.entities
        if (entry := entity_registry.async_get(entity_id)) is not None
    ]
    device_entries = [
        _sanitize_json(_jsonify(entry))
        for entry in device_registry.devices.values()
    ]
    area_entries = [
        _sanitize_json(_jsonify(entry))
        for entry in area_registry.areas.values()
    ]

    entities.sort(key=lambda item: str(item.get("entity_id", "")))
    entity_entries.sort(key=lambda item: str(item.get("entity_id", item.get("id", ""))))
    device_entries.sort(key=lambda item: str(item.get("id", item.get("name", ""))))
    area_entries.sort(key=lambda item: str(item.get("id", item.get("name", ""))))

    return {
        "schema_version": 1,
        "export_type": "ha_context",
        "exported_at": exported_at,
        "ha_version": HA_VERSION,
        "command_id": command_id,
        "source_installation_id": payload.get("source_installation_id"),
        "meta": {
            "source": "pentagonhub-ha",
            "created_at": exported_at,
            "ha_version": HA_VERSION,
        },
        "entities": entities,
        "entity_registry": entity_entries,
        "device_registry": device_entries,
        "area_registry": area_entries,
        "counts": {
            "entities": len(entities),
            "entity_registry": len(entity_entries),
            "device_registry": len(device_entries),
            "area_registry": len(area_entries),
        },
    }


async def _async_export_sandbox(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Export sanitized source state for virtual-environment generation."""

    exported_at = _utc_now_iso()
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    area_registry = ar.async_get(hass)
    entities: list[dict[str, Any]] = []

    for state in hass.states.async_all():
        registry_entry = entity_registry.async_get(state.entity_id)
        attributes = _bounded_attributes(_sanitize_json(_jsonify(dict(state.attributes))))
        entities.append(
            {
                "entity_id": state.entity_id,
                "domain": state.entity_id.split(".", 1)[0],
                "state": str(state.state)[:4096],
                "attributes": attributes,
                "unique_id": _optional_string(
                    getattr(registry_entry, "unique_id", None)
                ),
                "platform": _optional_string(
                    getattr(registry_entry, "platform", None)
                ),
                "device_id": _optional_string(
                    getattr(registry_entry, "device_id", None)
                ),
                "area_id": _optional_string(
                    getattr(registry_entry, "area_id", None)
                ),
                "config_entry_id": _optional_string(
                    getattr(registry_entry, "config_entry_id", None)
                ),
                "name": _optional_string(
                    getattr(registry_entry, "name", None)
                )
                or _optional_string(attributes.get("friendly_name")),
            }
        )

    devices = [
        {
            "id": str(device.id),
            "name": _optional_string(getattr(device, "name", None)),
            "name_by_user": _optional_string(getattr(device, "name_by_user", None)),
            "manufacturer": _optional_string(getattr(device, "manufacturer", None)),
            "model": _optional_string(getattr(device, "model", None)),
            "area_id": _optional_string(getattr(device, "area_id", None)),
        }
        for device in device_registry.devices.values()
    ]
    areas = [
        {
            "id": str(area.id),
            "name": str(area.name),
        }
        for area in area_registry.areas.values()
    ]
    entities.sort(key=lambda item: item["entity_id"])
    devices.sort(key=lambda item: item["id"])
    areas.sort(key=lambda item: item["id"])
    return {
        "schema_version": "sandbox_export_v1",
        "installation_id": _required_string(dict(entry.data), CONF_INSTALLATION_ID),
        "exported_at": exported_at,
        "ha_version": HA_VERSION,
        "entities": entities,
        "devices": devices,
        "areas": areas,
    }


async def _async_sandbox_inventory(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Export target ownership data used by preflight and postflight."""

    from .sandbox_runtime import sandbox_runtime_status

    entity_registry = er.async_get(hass)
    entities_by_id: dict[str, dict[str, Any]] = {}
    states_by_id = {state.entity_id: state for state in hass.states.async_all()}
    entity_ids = sorted(set(states_by_id) | set(entity_registry.entities))
    for entity_id in entity_ids:
        state = states_by_id.get(entity_id)
        registry_entry = entity_registry.async_get(entity_id)
        unique_id = _optional_string(getattr(registry_entry, "unique_id", None))
        marker = (
            unique_id
            if unique_id and unique_id.startswith(SANDBOX_UNIQUE_ID_PREFIX)
            else _optional_string(
                state.attributes.get(SANDBOX_MARKER_ATTRIBUTE)
                if state is not None
                else None
            )
        )
        entities_by_id[entity_id] = {
            "entity_id": entity_id,
            "domain": entity_id.split(".", 1)[0],
            "state": str(state.state)[:4096] if state is not None else "unavailable",
            "unique_id": unique_id,
            "platform": _optional_string(
                getattr(registry_entry, "platform", None)
            ),
            "device_id": _optional_string(
                getattr(registry_entry, "device_id", None)
            ),
            "config_entry_id": _optional_string(
                getattr(registry_entry, "config_entry_id", None)
            ),
            "sandbox_marker": marker,
        }
    entities = list(entities_by_id.values())
    status = await hass.async_add_executor_job(
        sandbox_runtime_status,
        Path(hass.config.path()),
    )
    return {
        "schema_version": "sandbox_inventory_v1",
        "installation_id": _required_string(dict(entry.data), CONF_INSTALLATION_ID),
        "exported_at": _utc_now_iso(),
        "runtime_hash": status.get("runtime_hash"),
        "entities": entities,
    }


async def _async_upload_json_result(
    hass: HomeAssistant,
    client: PentagonHubApiClient,
    installation_token: str,
    command_id: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    prefix: str,
    *,
    completion_extra: dict[str, Any] | None = None,
) -> None:
    artifact_id = _required_string(payload, "artifact_id")
    path = _tmp_dir(Path(hass.config.path())) / f"{prefix}-{command_id}.json"
    await hass.async_add_executor_job(
        _write_json_result,
        path,
        result,
    )
    await client.upload_artifact(
        installation_token,
        _required_string(payload, "artifact_upload_url"),
        path,
    )
    artifact_metadata = await hass.async_add_executor_job(
        _artifact_result_metadata,
        path,
    )
    await client.complete_command(
        installation_token,
        command_id,
        {
            "artifact_id": artifact_id,
            **artifact_metadata,
            **(completion_extra or {}),
        },
    )


def _context_result_summary(result: dict[str, Any]) -> dict[str, Any]:
    counts = result.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("Context export counts are missing")
    return {
        "exported_at": _required_string(result, "exported_at"),
        "ha_version": _required_string(result, "ha_version"),
        "entities_count": _required_nonnegative_integer(counts, "entities"),
        "entity_registry_count": _required_nonnegative_integer(
            counts,
            "entity_registry",
        ),
        "device_registry_count": _required_nonnegative_integer(
            counts,
            "device_registry",
        ),
        "area_registry_count": _required_nonnegative_integer(
            counts,
            "area_registry",
        ),
    }


def _artifact_result_metadata(path: Path) -> dict[str, Any]:
    return {
        "artifact_sha256": _sha256_file(path),
        "artifact_size_bytes": path.stat().st_size,
    }


def _iter_managed_files(config_dir: Path):
    for relative_path in sorted(_ALLOWED_EXACT):
        absolute_path = config_dir / relative_path
        if absolute_path.exists() and absolute_path.is_file() and not absolute_path.is_symlink():
            yield relative_path, absolute_path

    for prefix in _ALLOWED_DIRS:
        directory = config_dir / prefix
        if not directory.exists() or not directory.is_dir() or directory.is_symlink():
            continue
        for absolute_path in sorted(directory.rglob("*")):
            if not absolute_path.is_file() or absolute_path.is_symlink():
                continue
            relative_path = absolute_path.relative_to(config_dir).as_posix()
            if _is_managed_path(relative_path):
                yield relative_path, absolute_path


def _write_backup(config_dir: Path, backup_path: Path, relative_paths: list[str]) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as backup:
        for relative_path in relative_paths:
            existing = config_dir / relative_path
            if existing.exists() and existing.is_file() and not existing.is_symlink():
                backup.write(existing, f"files/{relative_path}")


def _read_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    return _parse_manifest_raw(_read_manifest_raw(archive))


def _read_manifest_raw(archive: zipfile.ZipFile) -> bytes:
    try:
        return archive.read("manifest.json")
    except KeyError as err:
        raise ValueError("Artifact manifest is missing") from err


def _parse_manifest_raw(raw: bytes) -> dict[str, Any]:
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Artifact manifest is invalid")
    return parsed


def _read_release_artifact(artifact_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    expected_sha256 = payload.get("artifact_sha256")
    if isinstance(expected_sha256, str) and expected_sha256 and _sha256_file(artifact_path) != expected_sha256:
        raise ValueError("Artifact hash mismatch")

    with zipfile.ZipFile(artifact_path, "r") as archive:
        manifest_raw = _read_manifest_raw(archive)
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        expected_manifest_sha256 = payload.get("manifest_sha256")
        if (
            isinstance(expected_manifest_sha256, str)
            and expected_manifest_sha256
            and expected_manifest_sha256 != manifest_sha256
        ):
            raise ValueError("Release manifest hash mismatch")
        manifest = _parse_manifest_raw(manifest_raw)
        if manifest.get("schema_version") != 1 or manifest.get("artifact_type") != "release":
            raise ValueError("Release artifact manifest is invalid")
        version = _required_string(manifest, "version")
        payload_version = payload.get("release_version")
        if isinstance(payload_version, str) and payload_version and payload_version != version:
            raise ValueError("Release version mismatch")
        project_code = payload.get("project_code")
        manifest_project_code = manifest.get("project_code") or manifest.get("installation")
        if isinstance(project_code, str) and isinstance(manifest_project_code, str):
            if project_code.upper() != manifest_project_code.upper():
                raise ValueError("Release project mismatch")

        manifest_files = _manifest_files(manifest)
        planned_files: list[tuple[str, bytes, str]] = []
        for relative_path, file_meta in sorted(manifest_files.items()):
            if not _is_managed_path(relative_path):
                raise ValueError(f"Release contains unmanaged path: {relative_path}")
            expected_file_hash = _required_string(file_meta, "sha256")
            try:
                content = archive.read(f"files/{relative_path}")
            except KeyError as err:
                raise ValueError(f"Release file is missing: {relative_path}") from err
            if hashlib.sha256(content).hexdigest() != expected_file_hash:
                raise ValueError(f"Release file hash mismatch: {relative_path}")
            expected_size = file_meta.get("size")
            if isinstance(expected_size, int) and len(content) != expected_size:
                raise ValueError(f"Release file size mismatch: {relative_path}")
            planned_files.append((relative_path, content, expected_file_hash))

    return {
        "manifest_raw": manifest_raw,
        "manifest_sha256": manifest_sha256,
        "manifest": manifest,
        "manifest_files": manifest_files,
        "planned_files": planned_files,
        "version": version,
    }


def _select_rollback_artifact(
    local_artifact_path: Path,
    downloaded_artifact_path: Path | None,
    payload: dict[str, Any],
) -> Path:
    expected_sha256 = payload.get("artifact_sha256")
    if local_artifact_path.exists() and local_artifact_path.is_file():
        if not isinstance(expected_sha256, str) or not expected_sha256:
            return local_artifact_path
        if _sha256_file(local_artifact_path) == expected_sha256:
            return local_artifact_path

    if downloaded_artifact_path is not None and downloaded_artifact_path.exists() and downloaded_artifact_path.is_file():
        return downloaded_artifact_path

    if local_artifact_path.exists() and local_artifact_path.is_file():
        raise ValueError("Stored rollback release hash mismatch")
    raise ValueError("Rollback release is not stored locally and no Core artifact was provided")


def _manifest_file_hash(manifest: dict[str, Any], relative_path: str) -> str | None:
    files = manifest.get("files")
    if not isinstance(files, dict):
        return None
    entry = files.get(relative_path)
    if not isinstance(entry, dict):
        return None
    value = entry.get("sha256")
    return value if isinstance(value, str) else None


def _manifest_files(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Release manifest contains no files")
    result: dict[str, dict[str, Any]] = {}
    for raw_path, raw_meta in files.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("Release manifest file path is invalid")
        relative_path = _normalize_delta_path(raw_path)
        if not isinstance(raw_meta, dict):
            raise ValueError(f"Release manifest file metadata is invalid: {relative_path}")
        result[relative_path] = raw_meta
    return result


def _read_release_state(config_dir: Path) -> dict[str, Any]:
    state_path = _release_state_path(config_dir)
    if not state_path.exists():
        return {}
    try:
        parsed = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_release_state(config_dir: Path, state: dict[str, Any]) -> None:
    state_path = _release_state_path(config_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(state, sort_keys=True, indent=2), encoding="utf-8")
    temp_path.replace(state_path)


def _state_files(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files = state.get("files")
    if not isinstance(files, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_path, raw_meta in files.items():
        if isinstance(raw_path, str) and isinstance(raw_meta, dict) and _is_managed_path(raw_path):
            result[raw_path] = raw_meta
    return result


def _detect_release_drift(
    config_dir: Path,
    previous_files: dict[str, dict[str, Any]],
    next_files: dict[str, dict[str, Any]],
) -> tuple[str, list[str]]:
    if previous_files:
        drift_paths = []
        matching_target = True
        for relative_path, previous_meta in sorted(previous_files.items()):
            target_path = config_dir / relative_path
            current_hash = _managed_file_hash(relative_path, target_path)
            previous_hash = previous_meta.get("sha256")
            if current_hash == previous_hash:
                continue
            drift_paths.append(relative_path)
            next_meta = next_files.get(relative_path)
            if not isinstance(next_meta, dict) or current_hash != next_meta.get("sha256"):
                matching_target = False
        if not drift_paths:
            return "clean", []
        return ("matching_target" if matching_target else "conflicting"), drift_paths

    unmanaged_conflicts = []
    for relative_path, next_meta in sorted(next_files.items()):
        target_path = config_dir / relative_path
        if not target_path.exists() or not target_path.is_file():
            continue
        if _managed_file_hash(relative_path, target_path) != next_meta.get("sha256"):
            unmanaged_conflicts.append(relative_path)
    if unmanaged_conflicts:
        return "initial_unmanaged", unmanaged_conflicts
    return "clean", []


def _state_string(state: dict[str, Any], key: str) -> str | None:
    value = state.get(key)
    return value if isinstance(value, str) and value else None


def _normalize_zip_member(filename: str) -> str:
    if not filename.startswith("files/"):
        raise ValueError("Artifact member is outside files/")
    path = filename.removeprefix("files/").replace("\\", "/").lstrip("/")
    if not path or path.startswith("../") or "/../" in path or path == "..":
        raise ValueError(f"Artifact contains unsafe path: {filename}")
    return path


def _normalize_delta_path(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    if normalized.lower().startswith("config/"):
        normalized = normalized[7:]
    if "\0" in normalized:
        raise ValueError("Delta path contains NUL")
    segments = normalized.split("/")
    if not normalized or any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"Delta path is unsafe: {path}")
    if not _is_managed_path(normalized):
        raise ValueError(f"Delta path is not managed: {normalized}")
    return normalized


def _is_managed_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/").lower()
    if normalized.startswith("config/"):
        normalized = normalized.removeprefix("config/")
    if normalized in _DENIED_EXACT:
        return False
    if any(normalized.startswith(prefix) for prefix in _DENIED_DIRS):
        return False
    filename = normalized.rsplit("/", 1)[-1]
    if filename == "home-assistant.log" or filename.startswith("home-assistant.log."):
        return False
    if filename.endswith(".db") or ".db-" in filename:
        return False
    return normalized in _ALLOWED_EXACT or any(normalized.startswith(prefix) for prefix in _ALLOWED_DIRS)


async def _async_reload_lovelace(hass: HomeAssistant) -> str:
    if not hass.services.has_service("lovelace", "reload"):
        return "service_unavailable"
    await hass.services.async_call("lovelace", "reload", blocking=True)
    return "called"


async def _async_reload_sandbox_platforms(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reload entity platforms before reporting a sandbox mutation complete."""

    from .sandbox_runtime import async_load_sandbox_runtime

    unloaded = await hass.config_entries.async_unload_platforms(
        entry,
        _SANDBOX_PLATFORMS,
    )
    if not unloaded:
        raise RuntimeError("Could not unload PentagonHub sandbox entity platforms")

    entry_runtime = hass.data[DOMAIN][entry.entry_id]
    entry_runtime.sandbox = await async_load_sandbox_runtime(hass, entry.entry_id)
    await hass.config_entries.async_forward_entry_setups(
        entry,
        _SANDBOX_PLATFORMS,
    )


def _tmp_dir(config_dir: Path) -> Path:
    path = config_dir / STORAGE_DIR_NAME / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backup_dir(config_dir: Path) -> Path:
    path = config_dir / STORAGE_DIR_NAME / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _release_state_path(config_dir: Path) -> Path:
    return config_dir / STORAGE_DIR_NAME / "state.json"


def _release_storage_dir(config_dir: Path, version: str) -> Path:
    return config_dir / STORAGE_DIR_NAME / "releases" / _safe_release_dir_name(version)


def _safe_release_dir_name(version: str) -> str:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,119}$", version):
        raise ValueError("Release version is not safe for storage")
    return version


def _sha256_file(path: Path) -> str:
    hash_obj = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(64 * 1024), b""):
            hash_obj.update(chunk)
    return hash_obj.hexdigest()


def _required_string(source: dict[str, Any], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Command is missing {key}")
    return value


def _required_nonnegative_integer(source: dict[str, Any], key: str) -> int:
    value = source.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"Command result is missing {key}")
    return value


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _require_dev_installation(entry: ConfigEntry) -> None:
    if BUILD_PROFILE != "dev" or entry.data.get(CONF_INSTALLATION_MODE) != "dev":
        raise PermissionError("PentagonHub sandbox mutation is allowed only on Dev HA")


def _read_sandbox_manifest_artifact(
    artifact_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not artifact_path.exists() or not artifact_path.is_file():
        raise ValueError("Sandbox runtime artifact is missing")
    size_bytes = artifact_path.stat().st_size
    if size_bytes > 25 * 1024 * 1024:
        raise ValueError("Sandbox runtime artifact exceeds 25 MB")
    expected_sha256 = payload.get("artifact_sha256")
    if (
        isinstance(expected_sha256, str)
        and expected_sha256
        and _sha256_file(artifact_path) != expected_sha256
    ):
        raise ValueError("Sandbox runtime artifact hash mismatch")
    parsed = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Sandbox runtime artifact must contain a JSON object")
    return parsed


def _write_json_result(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 25 * 1024 * 1024:
        raise ValueError("Sandbox export exceeds 25 MB")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encoded)


def _bounded_attributes(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in sorted(value)[:200]:
        candidate = value[key]
        try:
            encoded = json.dumps(candidate, ensure_ascii=True, default=str)
        except (TypeError, ValueError):
            continue
        if len(encoded.encode("utf-8")) > 16 * 1024:
            continue
        next_result = {**result, str(key): candidate}
        if len(
            json.dumps(next_result, ensure_ascii=True, default=str).encode("utf-8")
        ) > 64 * 1024:
            continue
        result = next_result
    return result


def _remove_stale_sandbox_registry_entries(
    hass: HomeAssistant,
    manifest: dict[str, Any],
) -> None:
    """Remove only stale PentagonHub-owned registry rows before entry reload."""

    expected_by_marker = {
        str(entity.get("unique_id")): str(entity.get("entity_id"))
        for entity in manifest.get("entities", [])
        if isinstance(entity, dict)
        and isinstance(entity.get("unique_id"), str)
        and isinstance(entity.get("entity_id"), str)
    }
    registry = er.async_get(hass)
    for registry_entry in list(registry.entities.values()):
        marker = _optional_string(getattr(registry_entry, "unique_id", None))
        if not marker or not marker.startswith(SANDBOX_UNIQUE_ID_PREFIX):
            continue
        expected_entity_id = expected_by_marker.get(marker)
        if expected_entity_id == registry_entry.entity_id:
            continue
        registry.async_remove(registry_entry.entity_id)


def _sanitize_json(value: Any, key: str | None = None) -> Any:
    if key and _SECRET_FIELD_RE.search(key):
        return "<redacted>"
    if isinstance(value, str):
        return _SECRET_QUERY_RE.sub(r"\1<redacted>", value)
    if isinstance(value, dict):
        return {str(item_key): _sanitize_json(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    return value


def _utc_now_iso() -> str:
    return _datetime_iso(datetime.now(timezone.utc))


def _datetime_iso(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat()
    normalized = value.astimezone(timezone.utc).isoformat()
    return normalized.replace("+00:00", "Z")

"""PentagonHub Core HTTP API client."""

from __future__ import annotations

import asyncio
from typing import Any
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientError, ClientResponse, ClientSession

DEFAULT_TIMEOUT_SECONDS = 20


class PentagonHubApiError(Exception):
    """Raised when PentagonHub Core cannot complete a request."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class PentagonHubApiClient:
    """Small async client for the PentagonHub HA-facing endpoints."""

    def __init__(self, session: ClientSession, api_base_url: str) -> None:
        self._session = session
        self._api_base_url = normalize_api_base_url(api_base_url)

    async def create_pairing_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a device-code pairing session."""

        return await self._post("pentagonhub-ha/pairing-sessions", payload)

    async def exchange_pairing_token(self, device_code: str) -> dict[str, Any]:
        """Exchange a device code for an installation token."""

        return await self._post("pentagonhub-ha/pairing-token", {"device_code": device_code})

    async def exchange_bootstrap_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Exchange a managed bootstrap token for an installation token."""

        return await self._post("pentagonhub-ha/bootstrap-token", payload)

    async def send_heartbeat(
        self,
        installation_token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a heartbeat using the scoped installation token."""

        return await self._post(
            "pentagonhub-ha/heartbeat",
            payload,
            headers={"Authorization": f"Bearer {installation_token}"},
        )

    async def next_command(
        self,
        installation_token: str,
    ) -> dict[str, Any]:
        """Fetch the next pending HA command, if any."""

        return await self._post(
            "pentagonhub-ha/commands/next",
            {},
            headers={"Authorization": f"Bearer {installation_token}"},
        )

    async def send_command_progress(
        self,
        installation_token: str,
        command_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Report HA command progress."""

        return await self._post(
            f"pentagonhub-ha/commands/{command_id}/progress",
            payload,
            headers={"Authorization": f"Bearer {installation_token}"},
        )

    async def complete_command(
        self,
        installation_token: str,
        command_id: str,
        result_json: dict[str, Any],
    ) -> dict[str, Any]:
        """Report successful HA command completion."""

        return await self._post(
            f"pentagonhub-ha/commands/{command_id}/complete",
            {"result_json": result_json},
            headers={"Authorization": f"Bearer {installation_token}"},
        )

    async def fail_command(
        self,
        installation_token: str,
        command_id: str,
        message: str,
        *,
        error_code: str | None = None,
        result_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Report failed HA command completion."""

        payload: dict[str, Any] = {"message": message}
        if error_code:
            payload["error_code"] = error_code
        if result_json is not None:
            payload["result_json"] = result_json
        return await self._post(
            f"pentagonhub-ha/commands/{command_id}/fail",
            payload,
            headers={"Authorization": f"Bearer {installation_token}"},
        )

    async def upload_artifact(
        self,
        installation_token: str,
        upload_url: str,
        artifact_path: Path,
    ) -> dict[str, Any]:
        """Upload an artifact file to a command-scoped Core URL."""

        try:
            async with asyncio.timeout(DEFAULT_TIMEOUT_SECONDS):
                with artifact_path.open("rb") as file_obj:
                    async with self._session.post(
                        upload_url,
                        data=file_obj,
                        headers={
                            "Authorization": f"Bearer {installation_token}",
                            "Content-Type": "application/octet-stream",
                        },
                    ) as response:
                        return await _read_json_response(response)
        except TimeoutError as err:
            raise PentagonHubApiError("PentagonHub artifact upload timed out") from err
        except (ClientError, OSError) as err:
            raise PentagonHubApiError(f"PentagonHub artifact upload failed: {err}") from err

    async def download_artifact_to_path(
        self,
        installation_token: str,
        download_url: str,
        target_path: Path,
    ) -> None:
        """Download an artifact file from a command-scoped Core URL."""

        try:
            async with asyncio.timeout(DEFAULT_TIMEOUT_SECONDS):
                async with self._session.get(
                    download_url,
                    headers={"Authorization": f"Bearer {installation_token}"},
                ) as response:
                    if response.status >= 400:
                        await _read_json_response(response)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with target_path.open("wb") as file_obj:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            file_obj.write(chunk)
        except TimeoutError as err:
            raise PentagonHubApiError("PentagonHub artifact download timed out") from err
        except (ClientError, OSError) as err:
            raise PentagonHubApiError(f"PentagonHub artifact download failed: {err}") from err

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._api_base_url}/{path.lstrip('/')}"
        try:
            async with asyncio.timeout(DEFAULT_TIMEOUT_SECONDS):
                async with self._session.post(url, json=payload, headers=headers) as response:
                    return await _read_json_response(response)
        except TimeoutError as err:
            raise PentagonHubApiError("PentagonHub Core request timed out") from err
        except ClientError as err:
            raise PentagonHubApiError(f"PentagonHub Core request failed: {err}") from err


async def _read_json_response(response: ClientResponse) -> dict[str, Any]:
    try:
        body = await response.json(content_type=None)
    except ValueError as err:
        text = await response.text()
        raise PentagonHubApiError(
            f"PentagonHub Core returned a non-JSON response: {text[:200]}",
            status=response.status,
        ) from err

    if not isinstance(body, dict):
        raise PentagonHubApiError(
            "PentagonHub Core returned an unexpected response",
            status=response.status,
        )

    if response.status >= 400:
        message = body.get("message") or body.get("error") or f"HTTP {response.status}"
        if isinstance(message, list):
            message = "; ".join(str(item) for item in message)
        raise PentagonHubApiError(str(message), status=response.status)

    return body


def normalize_api_base_url(value: str) -> str:
    """Validate and normalize a PentagonHub Core API base URL."""

    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("PentagonHub Core API URL must be an absolute http(s) URL")
    return normalized


def normalize_optional_url(value: str | None) -> str | None:
    """Normalize an optional absolute URL."""

    if value is None:
        return None
    normalized = value.strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("URL must be an absolute http(s) URL")
    return normalized

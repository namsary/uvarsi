"""Small, strict Cloudflare Workers API client for the Tesco bridge repair.

The caller owns token storage.  This module deliberately never logs provider
responses or includes them in exceptions because they can contain sensitive
account details.
"""

from __future__ import annotations

import json
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping


ACCOUNT_ID = "0510a19c8c69e8354378d3198e10302f"
SCRIPT_NAME = "uvarsi-tesco-bridge"
WORKERS_DEV_SUBDOMAIN = "pumaragency"
WORKER_HOST = f"{SCRIPT_NAME}.{WORKERS_DEV_SUBDOMAIN}.workers.dev"
WORKER_URL = f"https://{WORKER_HOST}"
API_ORIGIN = "https://api.cloudflare.com/client/v4"
COMPATIBILITY_DATE = "2026-09-11"
REQUEST_TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RECOVERABLE_REPAIR_VERSIONS = 16

_VERSION_ID = re.compile(r"[A-Za-z0-9._-]{8,128}\Z")
_RELEASE = re.compile(r"[0-9a-f]{12,64}\Z")
_BRIDGE_SECRET = re.compile(r"[A-Za-z0-9._~-]+\Z")
_TAG = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_REPAIR_TAG = re.compile(r"repair-[0-9]{8}T[0-9]{6}Z\Z")

Transport = Callable[
    [str, str, Mapping[str, str], bytes | None], tuple[int, bytes]
]


@dataclass(frozen=True)
class VersionInfo:
    id: str
    number: int


@dataclass(frozen=True)
class DeployableVersion:
    id: str
    number: int
    source: str | None
    message: str | None


class CloudflareApiError(RuntimeError):
    """A deliberately redacted, stable Cloudflare failure."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def urllib_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url=url,
        data=body,
        headers=dict(headers),
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as reply:
            return reply.status, reply.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        # HTTP bodies are intentionally discarded; provider errors can contain
        # account data and must never reach logs or operator-facing exceptions.
        return error.code, b""


class CloudflareWorkerClient:
    def __init__(self, token: str, transport: Transport = urllib_transport):
        if (
            not isinstance(token, str)
            or not token
            or any(ord(char) < 0x21 or ord(char) > 0x7E for char in token)
        ):
            raise CloudflareApiError("invalid_token")
        if not callable(transport):
            raise CloudflareApiError("invalid_transport")
        self._token = token
        self._transport = transport

    @property
    def _script_path(self) -> str:
        return f"/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}"

    def _send(
        self,
        method: str,
        suffix: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> object:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        try:
            reply = self._transport(method, API_ORIGIN + suffix, headers, body)
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError):
            raise CloudflareApiError("transport_error") from None
        if (
            not isinstance(reply, tuple)
            or len(reply) != 2
            or not isinstance(reply[0], int)
            or isinstance(reply[0], bool)
            or not isinstance(reply[1], bytes)
        ):
            raise CloudflareApiError("invalid_response")
        status, raw = reply
        if status < 200 or status >= 300:
            raise CloudflareApiError(f"http_{status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise CloudflareApiError("invalid_response")
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise CloudflareApiError("invalid_response") from None
        if (
            not isinstance(envelope, dict)
            or envelope.get("success") is not True
            or "result" not in envelope
        ):
            raise CloudflareApiError("invalid_response")
        return envelope["result"]

    def _request(
        self, method: str, suffix: str, payload: dict[str, object] | None = None
    ) -> object:
        body = None
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self._send(method, suffix, body)

    def get_active_version_id(self) -> str:
        result = self._request("GET", self._script_path + "/deployments")
        if not isinstance(result, dict):
            raise CloudflareApiError("invalid_response")
        deployments = result.get("deployments")
        if not isinstance(deployments, list) or not deployments:
            raise CloudflareApiError("invalid_response")

        parsed: list[tuple[datetime, dict[str, object]]] = []
        for deployment in deployments:
            if not isinstance(deployment, dict):
                raise CloudflareApiError("invalid_response")
            created_on = deployment.get("created_on")
            versions = deployment.get("versions")
            if not isinstance(created_on, str) or not isinstance(versions, list):
                raise CloudflareApiError("invalid_response")
            try:
                timestamp = datetime.fromisoformat(created_on.replace("Z", "+00:00"))
            except ValueError:
                raise CloudflareApiError("invalid_response") from None
            if timestamp.tzinfo is None:
                raise CloudflareApiError("invalid_response")
            parsed.append((timestamp, deployment))

        active = max(parsed, key=lambda item: item[0])[1]
        versions = active["versions"]
        if len(versions) != 1 or not isinstance(versions[0], dict):
            raise CloudflareApiError("invalid_response")
        version_id = versions[0].get("version_id")
        percentage = versions[0].get("percentage")
        if (
            not isinstance(version_id, str)
            or _VERSION_ID.fullmatch(version_id) is None
            or isinstance(percentage, bool)
            or not isinstance(percentage, (int, float))
            or percentage != 100
        ):
            raise CloudflareApiError("invalid_response")
        return version_id

    def upload_version(
        self,
        worker_source: bytes,
        release: str,
        bridge_secret: str,
        base_version_id: str,
        tag: str,
    ) -> VersionInfo:
        self._validate_upload_inputs(
            worker_source, release, bridge_secret, base_version_id, tag
        )
        latest_before = self._latest_deployable_versions()
        inheritance_parent = latest_before[0]
        base_positions = [
            index
            for index, version in enumerate(latest_before)
            if version.id == base_version_id
        ]
        if (
            len(base_positions) != 1
            or base_positions[0] > MAX_RECOVERABLE_REPAIR_VERSIONS
        ):
            raise CloudflareApiError("concurrent_version")
        for index in range(base_positions[0]):
            repair_version = latest_before[index]
            predecessor = latest_before[index + 1]
            if (
                repair_version.number != predecessor.number + 1
                or repair_version.source != "api"
                or repair_version.message is None
                or _REPAIR_TAG.fullmatch(repair_version.message) is None
            ):
                raise CloudflareApiError("concurrent_version")
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": COMPATIBILITY_DATE,
            "annotations": {"workers/message": tag},
            "bindings": [
                {
                    "name": "BRIDGE_SECRET",
                    "type": "secret_text",
                    "text": bridge_secret,
                },
                {
                    "name": "WORKER_RELEASE",
                    "type": "secret_text",
                    "text": release,
                },
                {
                    "name": "TOKEN_SECRET",
                    "type": "inherit",
                    "version_id": "latest",
                },
                {"name": "CF_VERSION_METADATA", "type": "version_metadata"},
            ],
        }
        boundary = "uvarsi-" + secrets.token_hex(18)
        body = self._multipart_body(boundary, metadata, worker_source)
        result = self._send(
            "POST",
            self._script_path + "/versions?bindings_inherit=strict",
            body,
            f"multipart/form-data; boundary={boundary}",
        )
        if not isinstance(result, dict):
            raise CloudflareApiError("invalid_response")
        version_id = result.get("id")
        number = result.get("number")
        if (
            not isinstance(version_id, str)
            or _VERSION_ID.fullmatch(version_id) is None
            or not isinstance(number, int)
            or isinstance(number, bool)
            or number < 1
        ):
            raise CloudflareApiError("invalid_response")
        latest_after = self._latest_deployable_versions()
        if (
            len(latest_after) < 2
            or latest_after[0].id != version_id
            or latest_after[0].number != number
            or latest_after[1].id != inheritance_parent.id
            or latest_after[0].number != latest_after[1].number + 1
        ):
            raise CloudflareApiError("concurrent_version")
        return VersionInfo(version_id, number)

    def _latest_deployable_versions(self) -> list[DeployableVersion]:
        result = self._request(
            "GET",
            self._script_path + "/versions?deployable=true&per_page=20",
        )
        versions = result.get("items") if isinstance(result, dict) else result
        if not isinstance(versions, list) or not versions:
            raise CloudflareApiError("invalid_response")
        parsed: list[DeployableVersion] = []
        for version in versions[: MAX_RECOVERABLE_REPAIR_VERSIONS + 1]:
            if not isinstance(version, dict):
                raise CloudflareApiError("invalid_response")
            version_id = version.get("id")
            number = version.get("number")
            metadata = version.get("metadata", {})
            annotations = version.get("annotations", {})
            if (
                not isinstance(version_id, str)
                or _VERSION_ID.fullmatch(version_id) is None
                or any(item.id == version_id for item in parsed)
                or not isinstance(number, int)
                or isinstance(number, bool)
                or number < 1
                or not isinstance(metadata, dict)
                or not isinstance(annotations, dict)
            ):
                raise CloudflareApiError("invalid_response")
            source = metadata.get("source")
            message = annotations.get("workers/message")
            if (source is not None and not isinstance(source, str)) or (
                message is not None and not isinstance(message, str)
            ):
                raise CloudflareApiError("invalid_response")
            parsed.append(DeployableVersion(version_id, number, source, message))
        return parsed

    def deploy_version(
        self,
        version_id: str,
        expected_active_version: str,
        *,
        force: bool = False,
    ) -> None:
        if (
            not isinstance(version_id, str)
            or _VERSION_ID.fullmatch(version_id) is None
            or not isinstance(expected_active_version, str)
            or _VERSION_ID.fullmatch(expected_active_version) is None
        ):
            raise CloudflareApiError("invalid_version")
        if not isinstance(force, bool):
            raise CloudflareApiError("invalid_force")
        if self.get_active_version_id() != expected_active_version:
            raise CloudflareApiError("concurrent_deploy")
        payload = {
            "strategy": "percentage",
            "versions": [{"version_id": version_id, "percentage": 100}],
            "annotations": {
                "workers/message": "Activate exact Uvar.si Tesco bridge repair version",
            },
        }
        suffix = self._script_path + "/deployments"
        if force:
            suffix += "?force=true"
        result = self._request("POST", suffix, payload)
        if not isinstance(result, dict):
            raise CloudflareApiError("invalid_response")
        deployment_id = result.get("id")
        if (
            not isinstance(deployment_id, str)
            or _VERSION_ID.fullmatch(deployment_id) is None
        ):
            raise CloudflareApiError("invalid_response")
        versions = result.get("versions")
        if versions is not None:
            if (
                not isinstance(versions, list)
                or len(versions) != 1
                or not isinstance(versions[0], dict)
                or versions[0].get("version_id") != version_id
                or versions[0].get("percentage") != 100
            ):
                raise CloudflareApiError("invalid_response")
        if self.get_active_version_id() != version_id:
            raise CloudflareApiError("concurrent_deploy")

    @staticmethod
    def _validate_upload_inputs(
        worker_source: bytes,
        release: str,
        bridge_secret: str,
        base_version_id: str,
        tag: str,
    ) -> None:
        if not isinstance(worker_source, bytes) or not worker_source:
            raise CloudflareApiError("invalid_worker_source")
        if not isinstance(release, str) or _RELEASE.fullmatch(release) is None:
            raise CloudflareApiError("invalid_release")
        if (
            not isinstance(bridge_secret, str)
            or _BRIDGE_SECRET.fullmatch(bridge_secret) is None
            or not bridge_secret.startswith(release + ".")
            or len(bridge_secret.partition(".")[2]) < 32
        ):
            raise CloudflareApiError("invalid_bridge_secret")
        if (
            not isinstance(base_version_id, str)
            or _VERSION_ID.fullmatch(base_version_id) is None
        ):
            raise CloudflareApiError("invalid_version")
        if not isinstance(tag, str) or _TAG.fullmatch(tag) is None:
            raise CloudflareApiError("invalid_tag")

    @staticmethod
    def _multipart_body(
        boundary: str, metadata: dict[str, object], worker_source: bytes
    ) -> bytes:
        marker = boundary.encode("ascii")
        metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
        return b"".join(
            [
                b"--" + marker + b"\r\n",
                b'Content-Disposition: form-data; name="metadata"\r\n',
                b"Content-Type: application/json\r\n\r\n",
                metadata_bytes,
                b"\r\n--" + marker + b"\r\n",
                b'Content-Disposition: form-data; name="worker.js"; '
                b'filename="worker.js"\r\n',
                b"Content-Type: application/javascript+module\r\n\r\n",
                worker_source,
                b"\r\n--" + marker + b"--\r\n",
            ]
        )

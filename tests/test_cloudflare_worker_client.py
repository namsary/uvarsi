import json
from email.parser import BytesParser
from email.policy import default

import pytest

from hetzner.uvarsi_cloudflare_worker import (
    ACCOUNT_ID,
    SCRIPT_NAME,
    CloudflareApiError,
    CloudflareWorkerClient,
)


BASE_VERSION = "11111111-1111-4111-8111-111111111111"
NEW_VERSION = "22222222-2222-4222-8222-222222222222"
OTHER_VERSION = "33333333-3333-4333-8333-333333333333"
RELEASE = "f320e6b58243b9f06ef5f368907ffd12750533df"
BRIDGE_SECRET = RELEASE + ".abcdefghijklmnopqrstuvwxyz0123456789_-"
WORKER_SOURCE = b'export default { fetch() { return new Response("ok"); } };\n'


def response(status, payload):
    if isinstance(payload, bytes):
        return status, payload
    return status, json.dumps(payload, separators=(",", ":")).encode("utf-8")


def deployments_payload(version_id=BASE_VERSION):
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": [
            {
                "id": "deployment-1",
                "created_on": "2026-09-21T10:00:00Z",
                "versions": [{"version_id": version_id, "percentage": 100}],
            }
        ],
    }


def created_version_payload(version_id=NEW_VERSION, number=2):
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": {"id": version_id, "number": number},
    }


def deployment_payload(version_id=NEW_VERSION):
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": {
            "id": "deployment-2",
            "versions": [{"version_id": version_id, "percentage": 100}],
        },
    }


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if not self.responses:
            raise AssertionError("unexpected Cloudflare request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def decode_multipart(call):
    _, _, headers, body = call
    message = BytesParser(policy=default).parsebytes(
        b"Content-Type: "
        + headers["Content-Type"].encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    metadata = None
    modules = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        content = part.get_payload(decode=True)
        if name == "metadata":
            metadata = json.loads(content)
        else:
            modules[name] = content
    return metadata, modules


def test_client_sends_token_only_in_authorization_header():
    transport = ScriptedTransport(response(200, deployments_payload()))
    client = CloudflareWorkerClient("unit-secret", transport=transport)

    assert client.get_active_version_id() == BASE_VERSION

    method, url, headers, body = transport.calls[0]
    assert method == "GET"
    assert url.endswith(
        f"/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}/deployments"
    )
    assert headers["Authorization"] == "Bearer unit-secret"
    assert b"unit-secret" not in (body or b"")


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 500, 503])
def test_client_rejects_cloudflare_failures_without_leaking_token(status, capsys):
    transport = ScriptedTransport(response(status, b"provider body with unit-secret"))
    client = CloudflareWorkerClient("unit-secret", transport=transport)

    with pytest.raises(CloudflareApiError) as error:
        client.get_active_version_id()

    assert error.value.reason == f"http_{status}"
    assert "unit-secret" not in str(error.value)
    assert "provider body" not in str(error.value)
    captured = capsys.readouterr()
    assert "unit-secret" not in captured.out + captured.err


def test_client_rejects_invalid_success_schema():
    transport = ScriptedTransport(response(200, {"success": True, "result": {}}))
    client = CloudflareWorkerClient("unit-secret", transport=transport)

    with pytest.raises(CloudflareApiError, match="invalid_response"):
        client.get_active_version_id()


def test_client_maps_transport_timeout_without_leaking_token():
    transport = ScriptedTransport(TimeoutError("unit-secret leaked by transport"))
    client = CloudflareWorkerClient("unit-secret", transport=transport)

    with pytest.raises(CloudflareApiError) as error:
        client.get_active_version_id()

    assert error.value.reason == "transport_error"
    assert "unit-secret" not in str(error.value)


def test_upload_pins_source_and_inherits_only_token_secret():
    transport = ScriptedTransport(response(200, created_version_payload()))
    client = CloudflareWorkerClient("unit-secret", transport=transport)

    result = client.upload_version(
        WORKER_SOURCE, RELEASE, BRIDGE_SECRET, BASE_VERSION, "repair-20260921"
    )

    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url.endswith(
        f"/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}/versions"
        "?bindings_inherit=strict"
    )
    assert headers["Authorization"] == "Bearer unit-secret"
    assert b"unit-secret" not in body
    metadata, modules = decode_multipart(transport.calls[0])
    assert modules == {"worker.js": WORKER_SOURCE}
    assert metadata == {
        "main_module": "worker.js",
        "compatibility_date": "2026-09-11",
        "annotations": {"workers/message": "repair-20260921"},
        "bindings": [
            {"name": "BRIDGE_SECRET", "type": "secret_text", "text": BRIDGE_SECRET},
            {"name": "WORKER_RELEASE", "type": "secret_text", "text": RELEASE},
            {"name": "TOKEN_SECRET", "type": "inherit", "version_id": BASE_VERSION},
            {"name": "CF_VERSION_METADATA", "type": "version_metadata"},
        ],
    }
    assert result.id == NEW_VERSION
    assert result.number == 2


def test_deploy_posts_exact_new_version_after_rechecking_active_version():
    transport = ScriptedTransport(
        response(200, deployments_payload(BASE_VERSION)),
        response(200, deployment_payload(NEW_VERSION)),
    )
    client = CloudflareWorkerClient("unit-secret", transport=transport)

    client.deploy_version(NEW_VERSION, expected_active_version=BASE_VERSION)

    method, url, headers, body = transport.calls[1]
    assert method == "POST"
    assert url.endswith(
        f"/accounts/{ACCOUNT_ID}/workers/scripts/{SCRIPT_NAME}/deployments"
    )
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {
        "strategy": "percentage",
        "versions": [{"version_id": NEW_VERSION, "percentage": 100}],
        "annotations": {
            "workers/message": "Activate exact Uvar.si Tesco bridge repair version",
            "workers/triggered_by": "uvarsi-server-repair",
        },
    }


def test_deploy_refuses_changed_active_version_without_posting():
    transport = ScriptedTransport(response(200, deployments_payload(OTHER_VERSION)))
    client = CloudflareWorkerClient("unit-secret", transport=transport)

    with pytest.raises(CloudflareApiError, match="concurrent_deploy"):
        client.deploy_version(NEW_VERSION, expected_active_version=BASE_VERSION)

    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"success": True, "result": []},
        {
            "success": True,
            "result": [
                {
                    "created_on": "not-a-date",
                    "versions": [{"version_id": BASE_VERSION, "percentage": 100}],
                }
            ],
        },
        {
            "success": True,
            "result": [
                {
                    "created_on": "2026-09-21T10:00:00Z",
                    "versions": [
                        {"version_id": BASE_VERSION, "percentage": 50},
                        {"version_id": OTHER_VERSION, "percentage": 50},
                    ],
                }
            ],
        },
    ],
)
def test_active_version_requires_one_unambiguous_full_deployment(payload):
    client = CloudflareWorkerClient(
        "unit-secret", transport=ScriptedTransport(response(200, payload))
    )

    with pytest.raises(CloudflareApiError, match="invalid_response"):
        client.get_active_version_id()

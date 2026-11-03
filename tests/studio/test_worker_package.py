"""Package delivery is authorized by an unspent pairing code, not a public URL."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.enrolment import (
    MemoryStudioEnrolmentStore,
    generate_pairing_token,
    generate_worker_credential,
    hash_token,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService

FILENAME = "outbound_gpu_worker_pool-0.1.0-py3-none-any.whl"
PACKAGE = b"private worker wheel fixture"


@pytest.fixture
def package_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_WORKER_PACKAGE_DIR", str(tmp_path))
    (tmp_path / FILENAME).write_bytes(PACKAGE)
    (tmp_path / "manifest.json").write_text(json.dumps({
        "filename": FILENAME,
        "sha256": hashlib.sha256(PACKAGE).hexdigest(),
        "commit": "abc123",
    }))
    store = MemoryStudioEnrolmentStore()
    assets = MemoryAssetStore()
    runtime = InMemoryRuntime(
        MemoryJobStore(), assets,
        studio_service=StudioService(store=MemoryStudioStore(), assets=assets),
        studio_enrolment=store,
    )
    with TestClient(create_app(runtime, service_role=ServiceRole.API)) as client:
        yield client, store, tmp_path


def mint(client):
    return client.post("/studio/workers/pairing-tokens", headers={
        "X-Studio-Actor-Id": "admin-1",
    }).json()["token"]


def test_download_is_private_retryable_and_does_not_spend_code(package_client):
    client, _, _ = package_client
    token = mint(client)
    for _ in range(2):
        response = client.post("/studio/workers/package", json={"token": token})
        assert response.status_code == 200
        assert response.content == PACKAGE
        assert response.headers["X-Worker-Package-Filename"] == FILENAME
        assert response.headers["X-Worker-Package-SHA256"] == hashlib.sha256(PACKAGE).hexdigest()
        assert response.headers["X-Worker-Package-Commit"] == "abc123"
        assert "no-store" in response.headers["cache-control"]
    assert client.post("/studio/workers/redeem", json={"token": token}).status_code == 200
    assert client.post("/studio/workers/package", json={"token": token}).status_code == 400
    assert client.post("/studio/workers/redeem", json={"token": token}).status_code == 400


@pytest.mark.parametrize("token", ["not-a-token", generate_pairing_token()])
def test_guessed_codes_cannot_download(package_client, token):
    client, _, _ = package_client
    response = client.post("/studio/workers/package", json={"token": token})
    assert response.status_code == 400
    assert "no-store" in response.headers["cache-control"]
    assert PACKAGE not in response.content


def test_expired_code_cannot_download(package_client):
    client, store, _ = package_client
    token = mint(client)
    token_id = store._token_hashes[hash_token(token)]
    store._tokens[token_id] = store._tokens[token_id].model_copy(update={
        "expires_at": datetime.now(UTC) - timedelta(seconds=1),
    })
    assert client.post("/studio/workers/package", json={"token": token}).status_code == 400


def test_browser_identity_alone_cannot_download(package_client):
    client, _, _ = package_client
    response = client.post("/studio/workers/package", json={}, headers={
        "X-Studio-Actor-Id": "admin-1",
    })
    assert response.status_code >= 400
    assert client.get("/studio/workers/package").status_code >= 400


@pytest.mark.parametrize("failure", ["missing", "corrupt", "traversal"])
def test_unavailable_package_fails_closed_without_spending_code(package_client, failure):
    client, _, directory = package_client
    token = mint(client)
    if failure == "missing":
        (directory / FILENAME).unlink()
    elif failure == "corrupt":
        (directory / FILENAME).write_bytes(b"damaged")
    else:
        (directory / "manifest.json").write_text(json.dumps({
            "filename": "../private.txt", "sha256": "0" * 64,
        }))
    response = client.post("/studio/workers/package", json={"token": token})
    assert response.status_code == 503
    assert "no-store" in response.headers["cache-control"]
    assert client.post("/studio/workers/redeem", json={"token": token}).status_code == 200


def enrol(client):
    return client.post("/studio/workers/redeem", json={"token": mint(client)}).json()


def test_enrolled_machine_reads_the_manifest_and_downloads_with_its_credential(package_client):
    client, _, _ = package_client
    credential = enrol(client)["credential"]
    manifest = client.post("/studio/workers/package/manifest", json={"credential": credential})
    assert manifest.status_code == 200
    assert manifest.json() == {
        "filename": FILENAME, "sha256": hashlib.sha256(PACKAGE).hexdigest(), "commit": "abc123",
    }
    assert "no-store" in manifest.headers["cache-control"]
    response = client.post("/studio/workers/package/update", json={"credential": credential})
    assert response.status_code == 200
    assert response.content == PACKAGE
    assert response.headers["X-Worker-Package-SHA256"] == manifest.json()["sha256"]
    assert response.headers["X-Worker-Package-Commit"] == "abc123"
    assert "no-store" in response.headers["cache-control"]


@pytest.mark.parametrize("route", ["manifest", "update"])
def test_update_routes_refuse_everything_but_a_live_machine_credential(package_client, route):
    client, store, _ = package_client
    worker = enrol(client)
    pairing = mint(client)
    path = f"/studio/workers/package/{route}"
    for credential in ["not-a-credential", pairing, generate_worker_credential()]:
        response = client.post(path, json={"credential": credential})
        assert response.status_code == 400
        assert PACKAGE not in response.content
        assert "no-store" in response.headers["cache-control"]
    # A browser identity is not a machine credential either.
    assert client.post(path, json={}, headers={"X-Studio-Actor-Id": "admin-1"}).status_code >= 400
    assert client.post(path, json={"credential": worker["credential"]}).status_code == 200
    assert client.delete(f"/studio/workers/{worker['worker_id']}", headers={"X-Studio-Actor-Id": "admin-1"}).status_code == 200
    assert client.post(path, json={"credential": worker["credential"]}).status_code == 400


def test_update_fails_closed_on_a_damaged_package(package_client):
    client, _, directory = package_client
    credential = enrol(client)["credential"]
    (directory / FILENAME).write_bytes(b"damaged")
    response = client.post("/studio/workers/package/update", json={"credential": credential})
    assert response.status_code == 503
    assert b"damaged" not in response.content

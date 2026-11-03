"""API-key authentication: minting, listing, revoking, and bearer resolution."""

import pytest
from fastapi.testclient import TestClient

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.api_keys import (
    API_KEY_PREFIX,
    API_KEY_RANDOM_LENGTH,
    MemoryStudioApiKeyStore,
    generate_api_key,
    hash_api_key,
    looks_like_api_key,
    reset_last_used_throttle,
    should_write_last_used,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService


@pytest.fixture(autouse=True)
def _clear_throttle() -> None:
    reset_last_used_throttle()


def test_generated_key_has_the_documented_shape() -> None:
    key = generate_api_key()

    assert key.startswith(API_KEY_PREFIX)
    assert len(key) == len(API_KEY_PREFIX) + API_KEY_RANDOM_LENGTH
    assert looks_like_api_key(key)
    assert generate_api_key() != key


def test_create_list_and_revoke_an_api_key() -> None:
    client = _client(actor_id="owner-1")

    created = client.post("/studio/api-keys", json={"name": "laptop"})

    assert created.status_code == 200
    body = created.json()
    assert body["name"] == "laptop"
    assert body["actor_id"] == "owner-1"
    assert body["revoked_at"] is None
    assert looks_like_api_key(body["key"])

    listed = client.get("/studio/api-keys")

    assert listed.status_code == 200
    assert [item["key_id"] for item in listed.json()] == [body["key_id"]]
    assert all("key" not in item for item in listed.json())

    revoked = client.delete(f"/studio/api-keys/{body['key_id']}")

    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None


def test_another_actor_cannot_revoke_a_key_they_do_not_own() -> None:
    store = MemoryStudioApiKeyStore()
    runtime = _studio_runtime(store)
    owner = _client_for(runtime, actor_id="owner-1")
    stranger = _client_for(runtime, actor_id="owner-2")
    key_id = owner.post("/studio/api-keys", json={"name": "laptop"}).json()["key_id"]

    assert stranger.delete(f"/studio/api-keys/{key_id}").status_code == 404
    assert owner.get("/studio/api-keys").json()[0]["revoked_at"] is None


def test_bearer_key_resolves_to_its_owner_and_reads_the_owner_project() -> None:
    store = MemoryStudioApiKeyStore()
    runtime = _studio_runtime(store)
    browser = _client_for(runtime, actor_id="owner-1")
    project_id = browser.post("/studio/projects", json={"name": "Ember"}).json()[
        "project_id"
    ]
    key = browser.post("/studio/api-keys", json={"name": "cli"}).json()["key"]
    api = _bearer_client(runtime, key)

    session = api.get("/studio/auth/session")
    assert session.status_code == 200
    assert session.json()["actor_id"] == "owner-1"

    project = api.get(f"/studio/projects/{project_id}")
    assert project.status_code == 200
    assert project.json()["manifest"]["owner_id"] == "owner-1"

    assert [item["project_id"] for item in api.get("/studio/projects").json()] == [
        project_id
    ]


def test_a_key_cannot_reach_another_actors_project() -> None:
    store = MemoryStudioApiKeyStore()
    runtime = _studio_runtime(store)
    stranger_project = (
        _client_for(runtime, actor_id="owner-2")
        .post("/studio/projects", json={"name": "Theirs"})
        .json()["project_id"]
    )
    key = (
        _client_for(runtime, actor_id="owner-1")
        .post("/studio/api-keys", json={"name": "cli"})
        .json()["key"]
    )

    response = _bearer_client(runtime, key).get(f"/studio/projects/{stranger_project}")

    assert response.status_code == 404


def test_a_key_cannot_mint_or_list_keys() -> None:
    store = MemoryStudioApiKeyStore()
    runtime = _studio_runtime(store)
    key = (
        _client_for(runtime, actor_id="owner-1")
        .post("/studio/api-keys", json={"name": "cli"})
        .json()["key"]
    )
    api = _bearer_client(runtime, key)

    # The bearer header is ignored by the management routes, so with no browser
    # identity present the gateway path answers 422.
    assert api.post("/studio/api-keys", json={"name": "second"}).status_code == 422
    assert api.get("/studio/api-keys").status_code == 422


def test_revoked_unknown_and_malformed_keys_are_all_rejected() -> None:
    store = MemoryStudioApiKeyStore()
    runtime = _studio_runtime(store)
    browser = _client_for(runtime, actor_id="owner-1")
    created = browser.post("/studio/api-keys", json={"name": "cli"}).json()
    browser.delete(f"/studio/api-keys/{created['key_id']}")

    revoked = _bearer_client(runtime, created["key"]).get("/studio/auth/session")
    assert revoked.status_code == 401
    assert revoked.json()["detail"] == "revoked API key"

    unknown = _bearer_client(runtime, generate_api_key()).get("/studio/auth/session")
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == "unknown API key"

    for malformed in ("not-a-key", "mvs_short", f"{API_KEY_PREFIX}{'!' * 32}"):
        response = _bearer_client(runtime, malformed).get("/studio/auth/session")
        assert response.status_code == 401, malformed
        assert response.json()["detail"] == "malformed API key"


def test_iap_gateway_header_path_is_unchanged_by_api_keys() -> None:
    client = _client(actor_id="owner-1")

    assert client.get("/studio/auth/session").json()["actor_id"] == "owner-1"

    anonymous = TestClient(
        create_app(_studio_runtime(MemoryStudioApiKeyStore()), service_role=ServiceRole.API)
    )
    assert anonymous.get("/studio/auth/session").status_code == 422


def test_bearer_key_authenticates_while_iap_is_the_trusted_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStudioApiKeyStore()
    runtime = _studio_runtime(store)
    key = (
        _client_for(runtime, actor_id="iap:user-1")
        .post("/studio/api-keys", json={"name": "cli"})
        .json()["key"]
    )
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TRUSTED_ACTOR_SOURCE", "iap")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_IAP_AUDIENCE", "/projects/1/x/2")
    api = _bearer_client(runtime, key)

    # No IAP assertion is present, so only the bearer key can answer this.
    assert api.get("/studio/auth/session").json()["actor_id"] == "iap:user-1"
    assert (
        TestClient(create_app(runtime, service_role=ServiceRole.API))
        .get("/studio/auth/session")
        .status_code
        == 401
    )


async def test_last_used_is_recorded_once_per_throttle_window() -> None:
    store = MemoryStudioApiKeyStore()
    runtime = _studio_runtime(store)
    key = (
        _client_for(runtime, actor_id="owner-1")
        .post("/studio/api-keys", json={"name": "cli"})
        .json()["key"]
    )
    api = _bearer_client(runtime, key)

    api.get("/studio/auth/session")
    record = await store.get_by_hash(hash_api_key(key))
    assert record is not None
    first_seen = record.last_used_at
    assert first_seen is not None

    api.get("/studio/auth/session")
    record = await store.get_by_hash(hash_api_key(key))
    assert record is not None
    assert record.last_used_at == first_seen


def test_should_write_last_used_reopens_after_the_window() -> None:
    assert should_write_last_used("key-1", now=1000.0)
    assert not should_write_last_used("key-1", now=1030.0)
    assert should_write_last_used("key-1", now=1090.0)


def _studio_runtime(api_keys: MemoryStudioApiKeyStore) -> InMemoryRuntime:
    assets = MemoryAssetStore()
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    return InMemoryRuntime(
        MemoryJobStore(),
        assets,
        studio_service=service,
        studio_api_keys=api_keys,
    )


def _client_for(runtime: InMemoryRuntime, *, actor_id: str) -> TestClient:
    return TestClient(
        create_app(runtime, service_role=ServiceRole.API),
        headers={"X-Studio-Actor-Id": actor_id},
    )


def _bearer_client(runtime: InMemoryRuntime, key: str) -> TestClient:
    return TestClient(
        create_app(runtime, service_role=ServiceRole.API),
        headers={"Authorization": f"Bearer {key}"},
    )


def _client(*, actor_id: str) -> TestClient:
    return _client_for(_studio_runtime(MemoryStudioApiKeyStore()), actor_id=actor_id)

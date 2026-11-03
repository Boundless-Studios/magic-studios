"""The enrolment routes, and the boundary between the two credentials.

Minting a pairing token is browser-only; redeeming one is deliberately open,
because the agent on the machine holds nothing but the token.
"""

import asyncio

from fastapi.testclient import TestClient
from outbound_gpu_worker_pool import MemoryAssetStore as PoolAssetStore
from outbound_gpu_worker_pool import (
    DETERMINISTIC_ECHO_CAPABILITY,
    MemoryAuditLog,
    MemoryWorkerRegistry,
)
from outbound_gpu_worker_pool import MemoryJobStore as PoolJobStore
from outbound_gpu_worker_pool.plugins import (
    DeterministicEchoPlugin,
    capability_schemas_from_plugins,
)
from outbound_gpu_worker_pool.service import WorkerPoolService

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.enrolment import (
    PAIRING_TOKEN_PREFIX,
    WORKER_CREDENTIAL_PREFIX,
    EnrolledWorkerAuthenticator,
    MemoryStudioEnrolmentStore,
    enrolled_worker_subject,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService

OWNER = "owner-1"
STRANGER = "owner-2"


def _runtime(store: MemoryStudioEnrolmentStore) -> InMemoryRuntime:
    assets = MemoryAssetStore()
    return InMemoryRuntime(
        MemoryJobStore(),
        assets,
        studio_service=StudioService(store=MemoryStudioStore(), assets=assets),
        studio_enrolment=store,
    )


def _client(runtime: InMemoryRuntime, *, actor_id: str | None = OWNER) -> TestClient:
    headers = {} if actor_id is None else {"X-Studio-Actor-Id": actor_id}
    return TestClient(create_app(runtime, service_role=ServiceRole.API), headers=headers)


def test_mint_redeem_and_list_a_machine() -> None:
    runtime = _runtime(MemoryStudioEnrolmentStore())
    client = _client(runtime)

    minted = client.post("/studio/workers/pairing-tokens")
    assert minted.status_code == 200
    token = minted.json()["token"]
    assert token.startswith(PAIRING_TOKEN_PREFIX)

    redeemed = client.post(
        "/studio/workers/redeem", json={"token": token, "label": "Desk rig"}
    )
    assert redeemed.status_code == 200
    body = redeemed.json()
    assert body["credential"].startswith(WORKER_CREDENTIAL_PREFIX)
    # The agent needs its owner too: that is what it advertises to the pool, and
    # what makes the pool hand it only this person's work.
    assert body["tenant_id"] == OWNER
    assert body["label"] == "Desk rig"

    listed = client.get("/studio/workers").json()
    assert [machine["label"] for machine in listed] == ["Desk rig"]
    # Listing must never expose the credential, only the machine.
    assert "credential" not in listed[0]


def test_a_spent_token_is_refused() -> None:
    runtime = _runtime(MemoryStudioEnrolmentStore())
    client = _client(runtime)
    token = client.post("/studio/workers/pairing-tokens").json()["token"]
    client.post("/studio/workers/redeem", json={"token": token})

    replayed = client.post("/studio/workers/redeem", json={"token": token})

    assert replayed.status_code == 400
    assert replayed.json()["detail"] == "pairing token is not usable"


def test_a_malformed_token_is_refused_identically() -> None:
    """Same status and wording as a spent one, so a guess reveals nothing."""
    runtime = _runtime(MemoryStudioEnrolmentStore())

    refused = _client(runtime).post(
        "/studio/workers/redeem", json={"token": "not-a-token"}
    )

    assert refused.status_code == 400
    assert refused.json()["detail"] == "pairing token is not usable"


def test_minting_requires_a_signed_in_person() -> None:
    """A machine credential must never mint a pairing token, or one compromised
    machine could enrol others."""
    runtime = _runtime(MemoryStudioEnrolmentStore())

    refused = _client(runtime, actor_id=None).post("/studio/workers/pairing-tokens")

    # The exact status depends on how the deployment resolves actors (422 for a
    # missing gateway header here, 401 behind a session); what must hold either
    # way is that the request is refused and no token is issued.
    assert refused.status_code >= 400
    assert "token" not in refused.json()


def test_a_stranger_can_neither_see_rename_nor_revoke_your_machine() -> None:
    store = MemoryStudioEnrolmentStore()
    runtime = _runtime(store)
    owner = _client(runtime)
    token = owner.post("/studio/workers/pairing-tokens").json()["token"]
    worker_id = owner.post("/studio/workers/redeem", json={"token": token}).json()[
        "worker_id"
    ]

    stranger = _client(runtime, actor_id=STRANGER)

    assert stranger.get("/studio/workers").json() == []
    # 404 rather than 403: a stranger's machine id must not be distinguishable
    # from one that does not exist.
    assert stranger.delete(f"/studio/workers/{worker_id}").status_code == 404
    assert (
        stranger.patch(
            f"/studio/workers/{worker_id}", json={"label": "mine now"}
        ).status_code
        == 404
    )
    assert owner.get("/studio/workers").json()[0]["revoked_at"] is None


def test_the_attach_script_and_runner_are_served_by_the_app() -> None:
    """`curl | sh` must receive a script. Serving these from the app rather than
    the CDN-backed bucket is what makes that true on the first try and after
    every change."""
    client = _client(_runtime(MemoryStudioEnrolmentStore()))

    script = client.get("/studio/attach.sh")
    runner = client.get("/studio/worker.py")
    installer = client.get("/studio/worker-install.py")

    assert script.status_code == 200
    assert script.text.startswith("#!/bin/sh")
    assert "text/plain" in script.headers["content-type"]
    assert runner.status_code == 200
    assert "class RotatingCredential" in runner.text
    assert installer.status_code == 200
    assert installer.text.startswith('"""')
    assert "text/plain" in installer.headers["content-type"]
    compile(installer.text, "worker-install.py", "exec")
    for name in ("worker-runtime.py", "worker-update.py"):
        response = client.get("/studio/" + name)
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        compile(response.text, name, "exec")


def test_windows_gets_a_command_that_windows_can_run() -> None:
    """`curl … | sh` is not a thing in PowerShell: the first Windows user got
    "The term 'sh' is not recognized"."""
    client = _client(_runtime(MemoryStudioEnrolmentStore()))

    script = client.get("/studio/attach.ps1")

    assert script.status_code == 200
    assert "param(" in script.text
    # The code arrives through the environment: a piped script takes no arguments.
    assert "$env:MVS_CODE" in script.text
    # Same guarantees as the Unix path: idempotent, and it never claims success
    # before the worker has actually started.
    assert "--allow-existing" in script.text
    assert "[agent,comfy]" in script.text
    assert "Get-ScheduledTask" in script.text


def test_the_api_documentation_is_served_rather_than_linked_to_a_private_repo() -> None:
    """The keys page linked into the repository, which for anyone without access
    was a login wall instead of an answer."""
    client = _client(_runtime(MemoryStudioEnrolmentStore()))

    docs = client.get("/studio/api.md")

    assert docs.status_code == 200
    assert "text/plain" in docs.headers["content-type"]
    assert len(docs.text) > 1000


def test_a_machine_renews_its_own_credential() -> None:
    """No session involved: holding a live credential is the authorization, which
    is what lets a machine stay attached with nobody touching a token."""
    runtime = _runtime(MemoryStudioEnrolmentStore())
    client = _client(runtime)
    token = client.post("/studio/workers/pairing-tokens").json()["token"]
    attached = client.post("/studio/workers/redeem", json={"token": token}).json()

    renewed = client.post(
        "/studio/workers/rotate", json={"credential": attached["credential"]}
    )

    assert renewed.status_code == 200
    body = renewed.json()
    assert body["credential"] != attached["credential"]
    assert body["worker_id"] == attached["worker_id"]
    assert body["tenant_id"] == OWNER


def test_a_superseded_credential_cannot_renew_again() -> None:
    """What a stolen copy looks like after the real machine has moved on: refused,
    in the same words as any other bad credential."""
    runtime = _runtime(MemoryStudioEnrolmentStore())
    client = _client(runtime)
    token = client.post("/studio/workers/pairing-tokens").json()["token"]
    original = client.post("/studio/workers/redeem", json={"token": token}).json()[
        "credential"
    ]
    client.post("/studio/workers/rotate", json={"credential": original})

    replayed = client.post("/studio/workers/rotate", json={"credential": original})

    assert replayed.status_code == 400
    assert replayed.json()["detail"] == "credential is not usable"


def test_a_pairing_code_cannot_be_used_to_renew() -> None:
    runtime = _runtime(MemoryStudioEnrolmentStore())
    client = _client(runtime)
    token = client.post("/studio/workers/pairing-tokens").json()["token"]

    refused = client.post("/studio/workers/rotate", json={"credential": token})

    assert refused.status_code == 400


def test_rename_and_revoke_a_machine() -> None:
    runtime = _runtime(MemoryStudioEnrolmentStore())
    client = _client(runtime)
    token = client.post("/studio/workers/pairing-tokens").json()["token"]
    worker_id = client.post("/studio/workers/redeem", json={"token": token}).json()[
        "worker_id"
    ]

    renamed = client.patch(f"/studio/workers/{worker_id}", json={"label": "Loft box"})
    assert renamed.status_code == 200
    assert renamed.json()["label"] == "Loft box"

    revoked = client.delete(f"/studio/workers/{worker_id}")
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None


# --- Server-approved pool enrollment -------------------------------------------

ECHO_CAPABILITY = {
    "capability_id": DETERMINISTIC_ECHO_CAPABILITY,
    "plugin_id": "deterministic-echo",
    "plugin_version": "1",
}


def _pooled_runtime(
    store: MemoryStudioEnrolmentStore,
) -> tuple[InMemoryRuntime, MemoryWorkerRegistry]:
    """A runtime whose pool authenticates enrolled machines, as production does."""
    runtime = _runtime(store)
    registry = MemoryWorkerRegistry()
    runtime.worker_pool = WorkerPoolService(
        PoolJobStore(),
        PoolAssetStore(),
        registry,
        MemoryAuditLog(),
        EnrolledWorkerAuthenticator(store, registry=registry),
        capability_schemas_from_plugins([DeterministicEchoPlugin()]),
    )
    return runtime, registry


def _redeem_new_machine(client: TestClient, label: str = "Desk rig") -> dict:
    token = client.post("/studio/workers/pairing-tokens").json()["token"]
    redeemed = client.post("/studio/workers/redeem", json={"token": token, "label": label})
    assert redeemed.status_code == 200, redeemed.text
    return redeemed.json()


def _heartbeat(client: TestClient, machine: dict, tenant_id: str | None) -> int:
    return client.post(
        "/pool/worker/v1/heartbeat",
        headers={"Authorization": f"Bearer {machine['credential']}"},
        json={
            "worker_id": machine["worker_id"],
            "tenant_id": tenant_id,
            "capabilities": [ECHO_CAPABILITY],
        },
    ).status_code


def test_a_redeemed_machine_is_enrolled_in_the_pool_under_its_owner() -> None:
    store = MemoryStudioEnrolmentStore()
    runtime, registry = _pooled_runtime(store)
    client = _client(runtime)

    machine = _redeem_new_machine(client)

    record = asyncio.run(registry.get(machine["worker_id"]))
    assert record is not None
    # The owner who minted the pairing code, never anything the machine sent.
    assert record.tenant_id == OWNER
    assert record.identity_subject == enrolled_worker_subject(machine["worker_id"])
    assert _heartbeat(client, machine, OWNER) == 200


def test_an_owner_can_attach_a_second_machine() -> None:
    """The pool keeps one worker per identity subject, so two machines of one
    owner must not share the owner as their subject."""
    store = MemoryStudioEnrolmentStore()
    runtime, registry = _pooled_runtime(store)
    client = _client(runtime)

    first = _redeem_new_machine(client, "Desk rig")
    second = _redeem_new_machine(client, "Laptop")

    assert _heartbeat(client, first, OWNER) == 200
    assert _heartbeat(client, second, OWNER) == 200
    tenants = {record.tenant_id for record in asyncio.run(registry.list())}
    assert tenants == {OWNER}


def test_an_enrolled_machine_cannot_claim_another_tenant() -> None:
    store = MemoryStudioEnrolmentStore()
    runtime, registry = _pooled_runtime(store)
    client = _client(runtime)
    machine = _redeem_new_machine(client)

    assert _heartbeat(client, machine, STRANGER) == 409
    assert _heartbeat(client, machine, None) == 409
    record = asyncio.run(registry.get(machine["worker_id"]))
    assert record is not None and record.tenant_id == OWNER


def test_a_refused_redemption_enrolls_nothing() -> None:
    store = MemoryStudioEnrolmentStore()
    runtime, registry = _pooled_runtime(store)
    client = _client(runtime)
    token = client.post("/studio/workers/pairing-tokens").json()["token"]
    client.post("/studio/workers/redeem", json={"token": token})
    enrolled = asyncio.run(registry.list())

    replayed = client.post("/studio/workers/redeem", json={"token": token})
    malformed = client.post("/studio/workers/redeem", json={"token": "mvsp_short"})

    assert replayed.status_code == 400
    assert malformed.status_code == 400
    assert asyncio.run(registry.list()) == enrolled
    assert len(enrolled) == 1


def test_a_machine_the_pool_will_not_enrol_is_revoked_not_handed_out() -> None:
    store = MemoryStudioEnrolmentStore()
    runtime, registry = _pooled_runtime(store)

    async def _refuse(worker_id: str, enrollment: object) -> None:
        raise RuntimeError("registry unavailable")

    runtime.worker_pool.enroll_worker = _refuse  # type: ignore[method-assign]
    client = _client(runtime)
    token = client.post("/studio/workers/pairing-tokens").json()["token"]

    refused = client.post("/studio/workers/redeem", json={"token": token})

    assert refused.status_code == 503
    assert "credential" not in refused.text
    machines = client.get("/studio/workers").json()
    assert len(machines) == 1 and machines[0]["revoked_at"] is not None
    assert asyncio.run(registry.list()) == ()

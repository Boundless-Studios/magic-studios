"""Product routes for pool jobs, served by the outbound-gpu-worker-pool library.

The product owns submission, replay, and cancellation of pool work; the library
owns the worker surface. Every case runs against the library's in-memory stores
through the real product app, so the DTO allowlist, `WorkerPoolService`, and the
mounted `/worker/v1` router are exercised together. Nothing is mocked.
"""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from outbound_gpu_worker_pool import (
    DETERMINISTIC_ECHO_CAPABILITY,
    MemoryAssetStore,
    MemoryAssetTransfer,
    MemoryAuditLog,
    MemoryJobStore,
    MemoryWorkerAuthenticator,
    MemoryWorkerRegistry,
    WorkerEnrollment,
    WorkerIdentity,
)
from outbound_gpu_worker_pool.agent import AgentOutcome, WorkerAgent
from outbound_gpu_worker_pool.plugins import (
    DeterministicEchoPlugin,
    capability_schemas_from_plugins,
)
from outbound_gpu_worker_pool.service import WorkerPoolService

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.memory import (
    InMemoryRuntime,
)
from magic_video_editor.memory import (
    MemoryAssetStore as ProductAssetStore,
)
from magic_video_editor.memory import (
    MemoryJobStore as ProductJobStore,
)

ECHO = DETERMINISTIC_ECHO_CAPABILITY
SOURCE_KEY = "sources/pool/job-1.bin"
OUTPUT_KEY = "outputs/pool/job-1.txt"
TEXT_ONLY_OUTPUT_KEY = "outputs/pool/job-2.txt"
SOURCE_BYTES = b"pool-source-job-1"
CREDENTIAL = "worker-token"
WORKER_ID = "worker-a"
STRANGER_CREDENTIAL = "stranger-token"
STRANGER_WORKER_ID = "someone-elses-box"
HOUSE_CREDENTIAL = "house-token"
HOUSE_WORKER_ID = "house-box"


@dataclass
class _Harness:
    application: FastAPI
    client: TestClient
    runtime: InMemoryRuntime
    service: WorkerPoolService
    assets: MemoryAssetStore
    registry: MemoryWorkerRegistry
    audit: MemoryAuditLog


def _harness(
    role: ServiceRole = ServiceRole.ALL, *, worker_tenant: str | None = None
) -> _Harness:
    """`worker_tenant` is the tenant the server approved for WORKER_ID; the pool
    admits only workers with an explicit enrollment or an existing binding."""
    runtime = InMemoryRuntime(ProductJobStore(), ProductAssetStore())
    assets = MemoryAssetStore(
        allowed_read_prefixes=("sources/pool/", "outputs/pool/"),
    )
    assets.assets[SOURCE_KEY] = SOURCE_BYTES
    assets.content_types[SOURCE_KEY] = "application/octet-stream"
    registry = MemoryWorkerRegistry()
    audit = MemoryAuditLog()
    service = WorkerPoolService(
        MemoryJobStore(),
        assets,
        registry,
        audit,
        MemoryWorkerAuthenticator(
            {
                CREDENTIAL: WorkerIdentity(WORKER_ID, f"static:{WORKER_ID}", "static"),
                # A credential identifies one machine, so testing what a person
                # may see across several machines needs one credential each.
                STRANGER_CREDENTIAL: WorkerIdentity(
                    STRANGER_WORKER_ID, f"static:{STRANGER_WORKER_ID}", "static"
                ),
                HOUSE_CREDENTIAL: WorkerIdentity(
                    HOUSE_WORKER_ID, f"static:{HOUSE_WORKER_ID}", "static"
                ),
            }
        ),
        capability_schemas_from_plugins([DeterministicEchoPlugin()]),
        enrollments={
            WORKER_ID: WorkerEnrollment(f"static:{WORKER_ID}", worker_tenant),
            STRANGER_WORKER_ID: WorkerEnrollment(
                f"static:{STRANGER_WORKER_ID}", "studio-actor-2"
            ),
            HOUSE_WORKER_ID: WorkerEnrollment(f"static:{HOUSE_WORKER_ID}", None),
        },
    )
    runtime.worker_pool = service
    application = create_app(runtime, role)
    return _Harness(
        application=application,
        # Pool job routes authenticate like the Studio routes; tests use the
        # local gateway header, which production never trusts.
        client=TestClient(application, headers={"X-Studio-Actor-Id": "studio-actor-1"}),
        runtime=runtime,
        service=service,
        assets=assets,
        registry=registry,
        audit=audit,
    )


def _request(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "idempotency_key": "pool:job-1",
        "capability_id": ECHO,
        "input_keys": [SOURCE_KEY],
        "output_key": OUTPUT_KEY,
        "payload": {"seed": 7, "label": "job-1"},
    }
    body.update(overrides)
    return body


def test_a_pool_job_is_created_replayed_and_conflicts_on_reuse() -> None:
    harness = _harness()

    created = harness.client.post("/pool/jobs", json=_request(), headers=_admin(harness))
    replay = harness.client.post("/pool/jobs", json=_request(), headers=_admin(harness))
    conflict = harness.client.post(
        "/pool/jobs", json=_request(payload={"seed": 8, "label": "job-1"}), headers=_admin(harness)
    )

    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "queued"
    assert body["capability_id"] == ECHO
    assert body["output_key"] == OUTPUT_KEY
    assert body["attempts"] == 0
    assert "claim_token" not in body
    assert replay.status_code == 200
    assert replay.json()["job_id"] == body["job_id"]
    assert conflict.status_code == 409


def test_a_pool_job_must_stay_inside_the_pool_namespace() -> None:
    harness = _harness()

    for body in (
        _request(input_keys=["sources/studio/other.bin"]),
        _request(input_keys=["sources/pool/../secret.bin"]),
        _request(output_key="outputs/studio/other.txt"),
        _request(capability_id="not a capability"),
    ):
        response = harness.client.post("/pool/jobs", json=body, headers=_admin(harness))
        assert response.status_code == 422, response.text


def test_a_pool_job_is_fetched_and_cancelled() -> None:
    harness = _harness()
    job_id = harness.client.post("/pool/jobs", json=_request(), headers=_admin(harness)).json()["job_id"]

    admin = _admin(harness)
    fetched = harness.client.get(f"/pool/jobs/{job_id}", headers=admin)
    missing = harness.client.get("/pool/jobs/00000000-0000-0000-0000-000000000009", headers=admin)
    cancelled = harness.client.post(f"/pool/jobs/{job_id}/cancel", headers=admin)

    assert fetched.status_code == 200
    assert fetched.json()["status"] == "queued"
    assert "claim_token" not in fetched.json()
    assert missing.status_code == 404
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert harness.client.get(f"/pool/jobs/{job_id}", headers=admin).json()["status"] == "cancelled"


def test_pool_routes_report_unavailable_without_a_configured_pool() -> None:
    runtime = InMemoryRuntime(ProductJobStore(), ProductAssetStore())
    client = TestClient(create_app(runtime), headers={"X-Studio-Actor-Id": "studio-actor-1"})

    created = client.post("/pool/jobs", json=_request())
    fetched = client.get("/pool/jobs/00000000-0000-0000-0000-000000000001")
    cancelled = client.post("/pool/jobs/00000000-0000-0000-0000-000000000001/cancel")
    lease = client.post(
        "/worker/v1/lease",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        json={"capability_ids": [ECHO]},
    )

    assert runtime.worker_pool is None
    assert created.status_code == 503
    assert fetched.status_code == 503
    assert cancelled.status_code == 503
    assert lease.status_code == 404
    assert client.get("/health").json()["worker_pool"] == {
        "enabled": False,
        "auth": "none",
    }


async def _run_agent_once(harness: _Harness, workspace_root: Path) -> AgentOutcome:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness.application),
        base_url="http://coordinator",
    ) as http:
        agent = WorkerAgent(
            coordinator_url="http://coordinator",
            worker_id=WORKER_ID,
            credential=lambda: CREDENTIAL,
            plugins=(DeterministicEchoPlugin(),),
            transfer=MemoryAssetTransfer(harness.assets),
            http=http,
            workspace_root=workspace_root,
        )
        return await agent.run_once()


async def test_a_worker_completes_a_pool_job_through_the_product_app(
    tmp_path: Path,
) -> None:
    harness = _harness()
    job_id = harness.client.post("/pool/jobs", json=_request(), headers=_admin(harness)).json()["job_id"]

    outcome = await _run_agent_once(harness, tmp_path / "workspaces")

    settled = harness.client.get(f"/pool/jobs/{job_id}", headers=_admin(harness))

    assert outcome is AgentOutcome.COMPLETED
    body = settled.json()
    assert body["status"] == "completed"
    assert body["attempts"] == 1
    assert body["output_byte_length"] == len(harness.assets.assets[OUTPUT_KEY])
    assert body["output_sha256"]
    assert body["error"] is None
    assert "claim_token" not in settled.text
    assert harness.assets.assets[OUTPUT_KEY].startswith(b"deterministic-echo/v1\n")


async def test_a_pool_job_with_no_inputs_is_accepted_and_completed(
    tmp_path: Path,
) -> None:
    """A text-only capability submits no input keys, so the digest is sha256(b"")."""
    harness = _harness()
    created = harness.client.post(
        "/pool/jobs",
        json=_request(
            idempotency_key="pool:job-2",
            input_keys=[],
            output_key=TEXT_ONLY_OUTPUT_KEY,
            payload={"seed": 7, "label": "job-2"},
        ),
        headers=_admin(harness),
    )

    outcome = await _run_agent_once(harness, tmp_path / "workspaces")

    assert created.status_code == 200, created.text
    settled = harness.client.get(f"/pool/jobs/{created.json()['job_id']}", headers=_admin(harness))
    assert outcome is AgentOutcome.COMPLETED
    assert settled.json()["status"] == "completed"
    empty_digest = hashlib.sha256(b"").hexdigest()
    assert harness.assets.assets[TEXT_ONLY_OUTPUT_KEY] == (
        b"deterministic-echo/v1\nlabel=job-2\nseed=7\n"
        + f"sha256={empty_digest}\n".encode()
    )


def test_only_a_coordinator_role_serves_the_worker_surface() -> None:
    lease_body = {"capability_ids": [ECHO]}
    headers = {"Authorization": f"Bearer {CREDENTIAL}"}

    coordinator = _harness(ServiceRole.COORDINATOR)
    api = _harness(ServiceRole.API)
    worker = _harness(ServiceRole.WORKER)

    assert coordinator.client.post("/pool/jobs", json=_request()).status_code == 404
    assert (
        coordinator.client.post(
            "/worker/v1/lease", headers=headers, json=lease_body
        ).status_code
        != 404
    )
    for role in (api, worker):
        assert (
            role.client.post(
                "/worker/v1/lease", headers=headers, json=lease_body
            ).status_code
            == 404
        )
    assert api.client.post("/pool/jobs", json=_request(), headers=_admin(api)).status_code == 200


def _actor() -> dict[str, str]:
    return {"X-Studio-Actor-Id": "studio-actor-1"}


ADMIN_ACTOR = "email:admin@example.com"


def _admin(harness: _Harness, enrolment=None) -> dict[str, str]:
    """Make ADMIN_ACTOR a verified operator and return its headers."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from magic_video_editor.studio.auth import MemoryStudioAuthStore, StudioUser
    from magic_video_editor.studio.enrolment import MemoryStudioEnrolmentStore
    from magic_video_editor.studio.operators import OperatorAccess

    users = MemoryStudioAuthStore()
    users._users["admin"] = StudioUser(
        user_id="admin", email="admin@example.com", created_at=datetime.now(UTC)
    )
    harness.runtime.studio_service = SimpleNamespace(
        operators=OperatorAccess(
            users,
            {"admin@example.com"},
            enrolment if enrolment is not None else MemoryStudioEnrolmentStore(),
        )
    )
    return {"X-Studio-Actor-Id": ADMIN_ACTOR}


def test_pool_status_routes_answer_for_an_authorised_studio_actor() -> None:
    harness = _harness()
    harness.client.post("/pool/jobs", json=_request(), headers=_admin(harness))

    workers = harness.client.get("/pool/workers", headers=_actor())

    assert workers.status_code == 200, workers.text
    assert workers.json() == {"workers": [], "cluster_admin": False}


def test_the_global_queue_is_for_pool_administrators_only() -> None:
    """The queue aggregate spans every tenant's work, so an ordinary signed-in
    actor is refused rather than shown other people's load."""
    harness = _harness()
    harness.client.post("/pool/jobs", json=_request(), headers=_admin(harness))

    ordinary = harness.client.get("/pool/queue", headers=_actor())
    admin = harness.client.get("/pool/queue", headers=_admin(harness))

    assert ordinary.status_code == 403
    assert "queued" not in ordinary.text
    assert admin.status_code == 200, admin.text
    body = admin.json()
    assert body["queued"] == 1
    assert body["processing"] == 0
    assert body["by_capability"][ECHO] == {"queued": 1, "processing": 0}


def test_the_pool_admin_gate_fails_closed_without_operator_access() -> None:
    """No studio service means nobody can be verified as an admin, not that
    everybody is one."""
    harness = _harness()
    assert harness.runtime.studio_service is None

    assert harness.client.get("/pool/queue", headers=_actor()).status_code == 403
    assert (
        harness.client.get(
            "/pool/queue", headers={"X-Studio-Actor-Id": ADMIN_ACTOR}
        ).status_code
        == 403
    )


def _heartbeat(
    harness, worker_id: str, tenant_id: str | None, credential: str = CREDENTIAL
) -> None:
    response = harness.client.post(
        "/worker/v1/heartbeat",
        headers={"Authorization": f"Bearer {credential}"},
        json={
            "worker_id": worker_id,
            "tenant_id": tenant_id,
            "capabilities": [
                {
                    "capability_id": ECHO,
                    "plugin_id": "deterministic-echo",
                    "plugin_version": "1",
                }
            ],
        },
    )
    # A silent registration failure would make every assertion below pass for
    # the wrong reason, so the heartbeat itself has to be checked.
    assert response.status_code == 200, response.text


def _register_three_machines(harness) -> None:
    _heartbeat(harness, WORKER_ID, "studio-actor-1")
    _heartbeat(harness, STRANGER_WORKER_ID, "studio-actor-2", STRANGER_CREDENTIAL)
    _heartbeat(harness, HOUSE_WORKER_ID, None, HOUSE_CREDENTIAL)


def test_an_enrolled_machine_can_reach_the_worker_endpoints_through_the_studio() -> None:
    """A machine enrolled through the studio holds an `mvsw_` credential, not a
    Google identity, so it cannot pass the coordinator's Cloud Run IAM -- it is
    rejected before the app ever sees it. The same endpoints are therefore served
    under /pool, which the load balancer already routes to the API."""
    harness = _harness()

    lease = harness.client.post(
        "/pool/worker/v1/lease",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        json={"capability_ids": [ECHO]},
    )
    unauthenticated = harness.client.post(
        "/pool/worker/v1/lease", json={"capability_ids": [ECHO]}
    )

    # The claim is reachability, not a successful lease: before this the agent
    # got `404 Not Found for /pool/worker/v1/heartbeat` and could never attach.
    assert lease.status_code != 404, lease.text
    # Still authenticated: the path is public, the endpoint is not.
    assert unauthenticated.status_code in {401, 403}


def test_the_gpu_strip_shows_your_machines_and_the_house_ones_only() -> None:
    """Someone else's hardware must not reach this browser at all -- not merely
    be filtered out of the display."""
    harness = _harness(worker_tenant="studio-actor-1")
    _register_three_machines(harness)

    rows = harness.client.get("/pool/workers", headers=_actor()).json()["workers"]

    assert {row["worker_id"] for row in rows} == {WORKER_ID, HOUSE_WORKER_ID}


def test_each_machine_lists_what_it_can_run_and_why_not() -> None:
    harness = _harness(worker_tenant="studio-actor-1")
    _register_three_machines(harness)

    rows = harness.client.get("/pool/workers", headers=_actor()).json()["workers"]

    for row in rows:
        assert [f["key"] for f in row["features"]] == [
            "subjects", "motions", "movie_takes", "movie_processing"]
        for feature in row["features"]:
            assert feature["available"] is (feature["reason"] is None)


def test_asking_for_another_actors_machines_is_ignored() -> None:
    """The library route takes tenant_id as a query parameter; the studio's own
    route must decide from the caller's identity instead."""
    harness = _harness(worker_tenant="studio-actor-1")
    _register_three_machines(harness)

    rows = harness.client.get(
        f"/pool/workers?tenant_id={STRANGER_WORKER_ID}", headers=_actor()
    ).json()["workers"]

    assert STRANGER_WORKER_ID not in {row["worker_id"] for row in rows}


def test_pool_status_routes_report_a_worker_with_gpu_telemetry() -> None:
    harness = _harness()
    harness.client.post(
        "/worker/v1/heartbeat",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        json={
            "worker_id": WORKER_ID,
            "capabilities": [
                {
                    "capability_id": ECHO,
                    "plugin_id": "deterministic-echo",
                    "plugin_version": "1",
                }
            ],
            "gpus": [
                {
                    "index": 0,
                    "name": "NVIDIA RTX 4090",
                    "utilization_pct": 42,
                    "memory_used_mb": 8192,
                    "memory_total_mb": 24576,
                }
            ],
        },
    )

    workers = harness.client.get("/pool/workers", headers=_actor())

    assert workers.status_code == 200, workers.text
    rows = workers.json()["workers"]
    assert len(rows) == 1
    row = rows[0]
    assert row["worker_id"] == WORKER_ID
    assert row["status"] in {"online", "busy"}
    assert row["capabilities"] == [ECHO]
    assert row["gpus"] == [
        {
            "index": 0,
            "name": "NVIDIA RTX 4090",
            "utilization_pct": 42,
            "memory_used_mb": 8192,
            "memory_total_mb": 24576,
        }
    ]
    assert row["draining"] is False


def test_pool_status_routes_fail_closed_without_a_studio_actor() -> None:
    anonymous = TestClient(_harness().application)

    assert anonymous.get("/pool/workers").status_code == 422
    assert anonymous.get("/pool/queue").status_code == 422


def test_pool_status_routes_follow_the_pool_jobs_roles() -> None:
    for role in (ServiceRole.COORDINATOR, ServiceRole.WORKER):
        harness = _harness(role)
        admin = _admin(harness)
        assert harness.client.get("/pool/workers", headers=admin).status_code == 404
        assert harness.client.get("/pool/queue", headers=admin).status_code == 404
    api = _harness(ServiceRole.API)
    assert api.client.get("/pool/workers", headers=_actor()).status_code == 200
    assert api.client.get("/pool/queue", headers=_admin(api)).status_code == 200


def test_admin_sees_full_cluster_without_changing_non_admin_visibility():
    from datetime import UTC, datetime

    from magic_video_editor.studio.enrolment import EnrolledWorker, MemoryStudioEnrolmentStore

    harness = _harness(worker_tenant="studio-actor-1")
    _register_three_machines(harness)
    enrolment = MemoryStudioEnrolmentStore()
    enrolment._workers[STRANGER_WORKER_ID] = EnrolledWorker(worker_id=STRANGER_WORKER_ID, owner_id='studio-actor-2', label='Colleague desktop', created_at=datetime.now(UTC))
    response = harness.client.get('/pool/workers', headers=_admin(harness, enrolment))
    assert response.status_code == 200
    assert response.json()['cluster_admin'] is True
    assert {row['worker_id'] for row in response.json()['workers']} == {WORKER_ID, HOUSE_WORKER_ID, STRANGER_WORKER_ID}
    colleague = next(row for row in response.json()['workers'] if row['worker_id'] == STRANGER_WORKER_ID)
    assert colleague['label'] == 'Colleague desktop'
    assert 'owner_id' not in colleague
    ordinary = harness.client.get('/pool/workers', headers=_actor()).json()
    assert ordinary['cluster_admin'] is False
    assert STRANGER_WORKER_ID not in {row['worker_id'] for row in ordinary['workers']}


def test_a_worker_cannot_join_under_a_tenant_it_was_not_approved_for() -> None:
    """Tenant placement is server-approved: a machine declaring someone else's
    tenant on its heartbeat is refused rather than bound to it."""
    harness = _harness(worker_tenant="studio-actor-1")

    response = harness.client.post(
        "/worker/v1/heartbeat",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        json={
            "worker_id": WORKER_ID,
            "tenant_id": "studio-actor-2",
            "capabilities": [{"capability_id": ECHO, "plugin_id": "deterministic-echo", "plugin_version": "1"}],
        },
    )

    assert response.status_code == 409
    assert asyncio.run(harness.registry.get(WORKER_ID)) is None


def test_an_unenrolled_worker_is_refused_before_it_can_register() -> None:
    harness = _harness()
    harness.service = WorkerPoolService(
        MemoryJobStore(),
        harness.assets,
        harness.registry,
        harness.audit,
        MemoryWorkerAuthenticator(
            {CREDENTIAL: WorkerIdentity(WORKER_ID, f"static:{WORKER_ID}", "static")}
        ),
        capability_schemas_from_plugins([DeterministicEchoPlugin()]),
    )
    harness.runtime.worker_pool = harness.service
    client = TestClient(create_app(harness.runtime))

    response = client.post(
        "/worker/v1/heartbeat",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        json={
            "worker_id": WORKER_ID,
            "tenant_id": None,
            "capabilities": [{"capability_id": ECHO, "plugin_id": "deterministic-echo", "plugin_version": "1"}],
        },
    )

    assert response.status_code == 401
    assert asyncio.run(harness.registry.get(WORKER_ID)) is None


def test_pool_job_routes_need_a_verified_actor_and_only_an_admin_may_submit() -> None:
    """A pool job is untenanted house work whose payload prompt screening never
    sees, so an ordinary signed-in account (invited or public) cannot submit one."""
    harness = _harness()
    anonymous = TestClient(harness.application)
    assert anonymous.post("/pool/jobs", json=_request()).status_code in {401, 422}
    job_id = harness.client.post("/pool/jobs", json=_request(), headers=_admin(harness)).json()["job_id"]
    assert anonymous.get(f"/pool/jobs/{job_id}").status_code in {401, 422}
    assert anonymous.post(f"/pool/jobs/{job_id}/cancel").status_code in {401, 422}

    refused = harness.client.post("/pool/jobs", json=_request(idempotency_key="pool:job-2"))

    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "pool_admin_required"


def test_pool_work_for_a_studio_project_answers_only_to_its_owner() -> None:
    """A subject's FLUX job has no Studio job row of its own; its output key
    names the project, and only that project's owner may read or cancel it."""
    from outbound_gpu_worker_pool.contracts import JobSubmission as PoolSubmission

    from magic_video_editor.studio.memory import MemoryStudioStore
    from magic_video_editor.studio.service import StudioService

    harness = _harness()
    studio = StudioService(store=MemoryStudioStore(), assets=harness.runtime.assets)
    harness.runtime.studio_service = studio
    project = asyncio.run(studio.create_project("Ember", "studio-actor-1"))
    job = asyncio.run(harness.service.submit(PoolSubmission(
        job_id="00000000-0000-0000-0000-00000000f1a2",
        idempotency_key="subject:1",
        capability_id=ECHO,
        input_keys=(SOURCE_KEY,),
        output_key=f"outputs/studio/{project.project_id}/subjects/op.raw.png",
        payload={"seed": 1, "label": "subject"},
    )))
    stranger = {"X-Studio-Actor-Id": "studio-actor-2"}

    assert harness.client.get(f"/pool/jobs/{job.job_id}").status_code == 200
    assert harness.client.get(f"/pool/jobs/{job.job_id}", headers=stranger).status_code == 404
    cancel = harness.client.post(f"/pool/jobs/{job.job_id}/cancel", headers=stranger)
    assert cancel.status_code == 404
    assert asyncio.run(harness.service.get(job.job_id)).status != "cancelled"


def test_house_pool_work_answers_only_to_cluster_admins() -> None:
    """A house job's id is visible to anyone who can see a busy house worker, so
    the job itself (its payload and prompt, and the power to cancel it) must stay
    with the admins who alone may submit such work."""
    harness = _harness()
    admin = _admin(harness)
    job_id = harness.client.post("/pool/jobs", json=_request(), headers=admin).json()["job_id"]
    member = {"X-Studio-Actor-Id": "studio-actor-1"}

    assert harness.client.get(f"/pool/jobs/{job_id}", headers=member).status_code == 404
    assert harness.client.post(f"/pool/jobs/{job_id}/cancel", headers=member).status_code == 404
    assert asyncio.run(harness.service.get(job_id)).status == "queued"
    assert harness.client.get(f"/pool/jobs/{job_id}", headers=admin).status_code == 200

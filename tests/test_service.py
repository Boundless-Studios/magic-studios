import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from outbound_gpu_worker_pool import MemoryWorkerRegistry, WorkerAuthError
from outbound_gpu_worker_pool.auth import GoogleIdTokenWorkerAuthenticator
from outbound_gpu_worker_pool.service import WorkerPoolService

from magic_video_editor.contracts import (
    JobOperation,
    JobStatus,
    JobSubmission,
    SpriteSheetMetadata,
    VideoDerivatives,
    VideoJobResult,
)
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore


_PRODUCTION_ENVIRONMENT = {
    "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
    "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
    "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
    "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
    "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
    "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
    "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
    "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
}


def _static_worker_auth(tokens: str) -> dict[str, str]:
    return {
        "MAGIC_VIDEO_EDITOR_WORKER_AUTH": "static",
        "MAGIC_VIDEO_EDITOR_WORKER_TOKENS": tokens,
    }


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


class _FailOnceDispatcher(_Dispatcher):
    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)
        if len(self.job_ids) == 1:
            raise OSError("transient Cloud Tasks failure")


# The legacy job and upload routes authenticate like every other route; in the
# tests' gateway mode that is the actor header.
ACTOR = {"X-Studio-Actor-Id": "owner-1"}

def test_job_api_is_idempotent_and_cancellable() -> None:
    dispatcher = _Dispatcher()
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    runtime.dispatcher = dispatcher
    client = TestClient(_service_module().create_app(runtime), headers=ACTOR)
    request = {
        "idempotency_key": "request-1",
        "operation": "package",
        "source_key": "sources/input.mp4",
        "output_key": "outputs/final.mp4",
        "requested_outputs": {"alpha": True},
    }

    created = client.post("/jobs", json=request)
    duplicate = client.post("/jobs", json=request)
    job_id = created.json()["job_id"]
    fetched = client.get(f"/jobs/{job_id}")
    cancelled = client.post(f"/jobs/{job_id}/cancel")

    assert created.status_code == 200
    assert duplicate.json()["job_id"] == job_id
    assert dispatcher.job_ids == [job_id, job_id]
    assert fetched.json()["status"] == "queued"
    assert cancelled.json()["status"] == "cancelled"


def test_package_idempotency_includes_requested_outputs() -> None:
    dispatcher = _Dispatcher()
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    runtime.dispatcher = dispatcher
    client = TestClient(_service_module().create_app(runtime), headers=ACTOR)
    request = {
        "idempotency_key": "request-1",
        "operation": "package",
        "source_key": "sources/input.mp4",
        "output_key": "outputs/final.mp4",
        "requested_outputs": {"alpha": True},
    }

    assert client.post("/jobs", json=request).status_code == 200
    conflict = client.post(
        "/jobs",
        json={**request, "requested_outputs": {"alpha": False}},
    )

    assert conflict.status_code == 409
    assert dispatcher.job_ids == [next(iter(runtime.jobs.records))]


def test_job_api_retry_recovers_a_failed_dispatch() -> None:
    dispatcher = _FailOnceDispatcher()
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    runtime.dispatcher = dispatcher
    client = TestClient(
        _service_module().create_app(runtime),
        raise_server_exceptions=False,
        headers=ACTOR,
    )
    request = {
        "idempotency_key": "request-1",
        "operation": "package",
        "source_key": "sources/input.mp4",
        "output_key": "outputs/final.mp4",
    }

    failed = client.post("/jobs", json=request)
    retried = client.post("/jobs", json=request)

    assert failed.status_code == 500
    assert retried.status_code == 200
    assert dispatcher.job_ids == [retried.json()["job_id"]] * 2


def test_job_api_rejects_nonlocal_asset_keys() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    client = TestClient(_service_module().create_app(runtime), headers=ACTOR)

    response = client.post(
        "/jobs",
        json={
            "idempotency_key": "request-1",
            "operation": "package",
            "source_key": "gs://other-project-media/input.mp4",
            "output_key": "outputs/final.mp4",
        },
    )

    assert response.status_code == 422


def test_job_api_rejects_an_unsupported_operation() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    client = TestClient(_service_module().create_app(runtime))

    response = client.post(
        "/jobs",
        json={
            "idempotency_key": "request-1",
            "operation": "generate",
            "source_key": "sources/input.mp4",
            "output_key": "outputs/final.mp4",
        },
    )

    assert response.status_code == 422


def _motion_request() -> dict[str, object]:
    return {
        "idempotency_key": "project-1:motion-1:attempt-1",
        "operation": "generate_motion",
        "operation_version": 1,
        "project_id": "project-1",
        "manifest_operation_id": "operation-1",
        "source_key": "sources/studio/project-1/subjects/canonical.png",
        "output_key": "outputs/studio/project-1/motions/motion-1/operation-1.mp4",
        "payload": {
            "subject_key": "sources/studio/project-1/subjects/canonical.png",
            "motion_id": "motion-1",
            "prompt": "subtle breathing idle",
            "loop": True,
            "requested_revision": 2,
            "subject_revision_id": "subject-1",
            "subject_artifact_key": "sources/studio/project-1/subjects/canonical.png",
            "subject_sha256": "a" * 64,
            "motion_spec_digest": "b" * 64,
        },
    }


class _OwnedProjectService:
    async def get_project(self, project_id: str, actor_id: str):
        if (project_id, actor_id) in {
            ("project-1", "owner-1"),
            ("project-2", "owner-2"),
        }:
            return object()
        return None


def test_generic_motion_submission_is_rejected_before_persistence() -> None:
    dispatcher = _Dispatcher()
    runtime = InMemoryRuntime(
        MemoryJobStore(),
        MemoryAssetStore(),
        studio_service=_OwnedProjectService(),
    )
    runtime.dispatcher = dispatcher
    client = TestClient(_service_module().create_app(runtime))

    assert client.post("/jobs", json=_motion_request()).status_code == 422
    runtime.gpu = object()
    runtime.motion_operations_enabled = True
    assert client.post("/jobs", json=_motion_request()).status_code == 422
    assert (
        client.post(
            "/jobs",
            json=_motion_request(),
            headers={"X-Studio-Actor-Id": "not-owner"},
        ).status_code
        == 422
    )
    rejected = client.post(
        "/jobs",
        json=_motion_request(),
        headers={"X-Studio-Actor-Id": "owner-1"},
    )

    assert rejected.status_code == 422
    assert runtime.jobs.records == {}
    assert dispatcher.job_ids == []


def test_motion_submission_rejects_non_v1_payload_before_dispatch() -> None:
    dispatcher = _Dispatcher()
    runtime = InMemoryRuntime(
        MemoryJobStore(),
        MemoryAssetStore(),
        studio_service=_OwnedProjectService(),
    )
    runtime.motion_operations_enabled = True
    runtime.gpu = object()
    runtime.dispatcher = dispatcher
    client = TestClient(_service_module().create_app(runtime))

    response = client.post(
        "/jobs",
        json={**_motion_request(), "operation_version": 2},
        headers={"X-Studio-Actor-Id": "owner-1"},
    )

    assert response.status_code == 422
    assert dispatcher.job_ids == []


def test_any_project_linked_submission_requires_verified_ownership() -> None:
    runtime = InMemoryRuntime(
        MemoryJobStore(),
        MemoryAssetStore(),
        studio_service=_OwnedProjectService(),
    )
    runtime.dispatcher = _Dispatcher()
    client = TestClient(_service_module().create_app(runtime))
    request = {
        "idempotency_key": "project-1:package-1",
        "operation": "package",
        "project_id": "project-1",
        "source_key": "sources/input.mp4",
        "output_key": "outputs/final.mp4",
    }

    # No identity at all is refused before the project is even looked at (422 is
    # the gateway dependency's answer; the session mode answers 401).
    assert client.post("/jobs", json=request).status_code == 422
    assert (
        client.post(
            "/jobs",
            json=request,
            headers={"X-Studio-Actor-Id": "not-owner"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/jobs",
            json=request,
            headers={"X-Studio-Actor-Id": "owner-1"},
        ).status_code
        == 200
    )


def test_job_api_rejects_unknown_fields_and_cross_project_motion_keys() -> None:
    runtime = InMemoryRuntime(
        MemoryJobStore(),
        MemoryAssetStore(),
        studio_service=_OwnedProjectService(),
    )
    runtime.motion_operations_enabled = True
    runtime.gpu = object()
    runtime.dispatcher = _Dispatcher()
    client = TestClient(_service_module().create_app(runtime))
    typo = {
        **_motion_request(),
        "payload": {**_motion_request()["payload"], "promtp": "typo"},
    }
    cross_project = {
        **_motion_request(),
        "source_key": "sources/studio/project-2/subjects/canonical.png",
        "payload": {
            **_motion_request()["payload"],
            "subject_key": "sources/studio/project-2/subjects/canonical.png",
        },
    }

    assert (
        client.post(
            "/jobs", json=typo, headers={"X-Studio-Actor-Id": "owner-1"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/jobs", json=cross_project, headers={"X-Studio-Actor-Id": "owner-1"}
        ).status_code
        == 422
    )


def test_job_api_rejects_unbounded_requested_outputs() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    client = TestClient(_service_module().create_app(runtime), headers=ACTOR)
    request = {
        "idempotency_key": "request-1",
        "operation": "package",
        "source_key": "sources/input.mp4",
        "output_key": "outputs/final.mp4",
    }

    too_many = client.post(
        "/jobs",
        json={
            **request,
            "requested_outputs": {f"output-{index}": True for index in range(17)},
        },
    )
    long_key = client.post(
        "/jobs",
        json={
            **request,
            "requested_outputs": {"x" * 65: True},
        },
    )
    wrong_value = client.post(
        "/jobs",
        json={
            **request,
            "requested_outputs": {"alpha": "yes"},
        },
    )

    assert too_many.status_code == 422
    assert long_key.status_code == 422
    assert wrong_value.status_code == 422


def test_generic_motion_endpoint_cannot_bypass_studio_ownership() -> None:
    runtime = InMemoryRuntime(
        MemoryJobStore(),
        MemoryAssetStore(),
        studio_service=_OwnedProjectService(),
    )
    runtime.motion_operations_enabled = True
    runtime.gpu = object()
    runtime.dispatcher = _Dispatcher()
    client = TestClient(_service_module().create_app(runtime))
    first = client.post(
        "/jobs",
        json=_motion_request(),
        headers={"X-Studio-Actor-Id": "owner-1"},
    )
    second_request = {
        **_motion_request(),
        "project_id": "project-2",
        "source_key": "sources/studio/project-2/subjects/canonical.png",
        "output_key": "outputs/studio/project-2/motions/motion-1/operation-1.mp4",
        "payload": {
            **_motion_request()["payload"],
            "subject_key": "sources/studio/project-2/subjects/canonical.png",
        },
    }
    conflict = client.post(
        "/jobs",
        json=second_request,
        headers={"X-Studio-Actor-Id": "owner-2"},
    )

    assert first.status_code == 422
    assert conflict.status_code == 422
    assert runtime.jobs.records == {}


def test_asset_upload_api_returns_a_bucket_local_signed_target() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    client = TestClient(_service_module().create_app(runtime), headers=ACTOR)

    response = client.post(
        "/assets/uploads",
        json={"key": "sources/job.mp4", "content_type": "video/mp4"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "key": "sources/job.mp4",
        "upload_url": "memory://upload/sources/job.mp4",
    }


def test_asset_upload_api_rejects_output_keys() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    client = TestClient(_service_module().create_app(runtime))

    response = client.post(
        "/assets/uploads",
        json={"key": "outputs/job.mp4", "content_type": "video/mp4"},
    )

    assert response.status_code == 422


def _service_module():
    try:
        spec = importlib.util.find_spec("magic_video_editor.app")
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, "standalone Magic Video Editor service is missing"
    return importlib.import_module("magic_video_editor.app")


def test_standalone_service_processes_a_claimed_job() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    client = TestClient(service.create_app(runtime))

    response = client.post("/tasks/jobs/job-1")

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-1", "status": "completed"}
    output_key = runtime.completed["job-1"]
    assert output_key.startswith("outputs/job-1.")
    assert output_key.endswith(".mp4")
    assert runtime.asset_bytes[output_key] == b"source-video"


def test_duplicate_task_delivery_is_a_no_op() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    client = TestClient(service.create_app(runtime))

    assert client.post("/tasks/jobs/job-1").status_code == 200
    response = client.post("/tasks/jobs/job-1")

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-1", "status": "already_settled"}
    assert runtime.publish_count == 1


def test_retryable_failure_releases_the_job_for_redelivery() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    runtime.processor = _FailOnceProcessor()
    client = TestClient(service.create_app(runtime), raise_server_exceptions=False)

    first = client.post("/tasks/jobs/job-1")
    second = client.post("/tasks/jobs/job-1")

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json() == {"job_id": "job-1", "status": "completed"}
    assert runtime.publish_count == 3


def test_derivative_failure_preserves_the_published_source() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    runtime.processor = _AlwaysFailProcessor()
    client = TestClient(service.create_app(runtime), raise_server_exceptions=False)

    response = client.post("/tasks/jobs/job-1")

    assert response.status_code == 500
    source_keys = [key for key in runtime.asset_bytes if key.startswith("outputs/")]
    assert len(source_keys) == 1
    assert runtime.asset_bytes[source_keys[0]] == b"source-video"
    assert runtime.publish_count == 1


def test_derivative_retry_reuses_source_and_publishes_artifacts_once() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    runtime.processor = _FailOnceProcessor()
    client = TestClient(service.create_app(runtime), raise_server_exceptions=False)

    first = client.post("/tasks/jobs/job-1")
    second = client.post("/tasks/jobs/job-1")

    assert first.status_code == 500
    assert second.status_code == 200
    output_key = runtime.completed["job-1"]
    assert runtime.asset_bytes[output_key] == b"source-video"
    assert (
        runtime.asset_bytes[output_key.removesuffix(".mp4") + ".alpha.webm"] == b"alpha"
    )
    assert (
        runtime.asset_bytes[output_key.removesuffix(".mp4") + ".sprite.png"]
        == b"sprite"
    )
    assert runtime.publish_count == 3


def test_unrequested_derivatives_are_not_published() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job("job-1", b"source-video")
    runtime.jobs.pending["job-1"] = replace(
        runtime.jobs.pending["job-1"],
        requested_outputs={"alpha": False, "sprite": True},
    )
    runtime.processor = _ArtifactProcessor()
    client = TestClient(service.create_app(runtime))

    response = client.post("/tasks/jobs/job-1")

    assert response.status_code == 200
    output_key = runtime.completed["job-1"]
    assert output_key.removesuffix(".mp4") + ".alpha.webm" not in runtime.asset_bytes
    assert output_key.removesuffix(".mp4") + ".sprite.png" in runtime.asset_bytes
    result = runtime.jobs.completed_results["job-1"]
    assert result.alpha_video_path is None
    assert result.sprite_sheet_path == output_key.removesuffix(".mp4") + ".sprite.png"
    assert result.sprite_metadata == SpriteSheetMetadata(32, 8, 4, 512, 512, 140)


def test_unpublished_sprite_omits_sprite_metadata() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job("job-1", b"source-video")
    runtime.jobs.pending["job-1"] = replace(
        runtime.jobs.pending["job-1"],
        requested_outputs={"alpha": True, "sprite": False},
    )
    runtime.processor = _ArtifactProcessor()
    client = TestClient(service.create_app(runtime))

    assert client.post("/tasks/jobs/job-1").status_code == 200
    result = runtime.jobs.completed_results["job-1"]
    assert result.alpha_video_path is not None
    assert result.sprite_sheet_path is None
    assert result.sprite_metadata is None


def test_job_lookup_and_cancellation_reject_malformed_ids() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    client = TestClient(_service_module().create_app(runtime))

    assert client.get("/jobs/not-a-uuid").status_code == 422
    assert client.post("/jobs/not-a-uuid/cancel").status_code == 422


def test_api_and_worker_roles_expose_only_their_own_routes() -> None:
    service = _service_module()
    api_runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    worker_runtime = service.InMemoryRuntime.with_job("job-1", b"source-video")
    api = TestClient(service.create_app(api_runtime, service_role="api"), headers=ACTOR)
    worker = TestClient(service.create_app(worker_runtime, service_role="worker"), headers=ACTOR)

    assert api.post("/tasks/jobs/job-1").status_code == 404
    assert worker.get("/jobs/00000000-0000-0000-0000-000000000001").status_code == 404
    assert (
        worker.post(
            "/assets/uploads",
            json={
                "key": "sources/job.mp4",
                "content_type": "video/mp4",
            },
        ).status_code
        == 404
    )


def test_completion_retry_does_not_republish_output() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    runtime.jobs.fail_next_complete = True
    runtime.processor = _ArtifactProcessor()
    client = TestClient(service.create_app(runtime), raise_server_exceptions=False)

    first = client.post("/tasks/jobs/job-1")
    second = client.post("/tasks/jobs/job-1")

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json() == {"job_id": "job-1", "status": "completed"}
    assert runtime.publish_count == 3
    result = runtime.jobs.completed_results["job-1"]
    assert result.alpha_video_path is not None
    assert result.sprite_sheet_path is not None


def test_rejected_completion_reports_claim_loss() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    runtime.jobs.reject_next_complete = True
    client = TestClient(service.create_app(runtime))

    response = client.post("/tasks/jobs/job-1")

    assert response.status_code == 200
    assert response.json() == {"job_id": "job-1", "status": "claim_lost"}
    assert runtime.completed == {}


def test_retry_budget_settles_the_persisted_source_without_derivatives() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    runtime.processor = _AlwaysFailProcessor()
    client = TestClient(service.create_app(runtime), raise_server_exceptions=False)

    responses = [client.post("/tasks/jobs/job-1") for _ in range(5)]

    assert [response.status_code for response in responses] == [500, 500, 500, 500, 200]
    assert responses[-1].json() == {"job_id": "job-1", "status": "completed"}
    output_key = runtime.completed["job-1"]
    assert runtime.asset_bytes[output_key] == b"source-video"
    assert runtime.failures == {}


async def test_stale_worker_cannot_settle_a_newer_claim() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job(
        job_id="job-1",
        source=b"source-video",
    )
    first = await runtime.jobs.claim("job-1")
    assert first is not None
    assert hasattr(first, "claim_token"), "claimed jobs must carry a fencing token"

    await runtime.jobs.release(first.job_id, first.claim_token, "retry")
    second = await runtime.jobs.claim("job-1")
    assert second is not None
    assert second.claim_token != first.claim_token

    stale_complete = await runtime.jobs.complete(
        first.job_id,
        first.claim_token,
        VideoJobResult("outputs/stale.mp4"),
    )
    stale_release = await runtime.jobs.release(
        first.job_id, first.claim_token, "stale worker"
    )

    assert stale_complete is False
    assert stale_release is False
    assert await runtime.jobs.claim("job-1") is None
    accepted = await runtime.jobs.complete(
        second.job_id,
        second.claim_token,
        VideoJobResult("outputs/current.mp4"),
    )
    assert accepted is True
    assert runtime.completed["job-1"] == "outputs/current.mp4"


class _FailOnceProcessor:
    def __init__(self) -> None:
        self.attempts = 0

    async def prepare_source(self, source: bytes) -> bytes:
        return source

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives:
        self.attempts += 1
        if self.attempts == 1:
            raise OSError("transient encoder failure")
        return VideoDerivatives(alpha_webm=b"alpha", sprite_sheet_png=b"sprite")


class _UnpreparableProcessor:
    async def prepare_source(self, source: bytes) -> bytes:
        raise OSError("persistent decoder failure")

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives:
        raise AssertionError("derivatives must not run for an unpreparable source")


def test_attempt_budget_bounds_retries_before_the_default_five() -> None:
    service = _service_module()
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    runtime.processor = _UnpreparableProcessor()
    runtime.assets_store.assets["sources/input.mp4"] = b"source-video"
    submission = JobSubmission(
        job_id=str(uuid4()),
        idempotency_key="budget-1",
        operation=JobOperation.PACKAGE,
        source_key="sources/input.mp4",
        output_key="outputs/final.mp4",
        requested_outputs={},
        attempt_budget=2,
    )
    asyncio.run(runtime.jobs.submit(submission))
    client = TestClient(service.create_app(runtime), raise_server_exceptions=False)

    first = client.post(f"/tasks/jobs/{submission.job_id}")
    second = client.post(f"/tasks/jobs/{submission.job_id}")

    assert first.status_code == 500
    assert second.status_code == 200
    assert second.json() == {"job_id": submission.job_id, "status": "failed"}
    record = runtime.jobs.records[submission.job_id]
    assert record.status is JobStatus.FAILED
    assert record.attempts == 2


class _AlwaysFailProcessor:
    async def prepare_source(self, source: bytes) -> bytes:
        return source

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives:
        raise OSError("persistent encoder failure")


class _ArtifactProcessor:
    async def prepare_source(self, source: bytes) -> bytes:
        return source

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives:
        return VideoDerivatives(
            alpha_webm=b"alpha",
            sprite_sheet_png=b"sprite",
            sprite_metadata=SpriteSheetMetadata(32, 8, 4, 512, 512, 140),
        )


class _LeasedJobStore:
    def __init__(self, error_type) -> None:
        self._error_type = error_type

    async def get(self, job_id: str):
        return None

    async def claim(self, job_id: str):
        raise self._error_type(job_id)


def test_health_fails_closed_without_a_configured_plugin() -> None:
    service = _service_module()
    client = TestClient(service.create_app(service.UnconfiguredRuntime()))

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime plugin is not configured"}


def test_health_only_advertises_motion_when_gate_and_gpu_are_ready() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    runtime.motion_operations_enabled = True
    client = TestClient(_service_module().create_app(runtime))

    assert client.get("/health").json()["capabilities"] == {
        "generate_motion_v1": False,
        "compose_frame_v1": False,
    }

    runtime.gpu = object()
    assert client.get("/health").json()["capabilities"] == {
        "generate_motion_v1": True,
        "compose_frame_v1": False,
    }


def test_task_fails_closed_without_a_configured_plugin() -> None:
    service = _service_module()
    client = TestClient(
        service.create_app(service.UnconfiguredRuntime()),
        raise_server_exceptions=False,
    )

    response = client.post("/tasks/jobs/job-1")

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime plugin is not configured"}


def test_live_lease_keeps_task_delivery_retryable() -> None:
    service = _service_module()
    runtime = service.InMemoryRuntime.with_job("job-1", b"source-video")
    runtime.jobs = _LeasedJobStore(service.JobLeaseActive)
    client = TestClient(service.create_app(runtime), raise_server_exceptions=False)

    response = client.post("/tasks/jobs/job-1")

    assert response.status_code == 503
    assert response.json() == {"detail": "video job is currently leased"}


def test_environment_builds_the_production_runtime(monkeypatch) -> None:
    service = _service_module()
    monkeypatch.setenv(
        "MAGIC_VIDEO_EDITOR_DATABASE_URL",
        "postgresql://video-worker@example.invalid/studio",
    )
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET", "magic-video-output")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TASK_PROJECT", "editor-project")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TASK_LOCATION", "us-west1")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TASK_QUEUE", "video-jobs")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_SERVICE_URL", "https://editor.invalid")
    monkeypatch.setenv(
        "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT", "tasks@editor.invalid"
    )
    monkeypatch.setenv(
        "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT", "runtime@editor.invalid"
    )

    runtime = service.runtime_from_env(dict(os.environ))

    assert runtime.ready is False
    assert runtime.jobs.__class__.__name__ == "PostgresJobStore"
    assert runtime.assets.__class__.__name__ == "GcsAssetStore"
    assert runtime.processor.__class__.__name__ == "FfmpegVideoProcessor"
    assert runtime.lifecycle is not None
    assert runtime.studio_service is not None


def test_environment_wires_studio_service_and_mounts_routes_for_api_and_all(
    monkeypatch,
) -> None:
    service = _service_module()

    class _FakeStudioStore:
        def __init__(self, database_url: str) -> None:
            self.database_url = database_url

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(service, "PostgresStudioStore", _FakeStudioStore)
    runtime = service.runtime_from_env(
        {
            "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
            "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
            "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
            "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
            "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
            "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
            "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
            "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
        }
    )

    assert runtime.studio_service is not None
    assert runtime.studio_service._assets is runtime.assets
    assert (
        runtime.studio_service._store.database_url
        == "postgresql://editor.invalid/video"
    )

    api_paths = {
        route.path for route in service.create_app(runtime, service_role="api").routes
    }
    all_paths = {
        route.path for route in service.create_app(runtime, service_role="all").routes
    }
    worker_paths = {
        route.path
        for route in service.create_app(runtime, service_role="worker").routes
    }

    assert "/studio/projects" in api_paths
    assert "/studio/projects" in all_paths
    assert "/studio/projects" not in worker_paths
    # No extension is named, so none of the hosted service's routes are served.
    assert runtime.extensions == ()
    assert "/studio/donations/options" not in api_paths
    assert "/studio/access-requests" not in api_paths
    assert runtime.lifecycle._components[-1] is runtime.processor


def test_an_extension_named_but_not_installed_stops_the_service(monkeypatch) -> None:
    """A deployment that relies on an extension must never start without it."""
    import pytest

    from magic_video_editor.extensions import ExtensionError

    service = _service_module()
    with pytest.raises(ExtensionError, match="not-installed"):
        service.runtime_from_env({
            "MAGIC_VIDEO_EDITOR_EXTENSIONS": "not-installed",
            "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
            "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
            "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
            "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
            "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
            "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
            "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
            "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
        })


def test_api_exposes_motion_generation_when_rollout_is_enabled_without_local_gpu() -> None:
    service = _service_module()
    runtime = service.runtime_from_env(
        {
            "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
            "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
            "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
            "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
            "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
            "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
            "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
            "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
            "MAGIC_VIDEO_EDITOR_MOTION_OPERATIONS_ENABLED": "true",
        },
        service_role="api",
    )

    assert runtime.gpu is None
    assert runtime.studio_service is not None
    assert runtime.studio_service._motion_generation_available is True


def test_worker_runtime_does_not_construct_a_studio_store(monkeypatch) -> None:
    service = _service_module()
    constructed: list[str] = []

    class _ForbiddenStudioStore:
        def __init__(self, database_url: str) -> None:
            constructed.append(database_url)

    monkeypatch.setattr(service, "PostgresStudioStore", _ForbiddenStudioStore)

    runtime = service.runtime_from_env(
        {
            "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
            "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
            "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
            "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
            "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
            "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
            "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
            "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
        },
        service_role="worker",
    )

    assert runtime.studio_service is None
    assert constructed == []


async def test_production_lifecycle_rolls_back_started_dependencies_on_studio_failure() -> (
    None
):
    service = _service_module()
    calls: list[str] = []

    class _StartStop:
        def __init__(self, name: str, *, fail_start: bool = False) -> None:
            self.name = name
            self.fail_start = fail_start

        async def start(self) -> None:
            calls.append(f"start:{self.name}")
            if self.fail_start:
                raise RuntimeError(f"{self.name} failed")

        async def stop(self) -> None:
            calls.append(f"stop:{self.name}")

    lifecycle = service.ProductionLifecycle(
        _StartStop("jobs"),
        _StartStop("assets"),
        _StartStop("processor"),
        _StartStop("studio", fail_start=True),
    )

    with pytest.raises(RuntimeError, match="studio failed"):
        await lifecycle.start()

    assert calls == [
        "start:jobs",
        "start:studio",
        "stop:jobs",
    ]


async def test_production_lifecycle_preserves_startup_error_when_cleanup_also_fails() -> (
    None
):
    service = _service_module()
    calls: list[str] = []

    class _StartStop:
        def __init__(
            self,
            name: str,
            *,
            fail_start: bool = False,
            fail_stop: bool = False,
        ) -> None:
            self.name = name
            self.fail_start = fail_start
            self.fail_stop = fail_stop

        async def start(self) -> None:
            calls.append(f"start:{self.name}")
            if self.fail_start:
                raise RuntimeError(f"{self.name} failed")

        async def stop(self) -> None:
            calls.append(f"stop:{self.name}")
            if self.fail_stop:
                raise RuntimeError(f"stop {self.name} failed")

    lifecycle = service.ProductionLifecycle(
        _StartStop("jobs", fail_stop=True),
        _StartStop("assets"),
        _StartStop("processor"),
        _StartStop("studio", fail_start=True),
    )

    with pytest.raises(RuntimeError, match="studio failed"):
        await lifecycle.start()

    assert calls == [
        "start:jobs",
        "start:studio",
        "stop:jobs",
    ]


async def test_production_lifecycle_stop_is_best_effort() -> None:
    service = _service_module()
    calls: list[str] = []

    class _StartStop:
        def __init__(self, name: str, *, fail_stop: bool = False) -> None:
            self.name = name
            self.fail_stop = fail_stop

        async def start(self) -> None:
            calls.append(f"start:{self.name}")

        async def stop(self) -> None:
            calls.append(f"stop:{self.name}")
            if self.fail_stop:
                raise RuntimeError(f"stop {self.name} failed")

    lifecycle = service.ProductionLifecycle(
        _StartStop("jobs"),
        _StartStop("assets", fail_stop=True),
        _StartStop("processor"),
        _StartStop("studio"),
    )

    with pytest.raises(RuntimeError, match="stop assets failed"):
        await lifecycle.stop()

    assert calls == [
        "stop:processor",
        "stop:assets",
        "stop:studio",
        "stop:jobs",
    ]


def test_environment_builds_cloud_sql_url_from_plugin_components() -> None:
    service = _service_module()

    runtime = service.runtime_from_env(
        {
            "MAGIC_VIDEO_EDITOR_DATABASE_PASSWORD": "p@ss word",
            "MAGIC_VIDEO_EDITOR_DATABASE_INSTANCE": (
                "example-project:us-west1:magic-video-editor-db"
            ),
            "MAGIC_VIDEO_EDITOR_DATABASE_USER": "magic_video_editor",
            "MAGIC_VIDEO_EDITOR_DATABASE_NAME": "magic_video_editor",
            "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
            "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
            "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
            "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
            "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
            "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
            "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
        }
    )

    assert runtime.jobs._database_url == (
        "postgresql://magic_video_editor:p%40ss%20word@/magic_video_editor?"
        "host=%2Fcloudsql%2Fexample-project%3Aus-west1%3A"
        "magic-video-editor-db"
    )


def test_environment_rejects_an_unknown_asset_plugin(monkeypatch) -> None:
    service = _service_module()
    monkeypatch.setenv(
        "MAGIC_VIDEO_EDITOR_DATABASE_URL",
        "postgresql://video-worker@example.invalid/studio",
    )
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET", "magic-video-output")
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_ASSET_PLUGIN", "filesystem")

    with pytest.raises(ValueError, match="asset plugin"):
        service.runtime_from_env(dict(os.environ))


def test_environment_selects_the_private_gpu_http_plugin(monkeypatch) -> None:
    service = _service_module()
    environment = {
        "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
        "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
        "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
        "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
        "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
        "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
        "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
        "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
        "MAGIC_VIDEO_EDITOR_GPU_PLUGIN": "http",
        "MAGIC_VIDEO_EDITOR_GPU_ENDPOINT": "https://gpu.internal/generate",
        "MAGIC_VIDEO_EDITOR_GPU_AUDIENCE": "https://gpu.internal",
        "MAGIC_VIDEO_EDITOR_MOTION_OPERATIONS_ENABLED": "true",
    }

    runtime = service.runtime_from_env(environment)

    assert runtime.gpu.__class__.__name__ == "HttpGpuProcessor"
    assert runtime.motion_operations_enabled is True


def test_environment_fails_closed_for_incomplete_gpu_http_config() -> None:
    service = _service_module()
    environment = {
        "MAGIC_VIDEO_EDITOR_DATABASE_URL": "postgresql://editor.invalid/video",
        "MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET": "studio-assets-test",
        "MAGIC_VIDEO_EDITOR_TASK_PROJECT": "editor-project",
        "MAGIC_VIDEO_EDITOR_TASK_LOCATION": "us-west1",
        "MAGIC_VIDEO_EDITOR_TASK_QUEUE": "video-jobs",
        "MAGIC_VIDEO_EDITOR_SERVICE_URL": "https://editor.invalid",
        "MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT": "tasks@editor.invalid",
        "MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT": "runtime@editor.invalid",
        "MAGIC_VIDEO_EDITOR_GPU_PLUGIN": "http",
    }

    runtime = service.runtime_from_env(environment)

    assert isinstance(runtime, service.UnconfiguredRuntime)


def test_environment_selects_the_pool_subject_plugin_when_the_pool_is_configured() -> (
    None
):
    service = _service_module()
    digest = hashlib.sha256(b"token-a").hexdigest()

    runtime = service.runtime_from_env(
        {
            **_PRODUCTION_ENVIRONMENT,
            **_static_worker_auth(f"worker-a:{digest}"),
            "MAGIC_VIDEO_EDITOR_SUBJECT_PLUGIN": "pool",
        }
    )

    assert runtime.subject_generator.__class__.__name__ == "PoolSubjectGenerator"
    assert isinstance(runtime.worker_pool, WorkerPoolService)


def test_environment_pool_subject_plugin_without_the_pool_is_unconfigured() -> None:
    service = _service_module()

    runtime = service.runtime_from_env(
        {
            **_PRODUCTION_ENVIRONMENT,
            "MAGIC_VIDEO_EDITOR_SUBJECT_PLUGIN": "pool",
        }
    )

    assert isinstance(runtime, service.UnconfiguredRuntime)


def test_environment_builds_the_library_worker_pool_for_static_tokens() -> None:
    service = _service_module()
    digest = hashlib.sha256(b"token-a").hexdigest()

    runtime = service.runtime_from_env(
        {**_PRODUCTION_ENVIRONMENT, **_static_worker_auth(f"worker-a:{digest}")}
    )

    assert isinstance(runtime.worker_pool, WorkerPoolService)
    assert runtime.worker_pool.auth_method == "static"
    # Both packaged plugins are published, so a host can see the H3 schema
    # without the coordinator ever building the ComfyUI plugin.
    capabilities = runtime.worker_pool.capabilities_schema()["capabilities"]
    assert "test.deterministic.echo.v1" in capabilities
    assert "video.minimax_h3.text_to_video.v1" in capabilities
    assert "/worker/v1/lease" in {
        route.path for route in service.create_app(runtime).routes
    }


WORKER_ACCOUNT = "gpu-worker-rig-x@editor-project.iam.gserviceaccount.com"


def _oidc_runtime(
    monkeypatch: pytest.MonkeyPatch, **environment: str
) -> tuple[object, list[GoogleIdTokenWorkerAuthenticator], list[str]]:
    service = _service_module()
    audiences: list[str] = []
    built: list[GoogleIdTokenWorkerAuthenticator] = []

    def _verify(token: str, audience: str) -> dict[str, object]:
        audiences.append(audience)
        return {"email": WORKER_ACCOUNT, "email_verified": True}

    def _authenticator(
        *, audience: str, registry: object, auto_enroll: bool = False, enrollments=None
    ) -> GoogleIdTokenWorkerAuthenticator:
        # The registry the service builds points at Cloud SQL and the verifier
        # reaches Google; everything else is the real authenticator.
        instance = GoogleIdTokenWorkerAuthenticator(
            audience=audience,
            registry=MemoryWorkerRegistry(),
            verifier=_verify,
            auto_enroll=auto_enroll,
            enrollments=enrollments,
        )
        built.append(instance)
        return instance

    monkeypatch.setattr(service, "GoogleIdTokenWorkerAuthenticator", _authenticator)
    runtime = service.runtime_from_env(
        {
            **_PRODUCTION_ENVIRONMENT,
            "MAGIC_VIDEO_EDITOR_WORKER_AUTH": "google_oidc",
            "MAGIC_VIDEO_EDITOR_WORKER_AUDIENCE": "https://coordinator.invalid",
            **environment,
        }
    )
    return runtime, built, audiences


def test_environment_refuses_an_unenrolled_worker_account_without_an_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cloud Run IAM reaching the service is no longer admission to the pool: a
    verified worker account with no registry row and no explicit enrollment is
    refused, and the service still starts."""
    runtime, built, _ = _oidc_runtime(monkeypatch)

    assert isinstance(runtime.worker_pool, WorkerPoolService)
    assert runtime.worker_pool.auth_method == "google_oidc"
    with pytest.raises(WorkerAuthError):
        asyncio.run(built[0].authenticate("Bearer signed-by-google"))


def test_environment_admits_only_an_exact_allowlisted_worker_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowlist, not the account name, chooses the worker id and tenant."""
    runtime, built, audiences = _oidc_runtime(
        monkeypatch,
        MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS=json.dumps(
            {"house-rig": {"identity_subject": WORKER_ACCOUNT, "tenant_id": None}}
        ),
    )

    assert isinstance(runtime.worker_pool, WorkerPoolService)
    identity = asyncio.run(built[0].authenticate("Bearer signed-by-google"))
    assert identity.worker_id == "house-rig"
    assert identity.subject == WORKER_ACCOUNT
    assert audiences == ["https://coordinator.invalid"]


def test_environment_rejects_a_malformed_enrollment_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A tenant key is mandatory: omitting it must not silently mean "house".
    with pytest.raises(ValueError, match="MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS"):
        _oidc_runtime(
            monkeypatch,
            MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS=json.dumps(
                {"house-rig": {"identity_subject": WORKER_ACCOUNT}}
            ),
        )


def test_environment_rejects_a_nonpositive_worker_admission_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="PER_SOURCE_LIMIT_PER_MINUTE"):
        _oidc_runtime(
            monkeypatch,
            MAGIC_VIDEO_EDITOR_WORKER_PER_SOURCE_LIMIT_PER_MINUTE="0",
        )


def test_google_oidc_worker_auth_without_an_audience_is_unconfigured() -> None:
    service = _service_module()

    runtime = service.runtime_from_env(
        {
            **_PRODUCTION_ENVIRONMENT,
            "MAGIC_VIDEO_EDITOR_WORKER_AUTH": "google_oidc",
        }
    )

    assert isinstance(runtime, service.UnconfiguredRuntime)


def test_environment_rejects_an_unknown_worker_auth_mode() -> None:
    service = _service_module()

    with pytest.raises(ValueError, match="worker auth"):
        service.runtime_from_env(
            {
                **_PRODUCTION_ENVIRONMENT,
                "MAGIC_VIDEO_EDITOR_WORKER_AUTH": "bogus",
            }
        )


def test_static_worker_auth_without_tokens_is_unconfigured() -> None:
    service = _service_module()

    runtime = service.runtime_from_env(
        {
            **_PRODUCTION_ENVIRONMENT,
            "MAGIC_VIDEO_EDITOR_WORKER_AUTH": "static",
        }
    )

    assert isinstance(runtime, service.UnconfiguredRuntime)


def test_the_default_environment_leaves_the_worker_pool_unconfigured() -> None:
    service = _service_module()

    runtime = service.runtime_from_env(dict(_PRODUCTION_ENVIRONMENT))

    assert runtime.worker_pool is None
    assert "/worker/v1/lease" not in {
        route.path for route in service.create_app(runtime).routes
    }


def test_environment_rejects_an_unknown_plugin_before_required_config() -> None:
    service = _service_module()

    with pytest.raises(ValueError, match="asset plugin"):
        service.runtime_from_env({"MAGIC_VIDEO_EDITOR_ASSET_PLUGIN": "filesystem"})


def test_versioned_output_preserves_a_qualified_gcs_uri() -> None:
    service = _service_module()

    key = service._versioned_output_key(
        "gs://magic-output/outputs/job-1.mp4",
        b"source-video",
    )

    assert key.startswith("gs://magic-output/outputs/job-1.")
    assert key.endswith(".mp4")


def test_versioned_output_forces_the_processor_mp4_contract() -> None:
    service = _service_module()

    key = service._versioned_output_key(
        "media/images/videos/video_lab_job.webm",
        b"source-video",
    )

    assert key.endswith(".mp4")
    assert ".webm" not in key


def test_studio_generation_jobs_cannot_be_submitted_through_the_generic_job_route() -> None:
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    runtime.dispatcher = _Dispatcher()
    client = TestClient(_service_module().create_app(runtime), headers=ACTOR)
    for operation in ("generate_subject", "generate_motion", "compose_frame"):
        response = client.post("/jobs", json={
            "idempotency_key": f"r-{operation}", "operation": operation,
            "source_key": "sources/input.png", "output_key": "outputs/out.png",
            "requested_outputs": {}, "payload": {"tenant_id": "someone-else"},
        })
        assert response.status_code == 422
        assert "Studio" in response.json()["detail"]
    assert runtime.dispatcher.job_ids == []


def test_generic_turnaround_submission_is_rejected_before_persistence() -> None:
    dispatcher = _Dispatcher()
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=_OwnedProjectService())
    runtime.dispatcher = dispatcher
    client = TestClient(_service_module().create_app(runtime))
    operation = "a" * 32
    rejected = client.post("/jobs", headers={"X-Studio-Actor-Id": "owner-1"}, json={
        "idempotency_key": f"project-1:turnaround:{operation}", "operation": "generate_turnaround",
        "operation_version": 1, "project_id": "project-1", "manifest_operation_id": operation,
        "source_key": "outputs/studio/project-1/opening-frames/view-frame.png",
        "output_key": f"outputs/studio/project-1/turnarounds/turnaround-{operation}/{operation}.mp4",
        "payload": {"turnaround_id": f"turnaround-{operation}",
                    "start_frame_key": "outputs/studio/project-1/opening-frames/view-frame.png"},
    })
    assert rejected.status_code == 422
    assert "turnaround endpoint" in rejected.json()["detail"]
    assert runtime.jobs.records == {} and dispatcher.job_ids == []


def test_legacy_job_and_upload_routes_require_a_signed_in_caller(monkeypatch) -> None:
    """On the API host every path reaches this service, so the legacy routes must
    authenticate like `/studio/*`: in session mode a forged actor header is ignored
    and, with no cookie or API key, every one of them answers 401 -- including the
    read and cancel of a job that belongs to no project."""
    monkeypatch.delenv("MAGIC_VIDEO_EDITOR_TRUSTED_ACTOR_SOURCE", raising=False)
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore())
    runtime.dispatcher = _Dispatcher()
    app = _service_module().create_app(runtime)
    # Mint one job the way the local gateway does, then switch to production's mode.
    job_id = TestClient(app, headers=ACTOR).post("/jobs", json={
        "idempotency_key": "request-1", "operation": "package",
        "source_key": "sources/input.mp4", "output_key": "outputs/final.mp4",
        "requested_outputs": {"alpha": True},
    }).json()["job_id"]
    monkeypatch.setenv("MAGIC_VIDEO_EDITOR_TRUSTED_ACTOR_SOURCE", "session")

    for headers in ({}, {"X-Studio-Actor-Id": "owner-1"}):
        client = TestClient(app, headers=headers)
        assert client.post("/jobs", json={
            "idempotency_key": "request-2", "operation": "package",
            "source_key": "sources/other.mp4", "output_key": "outputs/other.mp4",
            "requested_outputs": {"alpha": True},
        }).status_code == 401
        assert client.get(f"/jobs/{job_id}").status_code == 401
        assert client.post(f"/jobs/{job_id}/cancel").status_code == 401
        assert client.post(
            "/assets/uploads", json={"key": "sources/anyone.mp4", "content_type": "video/mp4"}
        ).status_code == 401
    assert runtime.dispatcher.job_ids == [job_id]
    assert asyncio.run(runtime.jobs.get(job_id)).status != "cancelled"

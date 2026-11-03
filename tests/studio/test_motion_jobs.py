from dataclasses import replace

from fastapi.testclient import TestClient

from magic_video_editor.app import create_app
from magic_video_editor.contracts import (
    GpuGenerationRequest,
    GpuGenerationResult,
    JobOperation,
    JobSubmission,
    MotionReferencePayload,
    VideoJobResult,
)
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.plugins.gpu_http import TerminalGpuError

JOB_ID = "00000000-0000-0000-0000-000000000001"


def _submission(*, operation_version: int = 1) -> JobSubmission:
    return JobSubmission(
        job_id=JOB_ID,
        idempotency_key="project-1:motion-1:attempt-1",
        operation=JobOperation.GENERATE_MOTION,
        source_key="sources/studio/project-1/subjects/canonical.png",
        output_key="outputs/studio/project-1/motions/motion-1/operation-1.mp4",
        requested_outputs={},
        operation_version=operation_version,
        project_id="project-1",
        manifest_operation_id="operation-1",
        payload={
            "subject_key": "sources/studio/project-1/subjects/canonical.png",
            "motion_id": "motion-1",
            "prompt": "subtle breathing idle",
            "loop": True,
            "width": 768,
            "height": 768,
            "duration_seconds": 6,
            "seed": 123,
            "preset": "high",
            "steps": 28,
            "provider_parameters": {"motion_strength": 0.75},
            "requested_revision": 2,
            "subject_revision_id": "subject-1",
            "subject_artifact_key": "sources/studio/project-1/subjects/canonical.png",
            "subject_sha256": "a" * 64,
            "motion_spec_digest": "b" * 64,
        },
    )


class _Gpu:
    def __init__(self) -> None:
        self.requests: list[GpuGenerationRequest] = []

    async def generate(self, request: GpuGenerationRequest) -> GpuGenerationResult:
        self.requests.append(request)
        return GpuGenerationResult(output_key=request.output_key)


class _RejectingGpu:
    async def generate(self, request: GpuGenerationRequest) -> GpuGenerationResult:
        raise TerminalGpuError("GPU rejected the motion input")


async def test_motion_job_records_manifest_operation_and_typed_payload() -> None:
    store = MemoryJobStore()
    submission = _submission()

    result = await store.submit(submission)
    claimed = await store.claim(JOB_ID)

    assert submission.operation is JobOperation.GENERATE_MOTION
    assert submission.operation_version == 1
    assert result.record.project_id == "project-1"
    assert result.record.manifest_operation_id == "operation-1"
    assert result.record.payload["motion_id"] == "motion-1"
    assert claimed is not None
    assert claimed.operation is JobOperation.GENERATE_MOTION
    assert claimed.operation_version == 1
    assert claimed.payload == submission.payload


async def test_memory_progress_update_is_fenced_and_preserves_bounds() -> None:
    store = MemoryJobStore()
    await store.submit(_submission())
    claimed = await store.claim(JOB_ID)
    assert claimed is not None

    for value in (None, 0, 100):
        assert await store.update_progress(JOB_ID, claimed.claim_token, value)
        assert (await store.get(JOB_ID)).progress_percent == value  # type: ignore[union-attr]
    assert not await store.update_progress(JOB_ID, "stale", 50)
    assert (await store.get(JOB_ID)).progress_percent == 100  # type: ignore[union-attr]


def test_generate_motion_v1_calls_gpu_and_settles_once() -> None:
    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    gpu = _Gpu()
    runtime.gpu = gpu
    runtime.motion_operations_enabled = True
    client = TestClient(create_app(runtime))

    import asyncio

    asyncio.run(jobs.submit(_submission()))
    first = client.post(f"/tasks/jobs/{JOB_ID}")
    duplicate = client.post(f"/tasks/jobs/{JOB_ID}")

    assert first.json() == {"job_id": JOB_ID, "status": "completed"}
    assert duplicate.json() == {"job_id": JOB_ID, "status": "already_settled"}
    assert len(gpu.requests) == 1
    assert gpu.requests[0] == GpuGenerationRequest(
        source_key="sources/studio/project-1/subjects/canonical.png",
        output_key="outputs/studio/project-1/motions/motion-1/operation-1.mp4",
        prompt="subtle breathing idle",
        width=768,
        height=768,
        duration_seconds=6,
        seed=123,
        idempotency_key="project-1:motion-1:attempt-1",
        motion_id="motion-1",
        loop=True,
        preset="high",
        steps=28,
        provider_parameters={"motion_strength": 0.75},
    )
    assert (
        runtime.completed[JOB_ID]
        == "outputs/studio/project-1/motions/motion-1/operation-1.mp4"
    )


def test_generate_motion_forwards_conditioning_assets_to_gpu_request() -> None:
    """T2: a shared-pose / reference-conditioned motion must forward the
    approved start frame and references to the GPU provider, not just record
    them in the job payload."""
    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    gpu = _Gpu()
    runtime.gpu = gpu
    runtime.motion_operations_enabled = True
    client = TestClient(create_app(runtime))

    import asyncio

    submission = replace(
        _submission(),
        payload={
            **_submission().payload,
            "start_frame_key": "outputs/studio/project-1/compositions/comp-1.png",
            "start_frame_sha256": "c" * 64,
            "composition_revision_id": "comp-1",
            "references": [
                {"key": "sources/studio/project-1/references/ref-1.png", "sha256": "d" * 64, "role": "start"}
            ],
        },
    )
    asyncio.run(jobs.submit(submission))
    response = client.post(f"/tasks/jobs/{JOB_ID}")

    assert response.json() == {"job_id": JOB_ID, "status": "completed"}
    assert len(gpu.requests) == 1
    request = gpu.requests[0]
    assert request.start_frame_key == "outputs/studio/project-1/compositions/comp-1.png"
    assert request.start_frame_sha256 == "c" * 64
    assert request.composition_revision_id == "comp-1"
    assert request.references == (
        MotionReferencePayload(
            key="sources/studio/project-1/references/ref-1.png", sha256="d" * 64, role="start"
        ),
    )


def test_unsupported_motion_version_is_a_terminal_input_failure() -> None:
    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    runtime.gpu = _Gpu()
    runtime.motion_operations_enabled = True
    client = TestClient(create_app(runtime))

    import asyncio

    asyncio.run(jobs.submit(_submission(operation_version=2)))
    response = client.post(f"/tasks/jobs/{JOB_ID}")

    assert response.json() == {"job_id": JOB_ID, "status": "failed"}
    assert (
        "unsupported generate_motion operation version: 2" in runtime.failures[JOB_ID]
    )
    assert runtime.jobs.pending == {}


def test_terminal_gpu_rejection_is_not_retried() -> None:
    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    runtime.gpu = _RejectingGpu()
    runtime.motion_operations_enabled = True
    client = TestClient(create_app(runtime))

    import asyncio

    asyncio.run(jobs.submit(_submission()))
    response = client.post(f"/tasks/jobs/{JOB_ID}")

    assert response.json() == {"job_id": JOB_ID, "status": "failed"}
    assert runtime.failures[JOB_ID] == "GPU rejected the motion input"
    assert runtime.jobs.pending == {}


async def test_stale_motion_claim_cannot_complete_a_newer_claim() -> None:
    jobs = MemoryJobStore()
    await jobs.submit(_submission())
    first = await jobs.claim(JOB_ID)
    assert first is not None
    await jobs.release(JOB_ID, first.claim_token, "retry")
    second = await jobs.claim(JOB_ID)
    assert second is not None

    assert (
        await jobs.complete(
            JOB_ID,
            first.claim_token,
            VideoJobResult(output_key="outputs/stale.mp4"),
        )
        is False
    )
    assert (
        await jobs.complete(
            JOB_ID,
            second.claim_token,
            VideoJobResult(output_key=second.output_key),
        )
        is True
    )


async def test_duplicate_motion_submission_reuses_one_candidate_job() -> None:
    jobs = MemoryJobStore()
    first = await jobs.submit(_submission())
    duplicate = await jobs.submit(
        replace(_submission(), job_id="00000000-0000-0000-0000-000000000002")
    )

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.record.job_id == JOB_ID
    assert len(jobs.pending) == 1


async def test_job_store_bounds_envelope_size_depth_and_copies_payload() -> None:
    import pytest

    jobs = MemoryJobStore()
    with pytest.raises(ValueError, match="idempotency_key"):
        await jobs.submit(replace(_submission(), idempotency_key="x" * 256))
    with pytest.raises(ValueError, match="payload exceeds"):
        await jobs.submit(
            replace(
                _submission(),
                payload={**_submission().payload, "prompt": "x" * 17_000},
            )
        )
    nested: object = "leaf"
    for _ in range(9):
        nested = {"nested": nested}
    with pytest.raises(ValueError, match="depth"):
        await jobs.submit(
            replace(
                _submission(),
                payload={**_submission().payload, "options": nested},
            )
        )
    with pytest.raises(ValueError, match="finite"):
        await jobs.submit(
            replace(
                _submission(),
                payload={**_submission().payload, "guidance": float("nan")},
            )
        )
    with pytest.raises(ValueError, match="requested_outputs"):
        await jobs.submit(
            replace(
                _submission(),
                requested_outputs={f"output-{index}": True for index in range(17)},
            )
        )
    with pytest.raises(ValueError, match="too long"):
        await jobs.submit(
            replace(
                _submission(),
                requested_outputs={"x" * 65: True},
            )
        )

    payload = dict(_submission().payload)
    await jobs.submit(replace(_submission(), payload=payload))
    payload["prompt"] = "mutated after persistence"

    assert jobs.records[JOB_ID].payload["prompt"] == "subtle breathing idle"
    assert jobs.pending[JOB_ID].payload["prompt"] == "subtle breathing idle"


def test_motion_worker_rejects_unknown_or_mismatched_payload_fields() -> None:
    import asyncio

    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    runtime.gpu = _Gpu()
    runtime.motion_operations_enabled = True
    submission = _submission()
    asyncio.run(
        jobs.submit(
            replace(
                submission,
                payload={**submission.payload, "promtp": "typo"},
            )
        )
    )

    response = TestClient(create_app(runtime)).post(f"/tasks/jobs/{JOB_ID}")

    assert response.json()["status"] == "failed"


async def test_motion_submission_rejects_cross_project_subject() -> None:
    jobs = MemoryJobStore()
    submission = _submission()

    import pytest

    with pytest.raises(ValueError, match="ledger source key"):
        await jobs.submit(
            replace(
                submission,
                payload={
                    **submission.payload,
                    "subject_key": "sources/studio/project-2/subjects/canonical.png",
                },
            )
        )


def test_motion_execution_is_disabled_until_workers_are_declared_ready() -> None:
    import asyncio

    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    runtime.gpu = _Gpu()
    asyncio.run(jobs.submit(_submission()))

    response = TestClient(create_app(runtime)).post(f"/tasks/jobs/{JOB_ID}")

    assert response.status_code == 503
    assert JOB_ID in jobs.pending


def test_motion_execution_is_disabled_without_a_gpu_even_if_flag_is_on() -> None:
    import asyncio

    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    runtime.motion_operations_enabled = True
    asyncio.run(jobs.submit(_submission()))

    response = TestClient(create_app(runtime)).post(f"/tasks/jobs/{JOB_ID}")

    assert response.status_code == 503
    assert JOB_ID in jobs.pending
    assert runtime.failures == {}


class _IdempotentGpu(_Gpu):
    def __init__(self) -> None:
        super().__init__()
        self.generated: dict[str, GpuGenerationResult] = {}
        self.effective_generations = 0

    async def generate(self, request: GpuGenerationRequest) -> GpuGenerationResult:
        self.requests.append(request)
        if request.idempotency_key not in self.generated:
            self.effective_generations += 1
            self.generated[request.idempotency_key] = GpuGenerationResult(
                request.output_key
            )
        return self.generated[request.idempotency_key]


def test_completion_retry_replays_one_idempotent_gpu_generation() -> None:
    import asyncio

    jobs = MemoryJobStore(fail_next_complete=True)
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    gpu = _IdempotentGpu()
    runtime.gpu = gpu
    runtime.motion_operations_enabled = True
    asyncio.run(jobs.submit(_submission()))
    client = TestClient(create_app(runtime), raise_server_exceptions=False)

    first = client.post(f"/tasks/jobs/{JOB_ID}")
    second = client.post(f"/tasks/jobs/{JOB_ID}")

    assert first.status_code == 500
    assert second.json()["status"] == "completed"
    assert len(gpu.requests) == 2
    assert gpu.effective_generations == 1


class _ClaimChangingGpu(_IdempotentGpu):
    def __init__(self, jobs: MemoryJobStore, *, cancel: bool) -> None:
        super().__init__()
        self.jobs = jobs
        self.cancel = cancel
        self.changed = False

    async def generate(self, request: GpuGenerationRequest) -> GpuGenerationResult:
        result = await super().generate(request)
        if not self.changed:
            self.changed = True
            claimed = self.jobs.claimed[JOB_ID]
            if self.cancel:
                await self.jobs.cancel(JOB_ID)
            else:
                await self.jobs.release(JOB_ID, claimed.claim_token, "lease expired")
                newer = await self.jobs.claim(JOB_ID)
                assert newer is not None
                await self.jobs.release(JOB_ID, newer.claim_token, "redeliver")
        return result


def test_cancel_during_gpu_generation_cannot_publish_job_completion() -> None:
    import asyncio

    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    gpu = _ClaimChangingGpu(jobs, cancel=True)
    runtime.gpu = gpu
    runtime.motion_operations_enabled = True
    asyncio.run(jobs.submit(_submission()))

    response = TestClient(create_app(runtime)).post(f"/tasks/jobs/{JOB_ID}")

    assert response.json()["status"] == "claim_lost"
    assert gpu.effective_generations == 1
    assert runtime.completed == {}


def test_stale_gpu_claim_replays_same_immutable_generation_for_new_claim() -> None:
    import asyncio

    jobs = MemoryJobStore()
    runtime = InMemoryRuntime(jobs, MemoryAssetStore())
    gpu = _ClaimChangingGpu(jobs, cancel=False)
    runtime.gpu = gpu
    runtime.motion_operations_enabled = True
    asyncio.run(jobs.submit(_submission()))
    client = TestClient(create_app(runtime))

    first = client.post(f"/tasks/jobs/{JOB_ID}")
    second = client.post(f"/tasks/jobs/{JOB_ID}")

    assert first.json()["status"] == "claim_lost"
    assert second.json()["status"] == "completed"
    assert gpu.effective_generations == 1
    assert len(gpu.requests) == 2

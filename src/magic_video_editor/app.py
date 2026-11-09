import asyncio
import hashlib
import os

from magic_video_editor.cpu_worker import MovieCpuPlugin
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from enum import StrEnum
from functools import partial
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from outbound_gpu_worker_pool import (
    CapabilitySchemas,
)
from outbound_gpu_worker_pool import (
    IdempotencyConflict as PoolIdempotencyConflict,
)
from outbound_gpu_worker_pool import (
    JobSubmission as PoolJobSubmission,
)
from outbound_gpu_worker_pool.auth import (
    GOOGLE_OIDC_AUTH_METHOD,
    STATIC_AUTH_METHOD,
    GoogleIdTokenWorkerAuthenticator,
    StaticTokenWorkerAuthenticator,
)
from outbound_gpu_worker_pool.contracts import PoolWorkerView
from outbound_gpu_worker_pool.enrollment import WorkerEnrollment, enrollments_from_json
from outbound_gpu_worker_pool.plugins import (
    DeterministicEchoPlugin,
    capability_schemas_from_plugins,
)
from outbound_gpu_worker_pool.postgres import (
    PostgresAuditLog,
    PostgresWorkerRegistry,
)
from outbound_gpu_worker_pool.postgres import (
    PostgresJobStore as PostgresPoolJobStore,
)
from outbound_gpu_worker_pool.routes import (
    PoolWorkerResponse,
    PoolWorkersResponse,
    create_pool_status_router,
    create_worker_router,
)
from outbound_gpu_worker_pool.service import WorkerPoolService
from pydantic import BaseModel, ValidationError

from magic_video_editor.api_models import (
    ComposeFramePayloadV1,
    CreateJobRequest,
    GenerateMotionPayloadV1,
    GenerateSubjectPayloadV1,
    JobResponse,
    PoolJobRequest,
    PoolJobResponse,
    UploadRequest,
    UploadResponse,
)
from magic_video_editor.api_reference import install_api_reference
from magic_video_editor.extensions import (
    ExtensionContext,
    RouterContext,
    extension_operators,
    load_extensions,
)
from magic_video_editor.contracts import (
    AssetDescriptor,
    CompositionGenerationRequest,
    GpuGenerationRequest,
    IdempotencyConflict,
    JobFailureCode,
    JobLeaseActive,
    JobOperation,
    JobRecord,
    JobSubmission,
    MotionReferencePayload,
    Runtime,
    RuntimeLifecycle,
    SubjectGenerationRequest,
    TerminalVideoError,
    VideoDerivatives,
    VideoJob,
    VideoJobResult,
    validate_job_submission,
)
from magic_video_editor.memory import InMemoryRuntime
from magic_video_editor.monitoring import (
    SentryConfig,
    capture_job_failure,
    configure_sentry,
    is_sentry_enabled,
)
from magic_video_editor.plugins.composition_http import (
    HttpCompositionGenerator,
    TerminalCompositionProviderError,
)
from magic_video_editor.plugins.ffmpeg import FfmpegVideoProcessor
from magic_video_editor.plugins.gcs import GcsAssetStore
from magic_video_editor.plugins.gpu_http import HttpGpuProcessor, TerminalGpuError
from magic_video_editor.plugins.postgres import PostgresJobStore
from magic_video_editor.plugins.subject_http import (
    HttpSubjectGenerator,
    TerminalSubjectProviderError,
)
from magic_video_editor.plugins.subject_pool import PoolSubjectGenerator, SubjectMachineUnavailable
from magic_video_editor.public_downloads import create_download_router
from magic_video_editor.retention import (
    MOVIE,
    PROJECT,
    ItemExpiring,
    PostgresActivityStore,
    Retention,
    RetentionPolicy,
    RetentionSweeper,
)
from magic_video_editor.safety import (
    PostgresPromptRefusalStore,
    PromptRefused,
    PromptScreenUnavailable,
    configured_prompt_screening,
)
from magic_video_editor.studio.api_keys import (
    MemoryStudioApiKeyStore,
    PostgresStudioApiKeyStore,
    StudioApiKeyStore,
)
from magic_video_editor.studio.provider_accounts import (
    CredentialCipher,
    PostgresProviderAccountStore,
    ProviderAccounts,
)
from magic_video_editor.studio.auth import (
    MemoryStudioAuthStore,
    PostgresStudioAuthStore,
)
from magic_video_editor.studio.accounts import account_policy_from_env
from magic_video_editor.studio.auth_service import build_studio_auth_service
from magic_video_editor.studio.enrolment import (
    EnrolledWorkerAuthenticator,
    PostgresStudioEnrolmentStore,
    enroll_in_pool,
)
from magic_video_editor.studio.limits import (
    OwnerLimits,
    PostgresStudioOwnerUsageStore,
)
from magic_video_editor.studio.operators import (
    ADMIN_EMAILS_ENV,
    OperatorAccess,
    admin_emails_from_env,
)
from magic_video_editor.studio.postgres import ManifestUnreadable, PostgresStudioStore
from magic_video_editor.studio.routes import (
    create_studio_router,
    studio_actor_id,
)
from magic_video_editor.studio.service import StudioService
from magic_video_editor.studio.telemetry import PostgresGpuTelemetryStore
from magic_video_editor.studio.write_guard import ManifestUnwritable
from magic_video_editor.task_dispatch import CloudTasksDispatcher

WORKER_AUTH_NONE = "none"
WORKER_ENROLLMENTS_VARIABLE = "MAGIC_VIDEO_EDITOR_WORKER_ENROLLMENTS"
# Per-process pre-authentication admission for the worker surface; unset keeps
# the pool library's defaults. Replicas do not share these buckets.
WORKER_ADMISSION_LIMIT_VARIABLES = {
    "pre_auth_limit_per_minute": "MAGIC_VIDEO_EDITOR_WORKER_PRE_AUTH_LIMIT_PER_MINUTE",
    "per_source_limit_per_minute": "MAGIC_VIDEO_EDITOR_WORKER_PER_SOURCE_LIMIT_PER_MINUTE",
    "auth_audit_limit_per_minute": "MAGIC_VIDEO_EDITOR_WORKER_AUTH_AUDIT_LIMIT_PER_MINUTE",
    "max_auth_concurrency": "MAGIC_VIDEO_EDITOR_WORKER_MAX_AUTH_CONCURRENCY",
}


def _worker_enrollments(values: Mapping[str, str]) -> dict[str, WorkerEnrollment]:
    """Operator-approved house/static workers that have no registry row yet.

    Same JSON shape as the pool's `OGWP_WORKER_ENROLLMENTS`: an object from
    worker id to `{"identity_subject": ..., "tenant_id": ...}`, where an
    explicit `null` tenant approves house membership. Studio-enrolled machines
    never appear here; redeeming a pairing code enrolls them directly.
    """
    try:
        return enrollments_from_json(values.get(WORKER_ENROLLMENTS_VARIABLE))
    except ValueError as exc:
        raise ValueError(f"invalid {WORKER_ENROLLMENTS_VARIABLE}") from exc


def _retention_sweep_caller(values: Mapping[str, str]) -> tuple[str, str] | None:
    """Who may run the inactivity sweep: the OIDC audience the scheduler signs
    for, and its service account. Both are required or the route stays off."""
    audience = values.get("MAGIC_VIDEO_EDITOR_RETENTION_SWEEP_AUDIENCE")
    invoker = values.get("MAGIC_VIDEO_EDITOR_RETENTION_SWEEP_INVOKER")
    return (audience, invoker) if audience and invoker else None


async def _verified_google_caller(authorization: str | None, audience: str) -> str | None:
    """The verified email of a Google-signed OIDC bearer token for `audience`."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    from google.auth.transport.requests import Request as GoogleRequest
    from google.oauth2 import id_token

    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token, authorization[7:].strip(), GoogleRequest(), audience
        )
    except ValueError:
        return None
    if not claims.get("email_verified"):
        return None
    return claims.get("email")


def _worker_admission_limits(values: Mapping[str, str]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for parameter, variable in WORKER_ADMISSION_LIMIT_VARIABLES.items():
        raw = values.get(variable)
        if raw is None or not raw.strip():
            continue
        try:
            limit = int(raw)
        except ValueError as exc:
            raise ValueError(f"{variable} must be a positive integer") from exc
        if limit < 1:
            raise ValueError(f"{variable} must be a positive integer")
        limits[parameter] = limit
    return limits


class ProductionLifecycle:
    def __init__(
        self,
        jobs: RuntimeLifecycle,
        assets: RuntimeLifecycle,
        processor: RuntimeLifecycle,
        studio_store: RuntimeLifecycle | None = None,
        telemetry_store: RuntimeLifecycle | None = None,
        pool_jobs: RuntimeLifecycle | None = None,
        worker_registry: RuntimeLifecycle | None = None,
        worker_audit: RuntimeLifecycle | None = None,
        studio_api_keys: RuntimeLifecycle | None = None,
        studio_auth: RuntimeLifecycle | None = None,
        studio_owner_usage: RuntimeLifecycle | None = None,
        studio_enrolment: RuntimeLifecycle | None = None,
        movie_store: RuntimeLifecycle | None = None,
        provider_accounts: RuntimeLifecycle | None = None,
        prompt_refusals: RuntimeLifecycle | None = None,
        item_activity: RuntimeLifecycle | None = None,
        audio_store: RuntimeLifecycle | None = None,
        assets3d_store: RuntimeLifecycle | None = None,
        extension_components: Iterable[RuntimeLifecycle] = (),
    ) -> None:
        self._components = tuple(
            component
            for component in (
                jobs,
                studio_store,
                studio_api_keys,
                studio_auth,
                studio_owner_usage,
                studio_enrolment,
                movie_store,
                audio_store,
                assets3d_store,
                provider_accounts,
                prompt_refusals,
                item_activity,
                *extension_components,
                telemetry_store,
                pool_jobs,
                worker_registry,
                worker_audit,
                assets,
                processor,
            )
            if component is not None
        )

    async def start(self) -> None:
        started: list[RuntimeLifecycle] = []
        try:
            for component in self._components:
                await component.start()
                started.append(component)
        except Exception as exc:
            cleanup_errors = await _stop_components(reversed(started))
            for cleanup_error in cleanup_errors:
                exc.add_note(f"cleanup error during startup rollback: {cleanup_error}")
            raise

    async def stop(self) -> None:
        cleanup_errors = await _stop_components(reversed(self._components))
        if cleanup_errors:
            raise cleanup_errors[0]


class TaskStatus(StrEnum):
    COMPLETED = "completed"
    ALREADY_SETTLED = "already_settled"
    FAILED = "failed"
    CLAIM_LOST = "claim_lost"


class ServiceRole(StrEnum):
    ALL = "all"
    API = "api"
    WORKER = "worker"
    COORDINATOR = "coordinator"


class TaskResponse(BaseModel):
    job_id: str
    status: TaskStatus


class MachineFeatureResponse(BaseModel):
    """One kind of work and whether this machine can run it; `reason` says why not."""

    key: str
    label: str
    available: bool
    capability_id: str | None = None
    reason: str | None = None


class StudioPoolWorkerResponse(PoolWorkerResponse):
    label: str | None = None
    features: list[MachineFeatureResponse] = []

    @classmethod
    def for_view(cls, view: PoolWorkerView, label: str | None = None) -> "StudioPoolWorkerResponse":
        from magic_video_editor.machine_features import features_for

        return cls(**PoolWorkerResponse.from_view(view).model_dump(), label=label,
                   features=features_for(view))


class StudioPoolWorkersResponse(PoolWorkersResponse):
    workers: list[StudioPoolWorkerResponse]
    cluster_admin: bool = False


class UnconfiguredRuntime(Runtime):
    def __init__(self) -> None:
        unavailable = _UnavailablePlugin()
        super().__init__(
            jobs=unavailable,
            assets=unavailable,
            processor=unavailable,
            ready=False,
        )


class _UnavailablePlugin:
    async def create_upload_url(
        self, key: str, content_type: str, *, max_bytes: int | None = None
    ) -> str:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def claim(self, job_id: str) -> VideoJob | None:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def get(self, job_id: str) -> JobRecord | None:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def create_output_upload_url(self, key: str, content_type: str) -> str:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def describe(self, key: str) -> AssetDescriptor | None:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def complete(
        self, job_id: str, claim_token: str, result: VideoJobResult
    ) -> bool:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def release(self, job_id: str, claim_token: str, reason: str) -> bool:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def fail(
        self, job_id: str, claim_token: str, reason: str, **_kwargs: object
    ) -> bool:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def read(self, key: str) -> bytes:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def write_once(self, key: str, content: bytes, content_type: str) -> bool:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def prepare_source(self, source: bytes) -> bytes:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")

    async def build_derivatives(
        self, source: bytes, requested_outputs: dict[str, bool]
    ) -> VideoDerivatives:
        raise RuntimeError("Magic Video Editor runtime plugin is not configured")


async def _execute(runtime: Runtime, job_id: str) -> TaskResponse:
    try:
        job = await runtime.jobs.claim(job_id)
    except JobLeaseActive as exc:
        raise HTTPException(
            status_code=503,
            detail="video job is currently leased",
        ) from exc
    if job is None:
        return TaskResponse(job_id=job_id, status=TaskStatus.ALREADY_SETTLED)
    if (
        job.operation is JobOperation.GENERATE_MOTION
        and not runtime.motion_generation_available
    ):
        await runtime.jobs.release(
            job.job_id,
            job.claim_token,
            "motion operations are disabled until compatible workers are ready",
        )
        raise HTTPException(
            status_code=503,
            detail="motion operations are disabled until compatible workers are ready",
        )
    if job.operation is JobOperation.PACKAGE:
        return await _execute_package(runtime, job)
    if job.operation is JobOperation.GENERATE_MOTION:
        return await _execute_generate_motion(runtime, job)
    if job.operation is JobOperation.GENERATE_SUBJECT:
        return await _execute_generate_subject(runtime, job)
    if job.operation is JobOperation.COMPOSE_FRAME:
        return await _execute_compose_frame(runtime, job)
    return await _fail_terminal_operation(
        runtime,
        job,
        f"unsupported execution operation: {job.operation}",
        JobFailureCode.UNSUPPORTED_OPERATION,
        "This operation is not supported by the current worker.",
    )


async def _execute_package(runtime: Runtime, job: VideoJob) -> TaskResponse:
    published_output_key: str | None = None
    generated_result: VideoJobResult | None = None
    try:
        source = await runtime.assets.read(job.source_key)
        output_key = _versioned_output_key(job.output_key, source)
        prepared_source = await runtime.processor.prepare_source(source)
        created = await runtime.assets.write_once(
            output_key, prepared_source, "video/mp4"
        )
        published_output_key = output_key
        if not created:
            prepared_source = await runtime.assets.read(output_key)
        derivatives = await runtime.processor.build_derivatives(
            prepared_source, job.requested_outputs
        )
        generated_result = await _publish_derivatives(
            runtime, output_key, derivatives, job.requested_outputs
        )
        accepted = await runtime.jobs.complete(
            job.job_id, job.claim_token, generated_result
        )
        if not accepted:
            return TaskResponse(job_id=job.job_id, status=TaskStatus.CLAIM_LOST)
    except TerminalVideoError as exc:
        accepted = await runtime.jobs.fail(
            job.job_id,
            job.claim_token,
            str(exc),
            failure_code=JobFailureCode.INVALID_INPUT,
            failure_message="The source asset could not be processed. Inspect or replace it before retrying.",
            retryable=False,
        )
        status = TaskStatus.FAILED if accepted else TaskStatus.CLAIM_LOST
        return TaskResponse(job_id=job.job_id, status=status)
    except Exception as exc:
        return await _handle_processing_failure(
            runtime,
            job,
            published_output_key,
            generated_result,
            exc,
        )
    return TaskResponse(job_id=job.job_id, status=TaskStatus.COMPLETED)


async def _execute_generate_motion(runtime: Runtime, job: VideoJob) -> TaskResponse:
    if job.operation_version != 1:
        return await _fail_terminal_operation(
            runtime,
            job,
            f"unsupported generate_motion operation version: {job.operation_version}",
            JobFailureCode.UNSUPPORTED_OPERATION,
            "This motion operation version is not supported. Edit the motion and try again.",
        )
    if runtime.gpu is None:
        return await _fail_terminal_operation(
            runtime,
            job,
            "GPU processor is not configured",
            JobFailureCode.PROVIDER_UNAVAILABLE,
            "Motion generation is not available for this project.",
        )
    try:
        payload = GenerateMotionPayloadV1.model_validate(job.payload)
    except ValidationError as exc:
        return await _fail_terminal_operation(
            runtime,
            job,
            f"invalid generate_motion v1 payload: {exc}",
            JobFailureCode.INVALID_INPUT,
            "The motion settings are invalid. Inspect and edit the motion before retrying.",
        )
    provenance_error = _motion_provenance_error(job, payload)
    if provenance_error is not None:
        return await _fail_terminal_operation(
            runtime,
            job,
            provenance_error,
            JobFailureCode.INVALID_INPUT,
            "The motion input no longer matches this project. Inspect and edit the motion.",
        )
    request = GpuGenerationRequest(
        source_key=payload.subject_key,
        output_key=job.output_key,
        prompt=payload.prompt,
        width=payload.width,
        height=payload.height,
        duration_seconds=payload.duration_seconds,
        seed=payload.seed,
        idempotency_key=job.idempotency_key,
        motion_id=payload.motion_id,
        loop=payload.loop,
        preset=payload.preset,
        steps=payload.steps,
        provider_parameters=payload.provider_parameters,
        start_frame_key=payload.start_frame_key,
        start_frame_sha256=payload.start_frame_sha256,
        composition_revision_id=payload.composition_revision_id,
        references=tuple(
            MotionReferencePayload(
                key=reference.key, sha256=reference.sha256, role=reference.role.value
            )
            for reference in payload.references
        ),
    )
    result: VideoJobResult | None = None
    telemetry_task = asyncio.create_task(_poll_gpu_telemetry(runtime, job))
    try:
        generated = await runtime.gpu.generate(request)
        if generated.output_key != job.output_key:
            raise TerminalVideoError("GPU result did not preserve immutable output key")
        result = VideoJobResult(output_key=generated.output_key)
        accepted = await runtime.jobs.complete(job.job_id, job.claim_token, result)
        status = TaskStatus.COMPLETED if accepted else TaskStatus.CLAIM_LOST
        return TaskResponse(job_id=job.job_id, status=status)
    except (TerminalVideoError, TerminalGpuError) as exc:
        return await _fail_terminal_operation(
            runtime,
            job,
            str(exc),
            JobFailureCode.INVALID_INPUT,
            "The provider could not process this motion. Inspect the input and settings.",
        )
    except Exception as exc:
        return await _handle_processing_failure(
            runtime,
            job,
            None,
            result,
            exc,
        )
    finally:
        telemetry_task.cancel()
        with suppress(asyncio.CancelledError):
            await telemetry_task
        await _capture_gpu_telemetry(runtime, job.project_id)


async def _capture_gpu_telemetry(runtime: Runtime, project_id: str | None) -> None:
    if (
        project_id is None
        or runtime.gpu_telemetry_provider is None
        or runtime.gpu_telemetry_store is None
    ):
        return
    try:
        snapshot = await runtime.gpu_telemetry_provider.status()
        if snapshot is not None:
            await runtime.gpu_telemetry_store.save(project_id, snapshot)
    except Exception:
        return


async def _poll_gpu_telemetry(runtime: Runtime, job: VideoJob) -> None:
    while True:
        try:
            if runtime.gpu_telemetry_provider is not None:
                snapshot = await runtime.gpu_telemetry_provider.status()
                if snapshot is not None:
                    if runtime.gpu_telemetry_store is not None and job.project_id:
                        await runtime.gpu_telemetry_store.save(job.project_id, snapshot)
                    progress = snapshot.operation_progress.get(job.idempotency_key)
                    if progress is not None:
                        await runtime.jobs.update_progress(
                            job.job_id, job.claim_token, progress.percent
                        )
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def _execute_generate_subject(runtime: Runtime, job: VideoJob) -> TaskResponse:
    if job.operation_version != 1:
        return await _fail_terminal_operation(
            runtime,
            job,
            f"unsupported generate_subject operation version: {job.operation_version}",
            JobFailureCode.UNSUPPORTED_OPERATION,
            "This subject operation version is not supported. Generate the subject again.",
        )
    if not runtime.subject_generation_available:
        return await _fail_terminal_operation(
            runtime,
            job,
            "subject generator is not capability-attested",
            JobFailureCode.PROVIDER_UNAVAILABLE,
            "Subject generation is not available in this environment.",
        )
    try:
        payload = GenerateSubjectPayloadV1.model_validate(job.payload)
        assert runtime.subject_generator is not None
        output_key = await runtime.subject_generator.generate_subject(
            SubjectGenerationRequest(
                reference_keys=payload.reference_keys,
                output_key=job.output_key,
                direction=payload.direction,
                idempotency_key=job.idempotency_key,
                owner_id=payload.tenant_id or await _studio_job_owner(runtime, job),
            )
        )
        if output_key != job.output_key:
            raise TerminalVideoError(
                "subject generator changed the immutable output key"
            )
        accepted = await runtime.jobs.complete(
            job.job_id, job.claim_token, VideoJobResult(output_key=output_key)
        )
        return TaskResponse(
            job_id=job.job_id,
            status=TaskStatus.COMPLETED if accepted else TaskStatus.CLAIM_LOST,
        )
    except ValidationError as exc:
        return await _fail_terminal_operation(
            runtime,
            job,
            f"invalid generate_subject v1 payload: {exc}",
            JobFailureCode.INVALID_INPUT,
            "The subject references are invalid. Review them and generate again.",
        )
    except SubjectMachineUnavailable as exc:
        return await _fail_terminal_operation(
            runtime, job, str(exc), JobFailureCode.PROVIDER_UNAVAILABLE, str(exc)
        )
    except (TerminalVideoError, TerminalSubjectProviderError) as exc:
        return await _fail_terminal_operation(
            runtime,
            job,
            str(exc),
            JobFailureCode.INVALID_INPUT,
            "The subject output was invalid. Review the references and generate again.",
        )
    except Exception as exc:
        return await _handle_processing_failure(runtime, job, None, None, exc)


async def _execute_compose_frame(runtime: Runtime, job: VideoJob) -> TaskResponse:
    if job.operation_version != 1:
        return await _fail_terminal_operation(
            runtime,
            job,
            f"unsupported compose_frame operation version: {job.operation_version}",
            JobFailureCode.UNSUPPORTED_OPERATION,
            "This composition operation version is not supported. Compose again.",
        )
    if not runtime.composition_generation_available:
        return await _fail_terminal_operation(
            runtime,
            job,
            "composition generator is not capability-attested",
            JobFailureCode.PROVIDER_UNAVAILABLE,
            "Frame composition is not available in this environment.",
        )
    try:
        payload = ComposeFramePayloadV1.model_validate(job.payload)
        assert runtime.composition_generator is not None
        output_key = await runtime.composition_generator.compose_frame(
            CompositionGenerationRequest(
                subject_key=payload.subject_key,
                reference_keys=payload.reference_keys,
                output_key=job.output_key,
                prompt=payload.prompt,
                model=payload.composition_model.value,
                idempotency_key=job.idempotency_key,
            )
        )
        if output_key != job.output_key:
            raise TerminalVideoError(
                "composition generator changed the immutable output key"
            )
        accepted = await runtime.jobs.complete(
            job.job_id, job.claim_token, VideoJobResult(output_key=output_key)
        )
        return TaskResponse(
            job_id=job.job_id,
            status=TaskStatus.COMPLETED if accepted else TaskStatus.CLAIM_LOST,
        )
    except ValidationError as exc:
        return await _fail_terminal_operation(
            runtime,
            job,
            f"invalid compose_frame v1 payload: {exc}",
            JobFailureCode.INVALID_INPUT,
            "The composition request is invalid. Review it and compose again.",
        )
    except (TerminalVideoError, TerminalCompositionProviderError) as exc:
        return await _fail_terminal_operation(
            runtime,
            job,
            str(exc),
            JobFailureCode.INVALID_INPUT,
            "The composed still was invalid. Review the prompt and compose again.",
        )
    except Exception as exc:
        return await _handle_processing_failure(runtime, job, None, None, exc)


def _motion_provenance_error(
    job: VideoJob,
    payload: GenerateMotionPayloadV1,
) -> str | None:
    if job.project_id is None or job.manifest_operation_id is None:
        return "generate_motion requires project and manifest operation identifiers"
    source_prefixes = (
        f"sources/studio/{job.project_id}/subjects/",
        f"outputs/studio/{job.project_id}/subjects/",
    )
    expected_output = (
        f"outputs/studio/{job.project_id}/motions/{payload.motion_id}/"
        f"{job.manifest_operation_id}.mp4"
    )
    if payload.subject_key != job.source_key:
        return "generate_motion subject key must match the ledger source key"
    if not job.source_key.startswith(source_prefixes):
        return "generate_motion source key is outside the project namespace"
    if job.output_key != expected_output:
        return "generate_motion output key is not the immutable operation key"
    return None


async def _fail_terminal_operation(
    runtime: Runtime,
    job: VideoJob,
    reason: str,
    failure_code: JobFailureCode,
    failure_message: str,
) -> TaskResponse:
    capture_job_failure(
        job_id=job.job_id,
        operation=job.operation.value,
        project_id=job.project_id,
        retryable=False,
        message=reason,
    )
    accepted = await runtime.jobs.fail(
        job.job_id,
        job.claim_token,
        reason,
        failure_code=failure_code,
        failure_message=failure_message,
        retryable=False,
    )
    status = TaskStatus.FAILED if accepted else TaskStatus.CLAIM_LOST
    return TaskResponse(job_id=job.job_id, status=status)


async def _handle_processing_failure(
    runtime: Runtime,
    job: VideoJob,
    published_output_key: str | None,
    generated_result: VideoJobResult | None,
    error: Exception,
) -> TaskResponse:
    capture_job_failure(
        job_id=job.job_id,
        operation=job.operation.value,
        project_id=job.project_id,
        retryable=job.attempts < job.attempt_budget,
        error=error,
    )
    if job.attempts < job.attempt_budget:
        accepted = await runtime.jobs.release(
            job.job_id, job.claim_token, str(error)
        )
        if not accepted:
            return TaskResponse(job_id=job.job_id, status=TaskStatus.CLAIM_LOST)
        raise HTTPException(
            status_code=500, detail="video processing retryable"
        ) from error
    if generated_result is not None:
        accepted = await runtime.jobs.complete(
            job.job_id, job.claim_token, generated_result
        )
        status = TaskStatus.COMPLETED if accepted else TaskStatus.CLAIM_LOST
    elif published_output_key is not None:
        accepted = await runtime.jobs.complete(
            job.job_id,
            job.claim_token,
            VideoJobResult(
                output_key=published_output_key,
                derivative_error=str(error),
            ),
        )
        status = TaskStatus.COMPLETED if accepted else TaskStatus.CLAIM_LOST
    else:
        accepted = await runtime.jobs.fail(
            job.job_id,
            job.claim_token,
            str(error),
            failure_code=JobFailureCode.TEMPORARY_FAILURE,
            failure_message=(
                "Generation was interrupted by a temporary service problem. "
                + ("Generate the subject again." if job.operation is JobOperation.GENERATE_SUBJECT
                   else "Retry this motion.")
            ),
            retryable=True,
        )
        status = TaskStatus.FAILED if accepted else TaskStatus.CLAIM_LOST
    return TaskResponse(job_id=job.job_id, status=status)


async def _publish_derivatives(
    runtime: Runtime,
    output_key: str,
    derivatives: VideoDerivatives,
    requested_outputs: dict[str, bool],
) -> VideoJobResult:
    stem = output_key.removesuffix(".mp4")
    artifacts = (
        (
            f"{stem}.alpha.webm",
            derivatives.alpha_webm if requested_outputs.get("alpha", False) else None,
            "video/webm",
        ),
        (
            f"{stem}.sprite.png",
            derivatives.sprite_sheet_png
            if requested_outputs.get("sprite", False)
            else None,
            "image/png",
        ),
    )
    for key, content, content_type in artifacts:
        if content is not None:
            await runtime.assets.write_once(key, content, content_type)
    return VideoJobResult(
        output_key=output_key,
        alpha_video_path=artifacts[0][0] if artifacts[0][1] is not None else None,
        sprite_sheet_path=artifacts[1][0] if artifacts[1][1] is not None else None,
        sprite_metadata=(
            derivatives.sprite_metadata
            if artifacts[1][1] is not None
            else None
        ),
    )


def _versioned_output_key(output_key: str, source: bytes) -> str:
    digest = hashlib.sha256(source).hexdigest()
    prefix, separator, filename = output_key.rpartition("/")
    stem, dot, _extension = filename.rpartition(".")
    if not dot:
        stem = filename
    versioned = f"{stem}.{digest}.mp4"
    return f"{prefix}{separator}{versioned}" if separator else versioned


def create_app(
    runtime: Runtime,
    service_role: ServiceRole | str = ServiceRole.ALL,
) -> FastAPI:
    role = ServiceRole(service_role)

    # Must run before FastAPI(...) is constructed so the FastAPI/Starlette
    # ASGI integrations wrap the app from its first request.
    configure_sentry(SentryConfig.from_env(), role.value)

    def require_api() -> None:
        if role in {ServiceRole.WORKER, ServiceRole.COORDINATOR}:
            raise HTTPException(status_code=404, detail="not found")

    def require_worker() -> None:
        if role in {ServiceRole.API, ServiceRole.COORDINATOR}:
            raise HTTPException(status_code=404, detail="not found")

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        if runtime.lifecycle is not None:
            await runtime.lifecycle.start()
            runtime.ready = True
        try:
            yield
        finally:
            runtime.ready = False
            if runtime.lifecycle is not None:
                await runtime.lifecycle.stop()

    # The published schema and its pages come from install_api_reference below.
    application = FastAPI(
        title="Magic Studios API",
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    @application.exception_handler(ManifestUnwritable)
    async def _manifest_unwritable_handler(
        _request: Request, exc: ManifestUnwritable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": "manifest_unwritable",
                "message": (
                    "This change would have left the project unreadable, so it "
                    "was not saved. Nothing else changed; the fault has been logged."
                ),
                "project_id": exc.project_id,
            },
        )

    @application.exception_handler(PromptRefused)
    async def _prompt_refused_handler(_request: Request, exc: PromptRefused) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": exc.code, "category": exc.category, "message": str(exc)}},
        )

    @application.exception_handler(ItemExpiring)
    async def _item_expiring_handler(_request: Request, exc: ItemExpiring) -> JSONResponse:
        return JSONResponse(status_code=410, content={"detail": {"code": exc.code, "message": str(exc)}})

    @application.exception_handler(PromptScreenUnavailable)
    async def _prompt_screen_unavailable_handler(
        _request: Request, exc: PromptScreenUnavailable
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": {"code": exc.code, "message": str(exc)}})

    @application.exception_handler(ManifestUnreadable)
    async def _manifest_unreadable_handler(
        _request: Request, _exc: ManifestUnreadable
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={
                "code": "manifest_unreadable",
                "message": (
                    "This project's latest revision could not be read; "
                    "call repair."
                ),
            },
        )

    @application.post("/internal/retention/sweep")
    async def retention_sweep(request: Request) -> dict[str, object]:
        """Run by Cloud Scheduler every hour. The load balancer never routes
        here; the caller must also present the scheduler's Google identity."""
        caller = runtime.retention_sweep_caller
        sweeper = runtime.retention_sweeper
        if role is not ServiceRole.API or caller is None or sweeper is None:
            raise HTTPException(status_code=404, detail="not found")
        audience, invoker = caller
        email = await _verified_google_caller(request.headers.get("authorization"), audience)
        if email != invoker:
            raise HTTPException(status_code=403, detail="forbidden")
        result = await sweeper.sweep()
        return {
            "expired": result.expired,
            "kept": result.kept,
            "failed": result.failed,
            "refusals_purged": result.refusals_purged,
            **result.purged,
        }

    @application.get("/health")
    async def health() -> dict[str, object]:
        if not runtime.ready:
            raise HTTPException(
                status_code=503,
                detail="runtime plugin is not configured",
            )
        return {
            "status": "healthy",
            "service": "magic-video-editor",
            "capabilities": {
                "generate_motion_v1": runtime.motion_generation_available,
                "compose_frame_v1": runtime.composition_generation_available,
            },
            "worker_pool": {
                "enabled": runtime.worker_pool is not None,
                "auth": (
                    runtime.worker_pool.auth_method
                    if runtime.worker_pool is not None
                    else "none"
                ),
            },
            "observability": {
                "sentry": is_sentry_enabled(),
            },
        }

    @application.post("/tasks/jobs/{job_id}", response_model=TaskResponse)
    async def execute_job(job_id: str) -> TaskResponse:
        require_worker()
        if not runtime.ready:
            raise HTTPException(
                status_code=503,
                detail="runtime plugin is not configured",
            )
        return await _execute(runtime, job_id)

    @application.post("/jobs", response_model=JobResponse)
    async def create_job(
        request: CreateJobRequest,
        actor_id: str = Depends(studio_actor_id),
    ) -> JobResponse:
        require_api()
        canonical_payload = request.payload
        if request.operation is JobOperation.GENERATE_MOTION:
            raise HTTPException(
                status_code=422,
                detail="generate_motion jobs must be derived through the Studio motion endpoint",
            )
        if request.operation is JobOperation.COMPOSE_FRAME:
            raise HTTPException(
                status_code=422,
                detail="compose_frame jobs must be derived through the Studio composition endpoint",
            )
        if request.operation is JobOperation.GENERATE_TURNAROUND:
            raise HTTPException(
                status_code=422,
                detail="generate_turnaround jobs must be derived through the Studio turnaround endpoint",
            )
        if request.operation is JobOperation.GENERATE_SUBJECT:
            # The Studio endpoint applies the account policy and records the tenant.
            raise HTTPException(
                status_code=422,
                detail="generate_subject jobs must be derived through the Studio subject endpoint",
            )
        if request.project_id is not None:
            if runtime.studio_service is None:
                raise HTTPException(status_code=404, detail="studio project not found")
            project = await runtime.studio_service.get_project(
                request.project_id,
                actor_id,
            )
            if project is None:
                raise HTTPException(status_code=404, detail="studio project not found")
        submission = JobSubmission(
            job_id=str(uuid4()),
            idempotency_key=request.idempotency_key,
            operation=request.operation,
            source_key=request.source_key,
            output_key=request.output_key,
            requested_outputs=request.requested_outputs,
            operation_version=request.operation_version,
            project_id=request.project_id,
            manifest_operation_id=request.manifest_operation_id,
            payload=canonical_payload,
        )
        try:
            validate_job_submission(submission)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            submitted = await runtime.jobs.submit(submission)
        except IdempotencyConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="idempotency key belongs to a different request",
            ) from exc
        if runtime.dispatcher is None:
            raise HTTPException(status_code=503, detail="task dispatcher unavailable")
        await runtime.dispatcher.enqueue(submitted.record.job_id)
        return JobResponse.from_record(submitted.record)

    # The load balancer routes /pool/ to this service, so every pool job route
    # authenticates its caller the way the Studio routes do.
    @application.post("/pool/jobs", response_model=PoolJobResponse)
    async def create_pool_job(
        request: PoolJobRequest, actor_id: str = Depends(studio_actor_id)
    ) -> PoolJobResponse:
        require_api()
        service = _require_worker_pool(runtime)
        # These jobs are untenanted house work with a caller-written payload that
        # prompt screening never sees, so only a verified cluster admin may submit
        # them. No studio service means nobody can be verified: fail closed.
        access = getattr(runtime.studio_service, "operators", None)
        if access is None or not await access.is_admin(actor_id):
            raise HTTPException(
                status_code=403,
                detail={"code": "pool_admin_required", "message": "Only cluster administrators can submit pool jobs."},
            )
        try:
            record = await service.submit(
                PoolJobSubmission(
                    job_id=str(uuid4()),
                    idempotency_key=request.idempotency_key,
                    capability_id=request.capability_id,
                    input_keys=tuple(request.input_keys),
                    output_key=request.output_key,
                    payload=request.payload,
                    tenant_id=None,
                    priority=request.priority,
                    attempt_budget=request.attempt_budget,
                    execution_deadline_seconds=request.execution_deadline_seconds,
                )
            )
        except PoolIdempotencyConflict as exc:
            raise HTTPException(
                status_code=409,
                detail="idempotency key belongs to a different request",
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return PoolJobResponse.from_record(record)

    @application.get("/pool/jobs/{job_id}", response_model=PoolJobResponse)
    async def get_pool_job(
        job_id: UUID, actor_id: str = Depends(studio_actor_id)
    ) -> PoolJobResponse:
        require_api()
        normalized_job_id = str(job_id)
        await _authorize_pool_job(runtime, normalized_job_id, actor_id)
        service = _require_worker_pool(runtime)
        record = await service.get(normalized_job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="pool job not found")
        return PoolJobResponse.from_record(record)

    @application.post("/pool/jobs/{job_id}/cancel", response_model=PoolJobResponse)
    async def cancel_pool_job(
        job_id: UUID, actor_id: str = Depends(studio_actor_id)
    ) -> PoolJobResponse:
        require_api()
        normalized_job_id = str(job_id)
        await _authorize_pool_job(runtime, normalized_job_id, actor_id)
        service = _require_worker_pool(runtime)
        await service.cancel(normalized_job_id)
        record = await service.get(normalized_job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="pool job not found")
        return PoolJobResponse.from_record(record)

    @application.post("/assets/uploads", response_model=UploadResponse)
    async def create_asset_upload(
        request: UploadRequest, _actor_id: str = Depends(studio_actor_id)
    ) -> UploadResponse:
        """A signed upload grant for a legacy (non-studio) source key. Signed-in
        callers only: on the API host every path reaches this service, so an open
        route here would let anyone fill the bucket."""
        require_api()
        upload_url = await runtime.assets.create_upload_url(
            request.key, request.content_type
        )
        return UploadResponse(key=request.key, upload_url=upload_url)

    @application.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(
        job_id: UUID,
        actor_id: str = Depends(studio_actor_id),
    ) -> JobResponse:
        require_api()
        record = await runtime.jobs.get(str(job_id))
        if record is None:
            raise HTTPException(status_code=404, detail="video job not found")
        await _authorize_studio_job(runtime, record, actor_id)
        return JobResponse.from_record(record)

    @application.post("/jobs/{job_id}/cancel", response_model=JobResponse)
    async def cancel_job(
        job_id: UUID,
        actor_id: str = Depends(studio_actor_id),
    ) -> JobResponse:
        require_api()
        normalized_job_id = str(job_id)
        record = await runtime.jobs.get(normalized_job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="video job not found")
        await _authorize_studio_job(runtime, record, actor_id)
        await runtime.jobs.cancel(normalized_job_id)
        record = await runtime.jobs.get(normalized_job_id)
        assert record is not None
        return JobResponse.from_record(record)

    if role in {ServiceRole.ALL, ServiceRole.API} and runtime.studio_service is not None:
        api_key_store: StudioApiKeyStore = (
            runtime.studio_api_keys
            if runtime.studio_api_keys is not None
            else MemoryStudioApiKeyStore()
        )
        auth_service = build_studio_auth_service(
            runtime.studio_auth_store
            if runtime.studio_auth_store is not None
            else MemoryStudioAuthStore(),
            projects=runtime.studio_service,
        )
        # `studio_actor_id` is a free-standing dependency shared with the pool router,
        # so both stores reach it through app state rather than a closure.
        application.state.studio_api_key_store = api_key_store
        application.state.studio_auth = auth_service
        application.include_router(
            create_studio_router(
                runtime.studio_service,
                api_keys=api_key_store,
                auth=auth_service,
                enrolment=runtime.studio_enrolment,
                pool_enrolment=(
                    None
                    if runtime.worker_pool is None
                    else partial(enroll_in_pool, runtime.worker_pool)
                ),
                provider_accounts=getattr(runtime.studio_service, "provider_accounts", None),
                owner_erasers=(
                    ()
                    if runtime.movie_service is None
                    else (runtime.movie_service.erase_owner,)
                )
                + (() if runtime.audio_service is None else (runtime.audio_service.erase_owner,))
                + (() if runtime.assets3d_service is None else (runtime.assets3d_service.erase_owner,))
                + tuple(erase for extension in runtime.extensions for erase in extension.owner_erasers),
            )
        )
    # Desktop mode serves its own generation config; it has neither a pool nor
    # provider accounts, so this route stays out of its way.
    if (role in {ServiceRole.ALL, ServiceRole.API} and runtime.studio_service is not None
            and (runtime.worker_pool is not None
                 or getattr(runtime.studio_service, "provider_accounts", None) is not None)):
        studio_service = runtime.studio_service

        @application.get("/studio/generation-config")
        async def generation_config_route(worker_id: str | None = None,
                                          actor_id: str = Depends(studio_actor_id)) -> dict:
            """Machine defaults plus bring-your-own-key providers. A provider is
            only used when the caller passes its returned settings."""
            defaults = studio_service.machine_defaults
            config = (await defaults.discover(actor_id, worker_id) if defaults is not None
                      else {"scope": "hosted", "workers": [], "selected": None, "settings": None})
            account = await studio_service.account_capabilities(actor_id)
            if not account["h3"]:
                # Every machine recipe is MiniMax H3; offer none to select.
                config = {**config, "selected": None, "settings": None}
            return {**config, "account": account,
                    "providers": await studio_service.hosted_provider_options(actor_id)}

    if role in {ServiceRole.ALL, ServiceRole.API} and runtime.worker_pool is not None:
        # Read-only pool status for the studio header, gated by the same trusted
        # actor identity the studio routes require.
        pool_service = runtime.worker_pool
        if runtime.studio_service is not None:
            from magic_video_editor.machine_defaults import HostedMachineDefaults
            runtime.studio_service.machine_defaults = HostedMachineDefaults(
                pool_service, runtime.studio_service._tenant_for, runtime.studio_service.operators
            )
            stages = getattr(runtime.assets3d_service, "stages", None)
            if stages is not None:
                # 3D stages resolve their pinned worker through the same tenant-
                # scoped discovery as motions.
                stages.pool = pool_service
                stages.machines = runtime.studio_service.machine_defaults

        # Declared before the library router so it wins the path: the library's
        # own `/pool/workers` takes `tenant_id` as a query parameter, which would
        # let a caller ask for someone else's machines. Here the caller's identity
        # decides; only explicit cluster admins can inspect the full registry.
        @application.get("/pool/workers", response_model=StudioPoolWorkersResponse)
        async def studio_pool_workers(
            actor_id: str = Depends(studio_actor_id),
        ) -> StudioPoolWorkersResponse:
            """The machines that can run this person's work: the ones they
            enrolled, plus the shared house machines, which are untenanted.
            Each row lists the features it can run and why not the rest."""
            access = getattr(runtime.studio_service, "operators", None)
            if access is not None and await access.is_admin(actor_id):
                rows = []
                for view in await pool_service.worker_views():
                    enrolled = await access.enrolment.worker_by_id(view.worker_id)
                    rows.append(StudioPoolWorkerResponse.for_view(view, enrolled.label if enrolled else None))
                return StudioPoolWorkersResponse(cluster_admin=True, workers=rows)
            own = await pool_service.worker_views(tenant_id=actor_id)
            # A public account cannot use the house machines, so it is not shown them.
            policy = getattr(runtime.studio_service, "account_policy", None)
            public = policy is not None and await policy.is_public(actor_id)
            house = [] if public else await pool_service.worker_views(tenant_id=None)
            owned_ids = {view.worker_id for view in own}
            shared = [
                view
                for view in house
                if view.tenant_id is None and view.worker_id not in owned_ids
            ]
            return StudioPoolWorkersResponse(
                workers=[
                    StudioPoolWorkerResponse.for_view(view) for view in (*own, *shared)
                ]
            )

        async def authorize_pool_admin(
            actor_id: str = Depends(studio_actor_id),
        ) -> bool:
            """The library's status routes are global views (every worker, the
            whole queue), so only a verified cluster admin may reach them. An
            ordinary signed-in actor is refused, not narrowed: tenant views are
            the studio's own routes above."""
            access = getattr(runtime.studio_service, "operators", None)
            if access is None or not await access.is_admin(actor_id):
                raise HTTPException(
                    status_code=403, detail="pool administrator required"
                )
            return True

        application.include_router(
            create_pool_status_router(
                pool_service, authorize_admin=authorize_pool_admin
            )
        )
        # Worker endpoints under /pool as well as on the coordinator. A machine
        # someone enrolled authenticates with its own credential, not a Google
        # identity, so it cannot pass the coordinator's Cloud Run IAM at all --
        # it never reaches the app to be authenticated. /pool/ already routes
        # here through the load balancer, and the router carries its own auth.
        application.include_router(
            create_worker_router(pool_service), prefix="/pool"
        )
    if role in {ServiceRole.ALL, ServiceRole.API}:
        extension_routes = RouterContext(
            studio_service=runtime.studio_service,
            auth=getattr(application.state, "studio_auth", None),
            api_keys=getattr(application.state, "studio_api_key_store", None) or MemoryStudioApiKeyStore(),
            operators=getattr(runtime.studio_service, "operators", None),
        )
        for extension in runtime.extensions:
            if extension.routers is not None:
                for router in extension.routers(extension_routes):
                    application.include_router(router)
        application.include_router(create_download_router())
    if runtime.movie_service is not None and role in {ServiceRole.ALL, ServiceRole.API}:
        from magic_video_editor.movies.routes import create_movie_router
        application.include_router(create_movie_router(runtime.movie_service))
        from magic_video_editor.flights.routes import create_scene_flight_router
        from magic_video_editor.flights.service import SceneFlightService
        application.include_router(
            create_scene_flight_router(SceneFlightService(runtime.movie_service))
        )
    if runtime.audio_service is not None and role in {ServiceRole.ALL, ServiceRole.API}:
        from magic_video_editor.audio.routes import create_audio_router
        application.include_router(create_audio_router(runtime.audio_service))
    if runtime.assets3d_service is not None and role in {ServiceRole.ALL, ServiceRole.API}:
        from magic_video_editor.assets3d.routes import create_assets3d_router
        application.include_router(create_assets3d_router(runtime.assets3d_service))
    if (
        role in {ServiceRole.ALL, ServiceRole.COORDINATOR}
        and runtime.worker_pool is not None
    ):
        application.include_router(create_worker_router(runtime.worker_pool))
    if role in {ServiceRole.ALL, ServiceRole.API}:
        install_api_reference(
            application,
            docs={key: doc for extension in runtime.extensions for key, doc in extension.route_docs.items()},
            hidden=frozenset().union(*(extension.hidden_routes for extension in runtime.extensions)),
            tags={name: text for extension in runtime.extensions for name, text in extension.route_tags.items()},
        )

    return application


def _require_worker_pool(runtime: Runtime) -> WorkerPoolService:
    if runtime.worker_pool is None:
        raise HTTPException(status_code=503, detail="worker pool is not configured")
    return runtime.worker_pool


async def _studio_job_owner(runtime: Runtime, record: JobRecord) -> str | None:
    """Which machines may run this job.

    The owner when they have attached a machine, so their work runs on their own
    hardware; `None` otherwise, so the shared house machines can take it. A job
    bound to an owner with no machine is leasable by nobody.
    """
    if record.project_id is None or runtime.studio_service is None:
        return None
    owner_id = await runtime.studio_service.owner_of(record.project_id)
    if owner_id is None or runtime.studio_enrolment is None:
        return None
    return await runtime.studio_service._tenant_for(owner_id)


async def _authorize_studio_job(
    runtime: Runtime,
    record: JobRecord,
    actor_id: str | None,
) -> None:
    project_id = record.project_id
    if project_id is None:
        return
    if actor_id is None:
        raise HTTPException(status_code=401, detail="verified studio actor required")
    if runtime.studio_service is None:
        raise HTTPException(status_code=404, detail="studio project not found")
    project = await runtime.studio_service.get_project(project_id, actor_id)
    if project is None:
        raise HTTPException(status_code=404, detail="studio project not found")


async def _authorize_pool_job(
    runtime: Runtime,
    job_id: str,
    actor_id: str | None,
) -> None:
    """A studio motion executes on the pool under its own studio job id, so a pool
    record can carry another operator's project keys and prompt -- authorize it as
    the studio job it is. Other pool work that writes into a Studio project (its
    subject jobs) answers only to that project's owner, and house work submitted
    by an admin only to cluster admins."""
    pool_record = None
    if runtime.worker_pool is not None:
        pool_record = await runtime.worker_pool.get(job_id)
        if (
            pool_record is not None
            and pool_record.output_key.startswith("outputs/movies/")
        ):
            raise HTTPException(status_code=404, detail="pool job not found")
    record = await runtime.jobs.get(job_id)
    if record is not None:
        await _authorize_studio_job(runtime, record, actor_id)
        return
    if pool_record is None:
        return
    service = runtime.studio_service
    parts = pool_record.output_key.split("/")
    if parts[:2] == ["outputs", "studio"] and len(parts) > 2:
        project = (
            await service.get_project(parts[2], actor_id)
            if service is not None and actor_id is not None
            else None
        )
        if project is None:
            raise HTTPException(status_code=404, detail="pool job not found")
        return
    # What remains is house work submitted through `POST /pool/jobs`, which only a
    # cluster admin may do, so only a cluster admin may read or cancel it: its id is
    # visible to anyone who can see a busy house worker.
    access = getattr(service, "operators", None)
    if access is None or actor_id is None or not await access.is_admin(actor_id):
        raise HTTPException(status_code=404, detail="pool job not found")


def _comfy_capability_schemas() -> CapabilitySchemas:
    """The packaged workflow templates, read as schemas and nothing more.

    The coordinator publishes what an approved worker can serve; it never builds
    the plugin, so it opens no connection to anybody's local runtime. Imported
    here because the library's `comfy` module needs the `comfy` extra.
    """
    from outbound_gpu_worker_pool.comfy import (
        PACKAGED_TEMPLATES_DIRECTORY,
        TemplateRegistry,
        capability_schemas,
    )

    from outbound_gpu_worker_pool.machine_profiles import PROFILE_DIRECTORY
    return {
        **capability_schemas(TemplateRegistry.from_directory(PACKAGED_TEMPLATES_DIRECTORY)),
        **capability_schemas(TemplateRegistry.from_directory(PROFILE_DIRECTORY)),
    }


def runtime_from_env(
    environment: dict[str, str] | None = None,
    *,
    service_role: ServiceRole | str | None = None,
) -> Runtime:
    values = environment if environment is not None else dict(os.environ)
    resolved_service_role = ServiceRole(
        service_role
        if service_role is not None
        else values.get("MAGIC_VIDEO_EDITOR_SERVICE_ROLE", ServiceRole.ALL)
    )
    job_plugin = values.get("MAGIC_VIDEO_EDITOR_JOB_PLUGIN", "postgres")
    asset_plugin = values.get("MAGIC_VIDEO_EDITOR_ASSET_PLUGIN", "gcs")
    gpu_plugin = values.get("MAGIC_VIDEO_EDITOR_GPU_PLUGIN", "none")
    subject_plugin = values.get("MAGIC_VIDEO_EDITOR_SUBJECT_PLUGIN", "none")
    composition_plugin = values.get("MAGIC_VIDEO_EDITOR_COMPOSITION_PLUGIN", "none")
    if job_plugin != "postgres":
        raise ValueError(f"unsupported job plugin: {job_plugin}")
    if asset_plugin != "gcs":
        raise ValueError(f"unsupported asset plugin: {asset_plugin}")
    if gpu_plugin not in {"none", "http"}:
        raise ValueError(f"unsupported GPU plugin: {gpu_plugin}")
    if subject_plugin not in {"none", "http", "pool"}:
        raise ValueError(f"unsupported subject plugin: {subject_plugin}")
    if composition_plugin not in {"none", "http"}:
        raise ValueError(f"unsupported composition plugin: {composition_plugin}")
    worker_auth = values.get("MAGIC_VIDEO_EDITOR_WORKER_AUTH", WORKER_AUTH_NONE)
    if worker_auth not in {
        WORKER_AUTH_NONE,
        STATIC_AUTH_METHOD,
        GOOGLE_OIDC_AUTH_METHOD,
    }:
        raise ValueError(f"unsupported worker auth mode: {worker_auth}")
    database_url = values.get("MAGIC_VIDEO_EDITOR_DATABASE_URL")
    if not database_url:
        database_url = _cloud_sql_database_url(values)
    output_bucket = values.get("MAGIC_VIDEO_EDITOR_OUTPUT_BUCKET")
    task_project = values.get("MAGIC_VIDEO_EDITOR_TASK_PROJECT")
    task_location = values.get("MAGIC_VIDEO_EDITOR_TASK_LOCATION")
    task_queue = values.get("MAGIC_VIDEO_EDITOR_TASK_QUEUE")
    service_url = values.get("MAGIC_VIDEO_EDITOR_SERVICE_URL")
    task_account = values.get("MAGIC_VIDEO_EDITOR_TASK_SERVICE_ACCOUNT")
    signing_account = values.get("MAGIC_VIDEO_EDITOR_SIGNING_SERVICE_ACCOUNT")
    if not all(
        (
            database_url,
            output_bucket,
            task_project,
            task_location,
            task_queue,
            service_url,
            task_account,
            signing_account,
        )
    ):
        return UnconfiguredRuntime()
    jobs = PostgresJobStore(database_url)
    telemetry_store = PostgresGpuTelemetryStore(database_url)
    assets = GcsAssetStore(
        output_bucket=output_bucket,
        signing_service_account_email=signing_account,
    )
    processor = FfmpegVideoProcessor()
    dispatcher = CloudTasksDispatcher(
        project=task_project,
        location=task_location,
        queue=task_queue,
        service_url=service_url,
        service_account_email=task_account,
    )
    studio_store: PostgresStudioStore | None = None
    studio_api_keys: PostgresStudioApiKeyStore | None = None
    studio_auth_store: PostgresStudioAuthStore | None = None
    studio_enrolment: PostgresStudioEnrolmentStore | None = None
    studio_owner_usage: PostgresStudioOwnerUsageStore | None = None
    provider_account_store: PostgresProviderAccountStore | None = None
    prompt_refusals: PostgresPromptRefusalStore | None = None
    item_activity: PostgresActivityStore | None = None
    retention_sweeper: RetentionSweeper | None = None
    studio_service: StudioService | None = None
    movie_store = None
    movie_service = None
    audio_store = None
    audio_service = None
    assets3d_store = None
    assets3d_service = None
    subject_generator = None
    if subject_plugin == "http":
        subject_endpoint = values.get("MAGIC_VIDEO_EDITOR_SUBJECT_ENDPOINT")
        subject_audience = values.get("MAGIC_VIDEO_EDITOR_SUBJECT_AUDIENCE")
        if not subject_endpoint or not subject_audience:
            return UnconfiguredRuntime()
        subject_generator = HttpSubjectGenerator(
            endpoint=subject_endpoint, audience=subject_audience
        )
    composition_generator = None
    if composition_plugin == "http":
        composition_url = values.get("MAGIC_VIDEO_EDITOR_COMPOSITION_URL")
        composition_audience = values.get("MAGIC_VIDEO_EDITOR_COMPOSITION_AUDIENCE")
        if not composition_url or not composition_audience:
            return UnconfiguredRuntime()
        composition_generator = HttpCompositionGenerator(
            endpoint=composition_url, audience=composition_audience
        )
    composition_operations_enabled = (
        composition_generator is not None
        and values.get(
            "MAGIC_VIDEO_EDITOR_COMPOSITION_OPERATIONS_ENABLED", "false"
        ).lower()
        == "true"
    )
    gpu = None
    if gpu_plugin == "http":
        gpu_endpoint = values.get("MAGIC_VIDEO_EDITOR_GPU_ENDPOINT")
        gpu_audience = values.get("MAGIC_VIDEO_EDITOR_GPU_AUDIENCE")
        if not gpu_endpoint or not gpu_audience:
            return UnconfiguredRuntime()
        gpu = HttpGpuProcessor(endpoint=gpu_endpoint, audience=gpu_audience)
    pool_jobs: PostgresPoolJobStore | None = None
    worker_registry: PostgresWorkerRegistry | None = None
    worker_audit: PostgresAuditLog | None = None
    worker_pool: WorkerPoolService | None = None
    # Built here, before the authenticator, because an enrolled machine
    # authenticates against it; the studio router later reuses the same store.
    studio_enrolment = PostgresStudioEnrolmentStore(database_url)
    studio_auth_store = PostgresStudioAuthStore(database_url)
    default_operators = OperatorAccess(
        studio_auth_store, admin_emails_from_env(values.get(ADMIN_EMAILS_ENV)), studio_enrolment
    )
    extensions = load_extensions(ExtensionContext(
        values=values,
        users=studio_auth_store,
        enrolment=studio_enrolment,
        operators=default_operators,
        database_url=database_url,
        serves_studio=resolved_service_role is not ServiceRole.WORKER,
    ))
    operators = extension_operators(extensions, default_operators)
    # Only the explicit allowlist and the operators get the shared machines.
    account_policy = account_policy_from_env(values, studio_auth_store, operators.is_admin)
    pool_job_factory = next(
        (extension.pool_jobs for extension in extensions if extension.pool_jobs is not None),
        PostgresPoolJobStore,
    )
    if worker_auth != WORKER_AUTH_NONE:
        pool_jobs = pool_job_factory(database_url)
        worker_registry = PostgresWorkerRegistry(database_url)
        worker_audit = PostgresAuditLog(database_url)
        worker_enrollments = _worker_enrollments(values)
        if worker_auth == STATIC_AUTH_METHOD:
            worker_tokens = values.get("MAGIC_VIDEO_EDITOR_WORKER_TOKENS")
            if not worker_tokens:
                return UnconfiguredRuntime()
            authenticator = StaticTokenWorkerAuthenticator.from_env_value(worker_tokens)
        else:
            worker_audience = values.get("MAGIC_VIDEO_EDITOR_WORKER_AUDIENCE")
            if not worker_audience:
                return UnconfiguredRuntime()
            # Cloud Run IAM decides who may reach this service at all, but it
            # no longer decides who joins the pool: a verified identity with no
            # registry row is admitted only when the server's explicit
            # enrollment allowlist names that exact account, and the allowlist
            # (never the account name) chooses its worker id and tenant. With no
            # allowlist, only already-registered workers authenticate.
            authenticator = GoogleIdTokenWorkerAuthenticator(
                audience=worker_audience,
                registry=worker_registry,
                auto_enroll=bool(worker_enrollments),
                enrollments=worker_enrollments,
            )
        # A machine someone enrolled through the studio presents an `mvsw_`
        # credential; anything else is left to the configured method above.
        authenticator = EnrolledWorkerAuthenticator(
            studio_enrolment, fallback=authenticator, registry=worker_registry
        )
        worker_pool = WorkerPoolService(
            pool_jobs,
            assets,
            worker_registry,
            worker_audit,
            authenticator,
            {
                **capability_schemas_from_plugins([DeterministicEchoPlugin()]),
                **_comfy_capability_schemas(),
                **capability_schemas_from_plugins([MovieCpuPlugin()]),
            },
            auth_method=worker_auth,
            enrollments=worker_enrollments,
            **_worker_admission_limits(values),
        )
    if subject_plugin == "pool":
        if worker_pool is None:
            return UnconfiguredRuntime()

        subject_generator = PoolSubjectGenerator(
            pool=worker_pool, assets=assets, house_tenants=operators.shared_tenants,
            enrolment=studio_enrolment,
        )
    if resolved_service_role is not ServiceRole.WORKER:
        studio_store = PostgresStudioStore(database_url)
        studio_api_keys = PostgresStudioApiKeyStore(database_url)
        studio_owner_usage = PostgresStudioOwnerUsageStore(database_url)
        provider_account_store = PostgresProviderAccountStore(database_url)
        prompt_refusals = PostgresPromptRefusalStore(database_url)
        prompt_screening = configured_prompt_screening(values, prompt_refusals)
        item_activity = PostgresActivityStore(database_url)
        retention = Retention(RetentionPolicy.from_env(values), item_activity)
        studio_service = StudioService(
            prompt_screening=prompt_screening,
            retention=retention,
            provider_accounts=ProviderAccounts(
                provider_account_store, CredentialCipher.from_env(values)
            ),
            owner_usage=studio_owner_usage,
            owner_limits=OwnerLimits.from_env(values),
            enrolment=studio_enrolment,
            operators=operators,
            account_policy=account_policy,
            subject_generation_on_pool=subject_plugin == "pool",
            store=studio_store,
            assets=assets,
            jobs=jobs,
            dispatcher=dispatcher,
            motion_generation_available=values.get(
                "MAGIC_VIDEO_EDITOR_MOTION_OPERATIONS_ENABLED", "false"
            ).lower()
            == "true",
            subject_generation_available=(
                subject_generator is not None
                and values.get(
                    "MAGIC_VIDEO_EDITOR_SUBJECT_OPERATIONS_ENABLED", "false"
                ).lower()
                == "true"
            ),
            composition_generation_available=composition_operations_enabled,
            gpu_telemetry_store=telemetry_store,
            motion_pool=worker_pool,
        )
    if resolved_service_role is not ServiceRole.WORKER:
        from magic_video_editor.movies.cpu_pool import PoolMovieMedia
        from magic_video_editor.movies.director import configured_director
        from magic_video_editor.movies.media import MovieMedia
        from magic_video_editor.movies.planner import configured_planner
        from magic_video_editor.movies.service import MovieService
        from magic_video_editor.flights.media import pinned_pool_supports_scene_flights
        from magic_video_editor.movies.store import PostgresMovieStore

        async def movie_work_guard(owner: str, requested: int) -> None:
            from magic_video_editor.movies.service import MovieError

            usage = await studio_service.owner_usage(owner)
            if (
                usage.storage_limit_bytes
                and usage.stored_bytes >= usage.storage_limit_bytes
            ):
                raise MovieError(
                    "Storage limit reached. Free storage before starting new work.", 507
                )
            if (
                usage.concurrent_job_limit
                and usage.running_jobs + requested > usage.concurrent_job_limit
            ):
                raise MovieError(
                    "Concurrent generation limit reached. Wait for work to finish.", 429
                )

        movie_store = PostgresMovieStore(database_url)
        movie_service = MovieService(
            movie_store,
            assets,
            worker_pool,
            composition_generator if composition_operations_enabled else None,
            PoolMovieMedia(assets, worker_pool, access=operators) if worker_pool else MovieMedia(assets),
            planner=configured_planner(values),
            director=configured_director(values),
            prompt_screening=studio_service.prompt_screening,
            retention=retention,
            tenant_for=studio_service._own_tenant_for,
            shared_tenant_for=operators.shared_tenant,
            house_tenants=operators.shared_tenants,
            work_guard=movie_work_guard,
            account_policy=account_policy,
            # Hosted workers run the pinned pool templates; flights need one
            # whose standard H3 graph binds a last frame.
            scene_flights_available=worker_pool is not None
            and pinned_pool_supports_scene_flights(),
        )
        movie_service.enrolment = studio_enrolment
        from magic_video_editor.audio.freesound import FreesoundClient
        from magic_video_editor.audio.media import AudioMedia
        from magic_video_editor.audio.service import AudioError, AudioService
        from magic_video_editor.audio.store import PostgresAudioStore

        async def audio_work_guard(owner: str, requested: int) -> None:
            from magic_video_editor.movies.service import MovieError

            try:
                await movie_work_guard(owner, requested)
            except MovieError as e:
                raise AudioError(str(e), e.status) from e

        audio_store = PostgresAudioStore(database_url)
        audio_service = AudioService(
            audio_store,
            assets,
            freesound=FreesoundClient.from_env(values),
            media=AudioMedia(),
            account_policy=account_policy,
            work_guard=audio_work_guard,
            movie_service=movie_service,
        )
        from magic_video_editor.assets3d.service import Asset3DError, Assets3DService
        from magic_video_editor.assets3d.stages import StageEngine, generation_enabled
        from magic_video_editor.assets3d.store import PostgresAssets3DStore

        async def assets3d_work_guard(owner: str, requested: int) -> None:
            from magic_video_editor.movies.service import MovieError

            try:
                await movie_work_guard(owner, requested)
            except MovieError as e:
                raise Asset3DError(str(e), e.status) from e

        async def assets3d_allowance(owner: str) -> int | None:
            # The owner's limit less what every other section stores; 3D grants
            # reserve against it under the store's per-owner lock.
            limit = (await studio_service.owner_usage(owner)).storage_limit_bytes
            if not limit:
                return None
            others = (
                await studio_owner_usage.stored_bytes(owner)
                + await movie_store.stored_bytes(owner)
                + await audio_store.stored_bytes(owner)
            )
            return limit - others

        assets3d_store = PostgresAssets3DStore(database_url)
        assets3d_service = Assets3DService(
            assets3d_store,
            assets,
            account_policy=account_policy,
            work_guard=assets3d_work_guard,
            storage_allowance=assets3d_allowance,
            # Off unless set; even on, nothing runs while the engine profile is
            # unqualified. The pool and machine discovery attach in create_app.
            stages=StageEngine(
                enabled=generation_enabled(values),
                own_tenant_for=studio_service._own_tenant_for,
            ),
        )

        async def running_jobs(owner: str) -> int:
            return await movie_service.running_jobs(owner) + await assets3d_service.running_jobs(owner)

        studio_service.additional_running_jobs = running_jobs

        async def stored_bytes(owner: str) -> int:
            return (
                await movie_store.stored_bytes(owner)
                + await audio_store.stored_bytes(owner)
                + await assets3d_store.stored_bytes(owner)
            )

        studio_service.additional_stored_bytes = stored_bytes
        retention_sweeper = RetentionSweeper(
            retention,
            listers={PROJECT: lambda: studio_store.list_project_owners(), MOVIE: lambda: movie_store.list_owners()},
            expirers={PROJECT: studio_service.expire_project, MOVIE: movie_service.expire},
            purge_refusals=prompt_screening.purge_expired,
            purges={name: purge for extension in extensions for name, purge in extension.purges.items()},
        )
    return Runtime(
        jobs=jobs,
        assets=assets,
        processor=processor,
        ready=False,
        lifecycle=ProductionLifecycle(
            jobs,
            assets,
            processor,
            studio_store,
            telemetry_store,
            pool_jobs,
            worker_registry,
            worker_audit,
            studio_api_keys,
            studio_auth_store,
            studio_owner_usage,
            studio_enrolment,
            movie_store,
            provider_account_store,
            prompt_refusals,
            item_activity,
            audio_store,
            assets3d_store,
            tuple(component for extension in extensions for component in extension.components),
        ),
        dispatcher=dispatcher,
        gpu=gpu,
        studio_service=studio_service,
        studio_api_keys=studio_api_keys,
        studio_auth_store=studio_auth_store,
        studio_enrolment=studio_enrolment,
        subject_generator=subject_generator,
        subject_operations_enabled=(
            values.get("MAGIC_VIDEO_EDITOR_SUBJECT_OPERATIONS_ENABLED", "false").lower()
            == "true"
        ),
        composition_generator=composition_generator,
        composition_operations_enabled=composition_operations_enabled,
        gpu_telemetry_provider=gpu if gpu_plugin == "http" else None,
        gpu_telemetry_store=telemetry_store,
        motion_operations_enabled=(
            values.get("MAGIC_VIDEO_EDITOR_MOTION_OPERATIONS_ENABLED", "false").lower()
            == "true"
        ),
        worker_pool=worker_pool,
        movie_service=movie_service,
        audio_service=audio_service,
        assets3d_service=assets3d_service,
        extensions=extensions,
        retention_sweeper=retention_sweeper,
        retention_sweep_caller=_retention_sweep_caller(values),
    )


async def _stop_components(
    components: Iterable[RuntimeLifecycle],
) -> list[Exception]:
    errors: list[Exception] = []
    for component in components:
        try:
            await component.stop()
        except Exception as exc:
            errors.append(exc)
    return errors


def _cloud_sql_database_url(values: dict[str, str]) -> str | None:
    password = values.get("MAGIC_VIDEO_EDITOR_DATABASE_PASSWORD")
    instance = values.get("MAGIC_VIDEO_EDITOR_DATABASE_INSTANCE")
    if not password or not instance:
        return None
    user = quote(values.get("MAGIC_VIDEO_EDITOR_DATABASE_USER", "magic_video_editor"), safe="")
    encoded_password = quote(password, safe="")
    database = quote(
        values.get("MAGIC_VIDEO_EDITOR_DATABASE_NAME", "magic_video_editor"), safe=""
    )
    socket_path = quote(f"/cloudsql/{instance}", safe="")
    return f"postgresql://{user}:{encoded_password}@/{database}?host={socket_path}"


_service_role = ServiceRole(
    os.getenv("MAGIC_VIDEO_EDITOR_SERVICE_ROLE", ServiceRole.ALL)
)

app = create_app(
    runtime_from_env(service_role=_service_role),
    service_role=_service_role,
)


__all__ = [
    "InMemoryRuntime",
    "UnconfiguredRuntime",
    "app",
    "create_app",
    "runtime_from_env",
]

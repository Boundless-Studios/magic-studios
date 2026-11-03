"""Turnarounds: one closed-loop turn of a bound view reference (design: docs/design/
direction-views-from-one-image.md, phase 1).

A turnaround is pool-only work on the standard motion capability, pinned to a
worker that reports last-frame support. It is stored on the manifest as a
`TurnaroundTake`, never as a motion, so motions, exports and completeness ignore
it. Picks and drafts (phase 2) build on the settled take.
"""

import asyncio
import io
import json
import logging
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

import numpy as np
from outbound_gpu_worker_pool.contracts import JobSubmission as PoolJobSubmission
from PIL import Image

from magic_video_editor.api_models import GenerateTurnaroundPayloadV1
from magic_video_editor.contracts import (
    AssetNotFound,
    AssetStorageUnavailable,
    AssetTooLarge,
    JobFailureCode,
    JobOperation,
    JobRecord,
    JobStatus,
    JobSubmission,
)

from .models import (
    Backdrop,
    MotionCandidate,
    MotionGenerationSettings,
    TurnaroundEquipment,
    TurnaroundPick,
    TurnaroundPickSet,
    TurnaroundPreparedFrame,
    TurnaroundTake,
    ViewId,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "turnaround_prompt.v1"
# v2: a second profile must narrow too, and the decoded frame count must match the take.
PICKS_VERSION = "turnaround-picks-v2"
PREPARATION_ALGORITHM = "full_canvas_contain_v1"
_SLOTS = ("first_frame", "last_frame")
_PAGE = 100
_ANALYSIS_LIMIT = 100_000_000
# One cold analysis per take at a time: concurrent reads wait for it, then use its cache.
# Each entry is [lock, participants]; it stays registered until the last one leaves.
_ANALYSIS_LOCKS: dict[str, list] = {}

# Front and front three-quarter sources are qualified (see the design's "Picking
# frames"). SE and SW joined on 2026-10-23 after real turntables of two characters
# each (the three-quarter proof, kept in the private art-archive-2026-11-02 release).
QUALIFIED_SOURCES = frozenset({ViewId.S, ViewId.SE, ViewId.SW})
_USE_S = "use a front-facing (S) or front three-quarter (SE, SW) view as the source"
_SOURCE_REFUSALS = {
    ViewId.NE: f"Back three-quarter (NE and NW) sources are untested; {_USE_S}",
    ViewId.NW: f"Back three-quarter (NE and NW) sources are untested; {_USE_S}",
    ViewId.E: f"A profile or back source would put a heading on the loop's seam; {_USE_S}",
    ViewId.W: f"A profile or back source would put a heading on the loop's seam; {_USE_S}",
    ViewId.N: f"A profile or back source would put a heading on the loop's seam; {_USE_S}",
}
_S_TURN = (
    "first turning to face the right side of the picture, then away from the viewer, "
    "then the left side of the picture, and back to face the viewer"
)
# The three-quarter sources qualified with the S prompt word for word.
_TURN_ORDER = {ViewId.S: _S_TURN, ViewId.SE: _S_TURN, ViewId.SW: _S_TURN}
_ATTACHMENT_SENTENCES = {
    "hand": "The {item} stays in the same hand.",
    "shoulder": "The {item} stays on the same shoulder.",
    "hip": "The {item} stays on the same hip.",
    "back": "The {item} stays on the back.",
    "other": "The {item} stays where it is attached.",
}
_CHROMA_CLAUSE = (
    "Render against a flat chroma green (#00B140) backdrop with even lighting, "
    "no shadows on the backdrop, and no green spill on the subject."
)
_GREEN_MIN = 120
_GREEN_MARGIN = 40
_BORDER_PX = 4
_CLEAR_ALPHA = 32
_TRANSPARENT_BORDER_MIN = 0.9
_CHROMA_BORDER_MIN = 0.9


# The pool rejected the submission outright, so nothing was stored or will run.
_DEFINITE_REFUSALS = (ValueError, PermissionError, LookupError)


class TurnaroundUnavailable(Exception):
    """No machine (or no supported source) can run this turnaround."""


def require_qualified_source(heading: ViewId | None) -> None:
    """Refuse a source heading that is not qualified, saying why."""
    if heading in QUALIFIED_SOURCES:
        return
    if heading is None:
        raise TurnaroundUnavailable(f"This layout's views have no compass heading; {_USE_S}")
    raise TurnaroundUnavailable(_SOURCE_REFUSALS.get(heading, f"Unsupported source heading; {_USE_S}"))


def turnaround_prompt(heading: ViewId, equipment: tuple[TurnaroundEquipment, ...]) -> str:
    """`turnaround_prompt.v1`: deterministic, server-owned and side-neutral.

    Equipment keeps its attachment without naming a body side: the side comes
    from the first frame, and naming a character's left or right is what failed
    in still-image generation.
    """
    require_qualified_source(heading)
    order = _TURN_ORDER[heading]
    parts = [
        f"The character stands on the spot and turns slowly in place through one full circle: {order}.",
        "The feet step around in place; the camera stays fixed, the body size stays constant "
        "and the whole body stays visible.",
        *(_ATTACHMENT_SENTENCES[row.attachment].format(item=row.item) for row in equipment),
        "All other equipment stays in the same hand and on the same shoulder throughout.",
    ]
    return " ".join(parts) + "\n\n" + _CHROMA_CLAUSE


def source_backdrop(content: bytes) -> str:
    """`transparent` or `chroma`; anything else would put scenery behind the turn.

    Judged on the image border, where a background shows: a transparent source
    needs nearly all of its border clear, so a few translucent pixels on an
    opaque scenery image never count; a chroma source needs nearly all of its
    border pixels individually opaque and green.
    """
    with Image.open(io.BytesIO(content)) as opened:
        rgba = np.asarray(opened.convert("RGBA"))
    ring = min(_BORDER_PX, max(1, min(rgba.shape[0], rgba.shape[1]) // 8))
    border = np.concatenate([rgba[:ring].reshape(-1, 4), rgba[-ring:].reshape(-1, 4),
                             rgba[:, :ring].reshape(-1, 4), rgba[:, -ring:].reshape(-1, 4)]).astype(int)
    if float((border[:, 3] < _CLEAR_ALPHA).mean()) >= _TRANSPARENT_BORDER_MIN:
        return "transparent"
    # Each border pixel must be opaque chroma green on its own: a median would let
    # a border just over half green carry nearly half of it in scenery.
    red, green, blue, alpha = border[:, 0], border[:, 1], border[:, 2], border[:, 3]
    chroma = (alpha >= 250) & (green >= _GREEN_MIN) & (green - np.maximum(red, blue) >= _GREEN_MARGIN)
    if float(chroma.mean()) >= _CHROMA_BORDER_MIN:
        return "chroma"
    raise TurnaroundUnavailable("Needs a transparent or green-screen reference")


def _source_view(manifest, view_id: str):
    view = next((v for v in manifest.views if str(v.view_id) == view_id), None)
    if view is None:
        raise ValueError("source view is not declared in the project")
    heading = view.heading if view.setup_version else view.view_id
    return view, ViewId(heading) if heading is not None else None


def _take(manifest, turnaround_id: str) -> TurnaroundTake:
    take = next((t for t in manifest.turnarounds if t.turnaround_id == turnaround_id), None)
    if take is None:
        raise ValueError("turnaround is not present in the project")
    return take


async def _resolve_settings(service, owner_id: str, settings: MotionGenerationSettings | None) -> MotionGenerationSettings:
    defaults = getattr(service, "machine_defaults", None)
    if defaults is None or service._motion_pool is None:
        raise TurnaroundUnavailable("Turnarounds need a hosted machine that reports last-frame support")
    if settings is None:
        from outbound_gpu_worker_pool.machine_profiles import STANDARD
        # Only the standard profile runs turnarounds; never let another profile sort first.
        discovered = await defaults.discover(owner_id, needs_last_frame=True, only_profile=STANDARD)
        if discovered["selected"] is None:
            raise TurnaroundUnavailable("No machine supports turnarounds yet")
        values = defaults.settings_for(discovered["selected"])
        settings = MotionGenerationSettings.model_validate(
            {**values, "provider_parameters": {**values["provider_parameters"], "closed_loop": True}})
    elif settings.provider_parameters.get("closed_loop") is not True:
        raise ValueError("a turnaround is always a closed loop")
    if settings.backdrop is not Backdrop.CHROMA_GREEN:
        raise ValueError("a turnaround renders on the chroma green backdrop")
    if not settings.provider_parameters.get("worker_id") or settings.provider_parameters.get("execution_profile"):
        raise TurnaroundUnavailable("Turnarounds run only on a pinned standard machine")
    try:
        resolved = await defaults.resolve(owner_id, settings)
    except ValueError as exc:
        raise TurnaroundUnavailable(str(exc)) from exc
    # Judged on what resolution returned, not on optional caller fields: dispatch
    # always uses the standard capability and its 24 GB requirement.
    from outbound_gpu_worker_pool.machine_profiles import STANDARD, describe_profile
    parameters = resolved.provider_parameters
    if (parameters.get("execution_profile") or parameters.get("closed_loop") is not True
            or parameters.get("recipe_version") != describe_profile(STANDARD)["recipe_version"]):
        raise TurnaroundUnavailable("Turnarounds run only on a pinned standard machine")
    if resolved.backdrop is not Backdrop.CHROMA_GREEN:
        raise ValueError("a turnaround renders on the chroma green backdrop")
    return resolved


async def start(service, project_id: str, expected_revision: int, source_view_id: str,
                source_revision_id: str, equipment: tuple[TurnaroundEquipment, ...],
                actor_id: str, request_id: str, settings: MotionGenerationSettings | None = None
                ) -> tuple[TurnaroundTake, JobRecord]:
    """Start (or return) one turnaround.

    Order of writes: the ledger job first, then the take derived from that job's
    payload, then the pool submission. A retry of the same request returns the
    original whatever the project revision is now; a crash between the writes
    resumes from the queued job; a take that cannot be stored fails its job, so
    no step leaves a take without a job or a job running without a take.
    """
    from .store import RevisionConflict

    if service._jobs is None:
        raise RuntimeError("motion execution store is not configured")
    if not request_id or len(request_id) > 100:
        raise ValueError("turnaround request_id must contain 1 to 100 characters")
    current = await service._require_owned_project(project_id, actor_id)
    operation_id = sha256(f"{project_id}:turnaround:{source_view_id}:{source_revision_id}:{request_id}"
                          .encode()).hexdigest()[:32]
    turnaround_id = f"turnaround-{operation_id}"
    idempotency_key = f"{project_id}:turnaround:{operation_id}"
    existing = next((t for t in current.manifest.turnarounds if t.turnaround_id == turnaround_id), None)
    record = await _record_for(service, project_id, idempotency_key)
    if existing is not None:
        if record is None:
            raise ValueError("this turnaround request has no job; start a new request")
        if record.status is JobStatus.QUEUED:
            # A stop after storing the take but before the pool submission resumes here.
            await _ensure_pool_job(service, current.manifest.owner_id, record)
        return existing, (await service._jobs.get(record.job_id)) or record
    if record is not None and record.status is not JobStatus.QUEUED:
        raise ValueError("this turnaround request ended before its take was stored; start a new request")

    if record is None:
        current = await service._require_owned_project(project_id, actor_id, expected_revision)
        record = await _submit_ledger_job(service, current, source_view_id, source_revision_id, equipment,
                                          settings, operation_id, idempotency_key)
    payload = GenerateTurnaroundPayloadV1.model_validate(record.payload)
    take = _take_from_payload(payload, operation_id)
    while True:
        view = next((v for v in current.manifest.views if str(v.view_id) == payload.source_view_id), None)
        if view is None or view.reference is None or view.reference.revision_id != take.source_reference.revision_id:
            await _fail_unstored(service, record, "The source reference changed before the turnaround was stored.")
            raise ValueError("source revision is not the view's current bound reference")
        try:
            await service._store.append(project_id, current.revision, current.manifest.model_copy(
                update={"turnarounds": (*current.manifest.turnarounds, take)}))
            break
        except RevisionConflict:
            planned_at = current.revision
            current = await service._require_owned_project(project_id, actor_id)
            stored = next((t for t in current.manifest.turnarounds if t.turnaround_id == turnaround_id), None)
            if stored is not None:
                # An identical concurrent request stored the same take for the same job.
                take = stored
                break
            if planned_at == expected_revision and record.payload.get("requested_revision") == expected_revision:
                # Planned against this revision and it moved underneath: never store it late.
                await _fail_unstored(service, record, "The project changed before the turnaround was stored.")
                raise
    await _ensure_pool_job(service, current.manifest.owner_id, record)
    return take, (await service._jobs.get(record.job_id)) or record


async def _submit_ledger_job(service, current, source_view_id, source_revision_id, equipment, settings,
                             operation_id, idempotency_key) -> JobRecord:
    from .service import MAX_SUBJECT_REFERENCE_BYTES, _render_view_opening_frame

    owner_id = current.manifest.owner_id
    project_id = current.manifest.project_id
    turnaround_id = f"turnaround-{operation_id}"
    view, heading = _source_view(current.manifest, source_view_id)
    if view.reference is None or view.reference.revision_id != source_revision_id:
        raise ValueError("source revision is not the view's current bound reference")
    require_qualified_source(heading)
    if equipment:
        await service.prompt_screening.require_allowed(owner_id, "motion", [row.item for row in equipment])
    await service._require_headroom_to_start_work(owner_id)
    resolved = await _resolve_settings(service, owner_id, settings)
    await service._require_motion_compute(owner_id, resolved)
    content, source_width, source_height = await service._read_view_reference(
        current.manifest, view.view_id, view.reference.key, view.reference.sha256)
    backdrop = source_backdrop(content)
    prompt = turnaround_prompt(heading, equipment)
    frame = await asyncio.to_thread(_render_view_opening_frame, content, resolved, fill=1.0)
    frame_hash = sha256(frame).hexdigest()
    frame_key = f"outputs/studio/{project_id}/opening-frames/view-{frame_hash}.png"
    await service._assets.write_once(frame_key, frame, "image/png")
    # The key is content-addressed and written once, so an existing object is
    # kept as it is: dispatch only after its bytes are the frame rendered here.
    stored_frame = await service._assets.read_limited(frame_key, MAX_SUBJECT_REFERENCE_BYTES)
    if sha256(stored_frame).hexdigest() != frame_hash:
        raise TurnaroundUnavailable("The prepared turnaround frame failed its integrity check; nothing was started.")
    with Image.open(io.BytesIO(frame)) as opened:
        frame_width, frame_height = opened.size

    from outbound_gpu_worker_pool.machine_profiles import STANDARD, describe_profile
    payload = GenerateTurnaroundPayloadV1(
        requested_settings=settings.model_dump(mode="json", exclude_unset=True) if settings else {},
        resolved_recipe=describe_profile(STANDARD),
        turnaround_id=turnaround_id, prompt_version=PROMPT_VERSION, prompt=prompt,
        width=resolved.width, height=resolved.height, duration_seconds=resolved.duration_seconds,
        seed=resolved.seed, preset=resolved.preset.value, steps=resolved.steps,
        backdrop=resolved.backdrop.value,
        provider_parameters=resolved.provider_parameters, requested_revision=current.revision,
        source_view_id=str(view.view_id), source_heading=heading.value,
        source_reference=view.reference.model_dump(mode="json"), source_backdrop=backdrop,
        equipment=tuple(row.model_dump(mode="json") for row in equipment),
        start_frame_key=frame_key, start_frame_sha256=frame_hash,
        reference_preparation={"algorithm": PREPARATION_ALGORITHM, "fill": 1.0,
                               "source_width": source_width, "source_height": source_height,
                               "canvas_width": frame_width, "canvas_height": frame_height,
                               "backdrop": resolved.backdrop.value, "slots": list(_SLOTS)})
    output_key = f"outputs/studio/{project_id}/turnarounds/{turnaround_id}/{operation_id}.mp4"
    submitted = await service._jobs.submit(JobSubmission(
        job_id=str(uuid4()), idempotency_key=idempotency_key,
        operation=JobOperation.GENERATE_TURNAROUND, source_key=frame_key, output_key=output_key,
        requested_outputs={}, project_id=project_id, manifest_operation_id=operation_id,
        payload=payload.model_dump(mode="json")))
    return submitted.record


def _take_from_payload(payload: GenerateTurnaroundPayloadV1, operation_id: str) -> TurnaroundTake:
    """The take is derived from its job, so the two can never disagree."""
    from .models import ViewReference

    preparation = payload.reference_preparation
    return TurnaroundTake(
        turnaround_id=payload.turnaround_id, operation_id=operation_id,
        source_view_id=payload.source_view_id, source_heading=ViewId(payload.source_heading),
        source_reference=ViewReference.model_validate(payload.source_reference),
        source_backdrop=payload.source_backdrop,
        equipment=tuple(TurnaroundEquipment.model_validate(row) for row in payload.equipment),
        prompt=payload.prompt,
        prepared_frame=TurnaroundPreparedFrame(
            key=payload.start_frame_key, sha256=payload.start_frame_sha256,
            width=int(preparation["canvas_width"]), height=int(preparation["canvas_height"]),
            algorithm=str(preparation["algorithm"]), fill=float(preparation["fill"])),
        requested_revision=payload.requested_revision, created_at=datetime.now(UTC))


async def _fail_unstored(service, record: JobRecord, message: str) -> None:
    await service._jobs.fail_unclaimed(record.job_id, "TurnaroundNotStored", failure_code=JobFailureCode.INVALID_INPUT,
                                       failure_message=message, retryable=False)


async def _ensure_pool_job(service, owner_id: str, record: JobRecord) -> None:
    """Submit the pool job once. The pool replays an equal submission, so a race
    between identical requests or a resumed retry can never start a second one."""
    if await service._motion_pool.get(record.job_id) is not None:
        return
    await _submit_pool_job(service, owner_id, record, GenerateTurnaroundPayloadV1.model_validate(record.payload))


async def _submit_pool_job(service, owner_id: str, record: JobRecord, payload: GenerateTurnaroundPayloadV1) -> None:
    from .service import (
        MOTION_POOL_CAPABILITY_ID, MOTION_POOL_REQUIREMENTS, MotionGenerationUnavailable,
        _pinned_requirements, _pool_template_payload,
    )

    pool_payload, input_keys = _pool_template_payload(
        prompt=payload.prompt, width=payload.width, height=payload.height,
        duration_seconds=payload.duration_seconds, steps=payload.steps, seed=payload.seed,
        provider_parameters=payload.provider_parameters, start_frame_key=payload.start_frame_key)
    try:
        await service._motion_pool.submit(PoolJobSubmission(
            job_id=record.job_id, idempotency_key=record.idempotency_key,
            capability_id=MOTION_POOL_CAPABILITY_ID, input_keys=input_keys, output_key=record.output_key,
            payload=pool_payload, tenant_id=await service._tenant_for(owner_id),
            requirements=_pinned_requirements(MOTION_POOL_REQUIREMENTS, MOTION_POOL_CAPABILITY_ID,
                                              payload.provider_parameters)))
    except _DEFINITE_REFUSALS as exc:
        # Pinned work never falls back to another machine or the Cloud Task path.
        await service._jobs.fail_unclaimed(
            record.job_id, type(exc).__name__, failure_code=JobFailureCode.PROVIDER_UNAVAILABLE,
            failure_message="The selected machine could not accept this turnaround; no fallback was run.",
            retryable=False)
        raise MotionGenerationUnavailable(
            "The selected machine could not accept this turnaround; no fallback was run.") from exc
    except Exception as exc:
        # A timeout or lost connection may still have stored the pool job, and the
        # pinned worker may run it. Failing the row would orphan that work, so it
        # stays queued: the same request resumes it through get-then-submit, and
        # the pool replays an equal submission rather than starting a second job.
        logger.warning("turnaround %s: pool submission outcome unknown", record.job_id, exc_info=True)
        raise MotionGenerationUnavailable(
            "The machine did not confirm this turnaround; retry the same request to resume it.") from exc


async def _turnaround_records(service, project_id: str, stop_at: str | None = None) -> dict[str, JobRecord]:
    """Turnaround jobs by idempotency key, paging through the whole history (an
    unpaged listing is capped on PostgreSQL); stops early once `stop_at` is found."""
    from .service import job_activity_cursor
    found: dict[str, JobRecord] = {}
    if service._jobs is None:
        return found
    cursor = None
    while True:
        page = await service._jobs.list_for_project(project_id, cursor=cursor, limit=_PAGE)
        for record in page:
            if record.operation is JobOperation.GENERATE_TURNAROUND:
                found.setdefault(record.idempotency_key, record)
        if (stop_at is not None and stop_at in found) or len(page) < _PAGE:
            return found
        cursor = job_activity_cursor(page[-1])


async def _record_for(service, project_id: str, idempotency_key: str) -> JobRecord | None:
    return (await _turnaround_records(service, project_id, stop_at=idempotency_key)).get(idempotency_key)


def _effective_slots(execution_provenance: dict[str, Any]) -> Any:
    attestation = execution_provenance.get("worker_attestation") or {}
    for container in (attestation.get("diagnostics") or {}, attestation):
        effective = container.get("effective_inputs") if isinstance(container, dict) else None
        if isinstance(effective, dict) and "images" in effective:
            return effective["images"]
    return None


async def settle(service, job_id: str, actor_id: str):
    """Attach a completed turnaround's video to its take, once its prepared frame is attested."""
    from .service import (
        MAX_MOTION_OUTPUT_BYTES, MotionAssetError, MotionLineageConflict, MotionOperationNotReady,
        _MOTION_MEASUREMENT_LIMIT, inspect_motion_artifact,
    )

    job = await service._jobs.get(job_id)
    if (job is None or job.operation is not JobOperation.GENERATE_TURNAROUND
            or job.status is not JobStatus.COMPLETED or job.result is None or job.project_id is None):
        raise MotionOperationNotReady("turnaround operation is not completed")
    from .store import RevisionConflict
    current = await service._require_owned_project(job.project_id, actor_id)
    take = _take(current.manifest, job.payload.get("turnaround_id", ""))
    if take.candidate is not None:
        return await _record_take_bytes(service, current, take.turnaround_id, actor_id)
    if job.result.output_key != job.output_key or job.source_key != take.prepared_frame.key:
        raise MotionLineageConflict("completed turnaround differs from its request")
    slots = _effective_slots(job.result.execution_provenance or {})
    if slots != {slot: take.prepared_frame.key for slot in _SLOTS}:
        raise MotionLineageConflict("worker did not attest the prepared frame in both template slots")
    try:
        artifact = await service._assets.read_limited(job.result.output_key, MAX_MOTION_OUTPUT_BYTES)
    except (AssetNotFound, AssetTooLarge) as exc:
        raise MotionAssetError("motion_asset_missing", "completed turnaround artifact cannot be read") from exc
    try:
        async with _MOTION_MEASUREMENT_LIMIT:
            metadata = await asyncio.to_thread(inspect_motion_artifact, artifact)
    except ValueError as exc:
        raise MotionAssetError("motion_asset_undecodable", "completed turnaround artifact is not decodable") from exc
    candidate = MotionCandidate(
        candidate_id=take.operation_id, artifact_key=job.result.output_key,
        sha256=sha256(artifact).hexdigest(), execution_provenance=job.result.execution_provenance,
        duration_ms=round(metadata.duration_ms) if metadata.duration_ms is not None else None,
        metadata=metadata,
        generation_settings=MotionGenerationSettings(
            preset=job.payload["preset"], steps=job.payload["steps"], seed=job.payload["seed"],
            width=job.payload["width"], height=job.payload["height"],
            duration_seconds=job.payload["duration_seconds"], backdrop=job.payload["backdrop"],
            provider_parameters=job.payload["provider_parameters"]))
    while True:
        takes = tuple(replace_candidate(t, candidate) if t.turnaround_id == take.turnaround_id else t
                      for t in current.manifest.turnarounds)
        try:
            current = await service._store.append(job.project_id, current.revision,
                                                  current.manifest.model_copy(update={"turnarounds": takes}))
            break
        except RevisionConflict:
            current = await service._require_owned_project(job.project_id, actor_id)
            if _take(current.manifest, take.turnaround_id).candidate is not None:
                break
    return await _record_take_bytes(service, current, take.turnaround_id, actor_id)


async def _record_take_bytes(service, current, turnaround_id: str, actor_id: str):
    """Count a settled take's video once, then mark the take counted.

    The count is keyed by the artifact (`record_artifact`), so a retry after a
    lost response, a crash, or a concurrent settle adds nothing a second time.
    Until the marker is set `is_settled` stays false and reconciliation retries.
    """
    from .service import MotionAssetError
    from .store import RevisionConflict

    take = _take(current.manifest, turnaround_id)
    if take.stored_bytes_recorded:
        return current
    descriptor = await service._assets.describe(take.candidate.artifact_key)
    if descriptor is None:
        raise MotionAssetError("motion_asset_missing", "settled turnaround artifact cannot be described")
    await service._owner_usage.record_artifact(current.manifest.owner_id, take.candidate.artifact_key,
                                               descriptor.size)
    if await service._store.get(current.project_id) is None:
        # Deleted while this settled: the purge has already released its keys, so
        # give this one back too rather than charge the owner for a gone project.
        await service._owner_usage.release_artifact(current.manifest.owner_id, take.candidate.artifact_key)
        from .service import ProjectNotFound
        raise ProjectNotFound(current.project_id)
    while True:
        takes = tuple(t.model_copy(update={"stored_bytes_recorded": True}) if t.turnaround_id == turnaround_id
                      else t for t in current.manifest.turnarounds)
        try:
            return await service._store.append(current.project_id, current.revision,
                                               current.manifest.model_copy(update={"turnarounds": takes}))
        except RevisionConflict:
            current = await service._require_owned_project(current.project_id, actor_id)
            if _take(current.manifest, turnaround_id).stored_bytes_recorded:
                return current


def replace_candidate(take: TurnaroundTake, candidate: MotionCandidate) -> TurnaroundTake:
    return take.model_copy(update={"candidate": candidate})


def describe(take: TurnaroundTake, manifest, record: JobRecord | None) -> dict[str, Any]:
    """The take as callers see it: provenance, state, and whether its source is still current."""
    from .turnaround_cards import cards

    view = next((v for v in manifest.views if str(v.view_id) == take.source_view_id), None)
    current_reference = view.reference if view is not None else None
    return {
        **cards(take.source_heading, take.equipment),
        "pick_set": _describe_picks(take.pick_set),
        "turnaround_id": take.turnaround_id,
        "source_view_id": take.source_view_id,
        "source_heading": take.source_heading.value,
        "source_reference": take.source_reference.model_dump(mode="json"),
        "source_superseded": current_reference is None or current_reference.revision_id != take.source_reference.revision_id,
        "source_backdrop": take.source_backdrop,
        "equipment": [row.model_dump(mode="json") for row in take.equipment],
        "prompt_version": take.prompt_version,
        "prompt": take.prompt,
        "prompt_sha256": sha256(take.prompt.encode()).hexdigest(),
        "prepared_frame": take.prepared_frame.model_dump(mode="json"),
        "requested_revision": take.requested_revision,
        "created_at": take.created_at.isoformat() if take.created_at else None,
        "job_id": record.job_id if record else None,
        "status": record.status.value if record else None,
        "settled": take.candidate is not None,
        "frame_count": take.candidate.metadata.frame_count if take.candidate and take.candidate.metadata else None,
        "video": ({"key": take.candidate.artifact_key, "sha256": take.candidate.sha256,
                   "execution_provenance": take.candidate.execution_provenance}
                  if take.candidate else None),
    }


async def get(service, project_id: str, turnaround_id: str, actor_id: str) -> dict[str, Any]:
    """One take, with Studio's suggested frame per heading once it has settled."""
    current = await service._require_owned_project(project_id, actor_id)
    take = _take(current.manifest, turnaround_id)
    record = await _record_for(service, project_id, f"{project_id}:turnaround:{take.operation_id}")
    described = describe(take, current.manifest, record)
    described["suggested_picks"] = None
    if take.candidate is not None and take.candidate.metadata is not None:
        from .service import MotionAssetError, MotionDerivationError
        try:
            described["suggested_picks"] = (await _pick_analysis(service, project_id, take))["suggested_picks"]
        except (MotionAssetError, MotionDerivationError) as exc:
            # The take stays readable; picks can still be made by hand from the frames.
            described["suggestions_error"] = str(exc)
    return described


_SUGGESTION_REASONS = {
    "profile": "the body is narrowest here, a profile",
    "back": "the body is widest here, between the two profiles",
    "spaced": "evenly spaced between its neighbours",
}


def _reason(heading: ViewId) -> str:
    if heading in (ViewId.E, ViewId.W):
        return _SUGGESTION_REASONS["profile"]
    if heading is ViewId.N:
        return _SUGGESTION_REASONS["back"]
    return _SUGGESTION_REASONS["spaced"]


def _decode_frames(content: bytes) -> list[bytes]:
    """Every stored frame of the take, decoded natively."""
    import tempfile
    from pathlib import Path

    from magic_video_editor.plugins.ffmpeg import alpha_video_decoder_args

    with tempfile.TemporaryDirectory(prefix="studio-turnaround-frames-") as folder:
        path = Path(folder) / "source"
        path.write_bytes(content)
        subprocess.run(["ffmpeg", "-v", "error", *alpha_video_decoder_args(content), "-i", str(path),
                        "-map", "0:v:0", "-vsync", "0", str(Path(folder) / "%05d.png")],
                       check=True, capture_output=True, timeout=120)
        return [frame.read_bytes() for frame in sorted(Path(folder).glob("*.png"))]


def _measure_widths(content: bytes) -> list[float]:
    """Torso width per frame, keyed exactly as the picker's fixtures were."""
    from magic_video_editor.plugins.subject_pool import key_green_screen

    from .turnaround_picks import torso_width

    from magic_video_editor.plugins.subject_pool import TerminalSubjectProviderError

    widths = []
    for index, frame in enumerate(_decode_frames(content)):
        try:
            keyed_frame = key_green_screen(frame)
        except TerminalSubjectProviderError as exc:
            # Name the frame, so the person knows where the backdrop broke.
            raise TerminalSubjectProviderError(f"frame {index}: {exc}") from exc
        with Image.open(io.BytesIO(keyed_frame)) as keyed:
            widths.append(torso_width(np.asarray(keyed.getchannel("A")) > 127))
    return widths


async def _pick_analysis(service, project_id: str, take: TurnaroundTake) -> dict[str, Any]:
    """Suggestions for a settled take, measured once and cached beside it.

    A cached analysis is used only for the take it describes: same algorithm,
    candidate, video hash and source heading.
    """
    candidate = take.candidate
    key = (f"outputs/studio/{project_id}/turnarounds/{take.turnaround_id}/"
           f"picks-{PICKS_VERSION}-{candidate.sha256[:16]}.json")
    entry = _ANALYSIS_LOCKS.setdefault(key, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            return await _cached_or_measured(service, take, key)
    finally:
        # A released lock can look free while a woken waiter has yet to resume,
        # so count participants rather than asking the lock.
        entry[1] -= 1
        if not entry[1]:
            del _ANALYSIS_LOCKS[key]


async def _cached_or_measured(service, take: TurnaroundTake, key: str) -> dict[str, Any]:
    from magic_video_editor.plugins.subject_pool import TerminalSubjectProviderError

    from .service import (
        _MOTION_MEASUREMENT_LIMIT, MAX_MOTION_OUTPUT_BYTES, MotionAssetError, MotionDerivationError,
        SuggestionsUnavailable,
    )
    from .turnaround_picks import PickUnavailable, suggest

    candidate = take.candidate
    identity = {"algorithm": PICKS_VERSION, "candidate_id": candidate.candidate_id,
                "video_sha256": candidate.sha256, "source_heading": take.source_heading.value}
    try:
        if await service._assets.describe(key) is not None:
            cached = json.loads(await service._assets.read_limited(key, _ANALYSIS_LIMIT))
            if {name: cached.get(name) for name in identity} != identity:
                raise MotionDerivationError("frame_analysis_failed",
                                            "cached pick analysis does not match this turnaround's video")
            if cached.get("frame_count") != candidate.metadata.frame_count:
                raise MotionDerivationError("frame_analysis_failed",
                                            "cached pick analysis has a different frame count from this turnaround")
            return cached
        content = await service._assets.read_limited(candidate.artifact_key, MAX_MOTION_OUTPUT_BYTES)
        if sha256(content).hexdigest() != candidate.sha256:
            raise ValueError("turnaround asset digest does not match metadata")
        async with _MOTION_MEASUREMENT_LIMIT:
            widths = await asyncio.to_thread(_measure_widths, content)
        if len(widths) != candidate.metadata.frame_count:
            # Picks index the settled candidate's frames, as frame inspection does.
            raise ValueError(f"decoded {len(widths)} frames, but the turnaround recorded "
                             f"{candidate.metadata.frame_count}")
        picks = suggest(widths, take.source_heading)
    except AssetStorageUnavailable as exc:
        raise MotionAssetError("motion_asset_unavailable", "turnaround storage is unavailable") from exc
    except (AssetNotFound, AssetTooLarge) as exc:
        raise MotionAssetError("motion_asset_missing", "turnaround asset cannot be read") from exc
    except PickUnavailable as exc:
        raise SuggestionsUnavailable("frame_analysis_failed", f"no suggestions: {exc}") from exc
    except TerminalSubjectProviderError as exc:
        # A frame without a green-screen border cannot be keyed and measured.
        raise SuggestionsUnavailable("frame_analysis_failed", f"a frame could not be keyed: {exc}") from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise MotionDerivationError("frame_analysis_failed", str(exc)) from exc
    result = {**identity, "frame_count": len(widths), "torso_width": [round(w, 4) for w in widths],
              "suggested_picks": {h.value: {"frame": frame, "reason": "the source, where the loop starts and ends"
                                            if h is take.source_heading else _reason(h)}
                                  for h, frame in picks.items()}}
    try:
        await service._assets.write_once(key, json.dumps(result, separators=(",", ":")).encode(),
                                         "application/json")
    except AssetStorageUnavailable:
        # The suggestions stand; the next read measures again.
        logger.warning("could not cache the pick analysis at %s", key)
    return result


async def save_picks(service, project_id: str, turnaround_id: str, expected_pick_revision: int,
                     picks: tuple[TurnaroundPick, ...], actor_id: str) -> dict[str, Any]:
    """Replace the take's whole pick set, fenced by its pick revision."""
    from .service import MotionOperationNotReady, SuggestionsUnavailable
    from .store import RevisionConflict
    from .turnaround_picks import check_picks

    current = await service._require_owned_project(project_id, actor_id)
    take = _take(current.manifest, turnaround_id)
    # Fence first: a stale request gets its 409, never validation of an obsolete set.
    if (take.pick_set.revision if take.pick_set is not None else 0) != expected_pick_revision:
        raise PickRevisionConflict(take.pick_set.revision if take.pick_set is not None else 0)
    if take.candidate is None or take.candidate.metadata is None:
        raise MotionOperationNotReady("turnaround has not settled")
    try:
        analysis = await _pick_analysis(service, project_id, take)
        suggested = {ViewId(h): entry["frame"] for h, entry in analysis["suggested_picks"].items()}
        frame_count = analysis["frame_count"]
    except SuggestionsUnavailable:
        # No suggestions (an unkeyable frame, a take that barely turns): the person
        # picks by hand, confirming each pick, and turn order is still checked. Any
        # other analysis failure, such as a cache that fails its identity check, refuses.
        suggested, frame_count = None, take.candidate.metadata.frame_count
    picks = tuple(picks)
    findings = check_picks(picks, suggested, take.source_heading, frame_count)
    while True:
        take = _take(current.manifest, turnaround_id)
        revision = take.pick_set.revision if take.pick_set is not None else 0
        if revision != expected_pick_revision:
            raise PickRevisionConflict(revision)
        pick_set = TurnaroundPickSet(revision=revision + 1, picks=picks, findings=tuple(findings),
                                     saved_by=actor_id, saved_at=datetime.now(UTC))
        takes = tuple(t.model_copy(update={"pick_set": pick_set}) if t.turnaround_id == turnaround_id else t
                      for t in current.manifest.turnarounds)
        try:
            await service._store.append(project_id, current.revision,
                                        current.manifest.model_copy(update={"turnarounds": takes}))
            return _describe_picks(pick_set)
        except RevisionConflict:
            current = await service._require_owned_project(project_id, actor_id)


def _describe_picks(pick_set: TurnaroundPickSet | None) -> dict[str, Any] | None:
    if pick_set is None:
        return None
    return {"pick_revision": pick_set.revision,
            "picks": {p.heading.value: p.model_dump(mode="json", exclude={"heading"}) for p in pick_set.picks},
            "findings": list(pick_set.findings), "saved_by": pick_set.saved_by,
            "saved_at": pick_set.saved_at.isoformat()}


class PickRevisionConflict(Exception):
    """The pick set changed since the caller read it."""

    def __init__(self, current: int):
        super().__init__(f"the pick set is at revision {current}")
        self.current = current


async def list_takes(service, project_id: str, actor_id: str) -> list[dict[str, Any]]:
    current = await service._require_owned_project(project_id, actor_id)
    records = await _turnaround_records(service, project_id)
    return [describe(t, current.manifest, records.get(f"{project_id}:turnaround:{t.operation_id}"))
            for t in current.manifest.turnarounds]


async def frames(service, project_id: str, turnaround_id: str, actor_id: str) -> dict:
    """Native decoded frames of a settled take, cached like motion candidates."""
    from .frame_analysis import ANALYSIS_VERSION, analyze_candidate_frames
    from .service import MAX_MOTION_OUTPUT_BYTES, MotionAssetError, MotionDerivationError, MotionOperationNotReady

    current = await service._require_owned_project(project_id, actor_id)
    take = _take(current.manifest, turnaround_id)
    candidate = take.candidate
    if candidate is None or candidate.metadata is None:
        raise MotionOperationNotReady("turnaround has not settled")
    key = (f"outputs/studio/{project_id}/turnarounds/{turnaround_id}/"
           f"frames-{ANALYSIS_VERSION}-{candidate.sha256[:16]}.json")
    if await service._assets.describe(key) is not None:
        return _checked_analysis(json.loads(await service._assets.read_limited(key, _ANALYSIS_LIMIT)), candidate)
    try:
        content = await service._assets.read_limited(candidate.artifact_key, MAX_MOTION_OUTPUT_BYTES)
        if sha256(content).hexdigest() != candidate.sha256:
            raise ValueError("turnaround asset digest does not match metadata")
        result = await asyncio.to_thread(analyze_candidate_frames, content, candidate)
    except (AssetNotFound, AssetTooLarge) as exc:
        raise MotionAssetError("motion_asset_missing", "turnaround asset cannot be read") from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise MotionDerivationError("frame_analysis_failed", str(exc)) from exc
    encoded = json.dumps(result, separators=(",", ":")).encode()
    if len(encoded) > _ANALYSIS_LIMIT:
        raise MotionDerivationError("frame_analysis_failed", "frame previews exceed size budget")
    await service._assets.write_once(key, encoded, "application/json")
    return _checked_analysis(result, candidate)


def _checked_analysis(analysis: dict, candidate) -> dict:
    """A cached analysis is only used for the take it describes: same algorithm,
    same candidate, same video bytes. Anything else is refused, never served."""
    from .frame_analysis import ANALYSIS_VERSION
    from .service import MotionDerivationError

    if (analysis.get("analysis_version") != ANALYSIS_VERSION or analysis.get("candidate_id") != candidate.candidate_id
            or analysis.get("source_sha256") != candidate.sha256):
        raise MotionDerivationError("frame_analysis_failed",
                                    "cached analysis does not match this turnaround's video and algorithm")
    return analysis


async def cancel(service, project_id: str, turnaround_id: str, actor_id: str) -> JobRecord:
    current = await service._require_owned_project(project_id, actor_id)
    take = _take(current.manifest, turnaround_id)
    record = await _record_for(service, project_id, f"{project_id}:turnaround:{take.operation_id}")
    if record is None:
        raise ValueError("turnaround has no operation")
    if record.status in {JobStatus.QUEUED, JobStatus.PROCESSING}:
        if service._motion_pool is not None:
            try:
                await service._motion_pool.cancel(record.job_id)
            except Exception:
                pass
        await service._jobs.cancel(record.job_id)
    return (await service._jobs.get(record.job_id)) or record

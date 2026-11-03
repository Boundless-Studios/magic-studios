"""Atomic setup requests exercise the real service/store/asset boundaries."""

import io
from dataclasses import replace
from hashlib import sha256

import pytest
from PIL import Image
from pydantic import ValidationError

from magic_video_editor.contracts import JobOperation, JobRecord, JobStatus
from magic_video_editor.memory import MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import create_project_manifest, manifest_digest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ApprovalState,
    Backdrop,
    MotionCandidate,
    MotionSpecification,
    MotionState,
    SubjectRevision,
    SubjectRevisionSource,
)
from magic_video_editor.studio.service import (
    StudioService,
    _motion_prompt,
    motion_spec_digest,
)
from magic_video_editor.studio.setup import (
    ProjectSetup,
    SetupBlocked,
    SetupConfirmationRequired,
    SetupConflict,
)
from magic_video_editor.studio.store import RevisionConflict


async def project():
    store, assets, jobs = MemoryStudioStore(), MemoryAssetStore(), MemoryJobStore()
    buffer = io.BytesIO()
    Image.new("RGBA", (32, 48), (200, 100, 20, 255)).save(buffer, format="PNG")
    content = buffer.getvalue()
    key = "sources/studio/p/subjects/upload.png"
    await assets.write_once(key, content, "image/png")
    subject = SubjectRevision(
        revision_id="subject",
        artifact_key=key,
        sha256=sha256(content).hexdigest(),
        width=32,
        height=48,
        source=SubjectRevisionSource.UPLOADED,
    )
    manifest = create_project_manifest("p", "Hero", owner_id="actor").with_subject(
        subject
    )
    current = await store.create(manifest)
    return (
        StudioService(store=store, assets=assets, jobs=jobs),
        store,
        assets,
        jobs,
        current,
    )


def setup(views=None, layout="single_view"):
    return ProjectSetup.model_validate(
        {
            "sprite_layout": layout,
            "identity_reference": {"revision_id": "subject"},
            "views": views
            if views is not None
            else [
                {
                    "view_id": "single",
                    "heading": None,
                    "name": "Original view",
                    "reference": {"revision_id": "subject"},
                    "motions": [],
                }
            ],
        }
    )


async def apply(service, current, desired, confirm=True):
    plan = await service.preview_project_setup("p", current.revision, desired, "actor")
    result = await service.apply_project_setup(
        "p",
        current.revision,
        plan.plan_token,
        [i.impact_id for i in plan.impacts] if confirm else [],
        "actor",
    )
    return result, plan


async def test_one_uploaded_image_establishes_neutral_identity_and_opening_without_jobs():
    service, store, assets, jobs, current = await project()
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original view",
                "reference": {"revision_id": "subject"},
                "motions": [
                    {
                        "motion_id": "idle",
                        "name": "Idle",
                        "preset": "idle",
                        "prompt": "Breathe",
                    },
                    {"motion_id": "death", "name": "Death", "preset": "death"},
                ],
            }
        ]
    )
    result, _plan = await apply(service, current, desired)
    assert result.revision == current.revision + 1
    assert (
        result.manifest.identity_reference.key == result.manifest.views[0].reference.key
    )
    assert result.manifest.subject.approval.state is ApprovalState.APPROVED
    assert result.manifest.views[0].heading is None
    assert len([key for key in assets.assets if key.endswith(".png")]) == 1
    assert await jobs.list_for_project("p") == ()
    prompt = _motion_prompt(
        result.manifest.motions[0].specification, Backdrop.CHROMA_GREEN
    )
    assert "South" not in prompt and "heading S:" not in prompt
    assert "reference-relative" in prompt
    assert (await service.get_project_setup("p", "actor")).views[0].heading is None
    reloaded = await store.get("p")
    assert reloaded.manifest == result.manifest


async def test_exact_partial_thirteen_matrix_and_zero_motion_drafts():
    service, _, _, _, current = await project()
    headings = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    views = []
    for h in headings:
        motions = [{"motion_id": f"walk-{h.lower()}", "name": "Walk", "preset": "walk"}]
        if h in ["N", "E", "S", "W"]:
            motions.append(
                {
                    "motion_id": f"attack-{h.lower()}",
                    "name": "Attack",
                    "preset": "attack",
                }
            )
        if h == "S":
            motions.append({"motion_id": "idle-s", "name": "Idle", "preset": "idle"})
        views.append({"view_id": h, "heading": h, "name": h, "motions": motions})
    result, _ = await apply(service, current, setup(views, "isometric"))
    assert len(result.manifest.motions) == 13
    assert all(m.specification.preset != "death" for m in result.manifest.motions)
    assert sum(v.reference is not None for v in result.manifest.views) == 0


async def test_foreign_hash_stale_actor_and_revision_plans_fail_without_mutation():
    service, store, assets, _, current = await project()
    with pytest.raises(SetupConflict, match="belong"):
        await service.preview_project_setup(
            "p",
            1,
            setup(
                [
                    {
                        "view_id": "single",
                        "name": "Original",
                        "reference": {"revision_id": "foreign"},
                    }
                ]
            ),
            "actor",
        )
    plan = await service.preview_project_setup("p", 1, setup(), "actor")
    from magic_video_editor.studio.service import ProjectNotFound

    with pytest.raises(ProjectNotFound):
        await service.apply_project_setup("p", 1, plan.plan_token, [], "other")
    assets.assets[current.manifest.subject.current_revision.artifact_key] = b"wrong"
    with pytest.raises(SetupConflict, match="hash"):
        await service.apply_project_setup("p", 1, plan.plan_token, [], "actor")
    assert (await store.get("p")).revision == 1
    service, store, _, _, current = await project()
    plan = await service.preview_project_setup("p", 1, setup(), "actor")
    await store.append("p", 1, current.manifest)
    with pytest.raises(RevisionConflict):
        await service.apply_project_setup("p", 1, plan.plan_token, [], "actor")


async def test_destructive_scope_history_reheading_and_removed_identity_fence():
    service, store, _, _, current = await project()
    initial = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "reference": {"revision_id": "subject"},
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, initial)
    spec = current.manifest.motions[0].specification
    candidate = MotionCandidate(
        candidate_id="take",
        artifact_key="outputs/take.png",
        sha256="a" * 64,
        motion_spec_digest=motion_spec_digest(spec),
    )
    state = MotionState(
        specification=spec, candidates=(candidate,), selected_candidate_id="take"
    )
    current = await store.append(
        "p", current.revision, current.manifest.model_copy(update={"motions": (state,)})
    )
    old_digest = manifest_digest(current.manifest)
    desired = setup(
        [
            {
                "view_id": "single",
                "heading": "N",
                "name": "North",
                "reference": {"revision_id": "subject"},
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ],
        "isometric",
    )
    plan = await service.preview_project_setup("p", current.revision, desired, "actor")
    assert any(
        i.kind == "change_motion" and i.candidate_count == 1 for i in plan.impacts
    )
    with pytest.raises(SetupConfirmationRequired):
        await service.apply_project_setup(
            "p", current.revision, plan.plan_token, [], "actor"
        )
    result, _ = await apply(service, current, desired)
    assert result.manifest.motions[0].selected_candidate_id == "take"
    assert (
        motion_spec_digest(result.manifest.motions[0].specification)
        != candidate.motion_spec_digest
    )
    assert (
        manifest_digest(
            next(
                r
                for r in await store.list_revisions("p")
                if r.revision == current.revision
            ).manifest
        )
        == old_digest
    )
    deletion = setup([], "isometric")
    plan = await service.preview_project_setup("p", result.revision, deletion, "actor")
    assert {
        (i.kind, i.candidate_count, i.revision_count, i.reference_count)
        for i in plan.impacts
    } == {
        ("remove_motion", 1, 0, 0),
        ("remove_view", 0, 0, 1),
    }
    removed, _ = await apply(service, result, deletion)
    assert removed.manifest.motions == () and removed.manifest.views == ()
    with pytest.raises(SetupConflict, match="cannot be reused"):
        await service.preview_project_setup("p", removed.revision, desired, "actor")


async def test_active_and_unsettled_jobs_block_and_empty_removal_needs_no_confirmation():
    service, _store, _, jobs, current = await project()
    current, _ = await apply(
        service,
        current,
        setup(
            [
                {
                    "view_id": "single",
                    "name": "Original",
                    "motions": [
                        {"motion_id": "idle", "name": "Idle", "preset": "idle"}
                    ],
                }
            ]
        ),
    )
    job = JobRecord(
        job_id="pending",
        idempotency_key="k",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.QUEUED,
        source_key="s",
        output_key="o",
        attempts=0,
        project_id="p",
        manifest_operation_id="take",
        payload={"motion_id": "idle"},
    )
    jobs.records[job.job_id] = job
    plan = await service.preview_project_setup(
        "p", current.revision, setup([]), "actor"
    )
    assert plan.blocked_operations == ("pending",)
    with pytest.raises(SetupBlocked):
        await service.apply_project_setup(
            "p", current.revision, plan.plan_token, [], "actor"
        )
    jobs.records[job.job_id] = replace(job, status=JobStatus.COMPLETED)
    with pytest.raises(SetupBlocked):
        await service.apply_project_setup(
            "p", current.revision, plan.plan_token, [], "actor"
        )
    jobs.records[job.job_id] = replace(job, status=JobStatus.CANCELLED)
    result, _ = await apply(service, current, setup([]), confirm=False)
    assert result.manifest.views == ()


def test_heading_collision_and_mixed_convention_rejected():
    with pytest.raises(ValidationError, match="unique"):
        setup(
            [
                {"view_id": "a", "heading": "N", "name": "a"},
                {"view_id": "b", "heading": "N", "name": "b"},
            ],
            "isometric",
        )
    with pytest.raises(ValidationError, match="layout"):
        setup([{"view_id": "a", "heading": "left", "name": "a"}], "isometric")


async def test_staged_upload_is_project_scoped_and_does_not_mutate_identity_before_apply():
    service, store, assets, jobs, current = await project()
    content = assets.assets[current.manifest.subject.current_revision.artifact_key]
    staged = await service.stage_setup_reference("p", content, "image/png", "actor")
    assert (await store.get("p")).revision == current.revision
    desired = ProjectSetup.model_validate(
        {
            "sprite_layout": "single_view",
            "identity_reference": {"revision_id": staged.revision_id},
            "views": [
                {
                    "view_id": "single",
                    "name": "Original",
                    "reference": {"revision_id": staged.revision_id},
                    "motions": [],
                }
            ],
        }
    )
    current, _ = await apply(service, current, desired)
    assert current.manifest.subject.current_revision.revision_id == staged.revision_id
    assert current.manifest.identity_reference.revision_id == staged.revision_id
    second = await service.stage_setup_reference("p", content, "image/png", "actor")
    replacement = desired.model_copy(
        update={
            "views": (
                desired.views[0].model_copy(
                    update={
                        "reference": type(desired.identity_reference)(
                            revision_id=second.revision_id
                        )
                    }
                ),
            )
        }
    )
    result, _ = await apply(service, current, replacement)
    assert result.manifest.identity_reference == current.manifest.identity_reference
    assert result.manifest.views[0].reference.revision_id == second.revision_id
    assert (
        result.manifest.subject.current_revision
        == current.manifest.subject.current_revision
    )
    assert await jobs.list_for_project("p") == ()


async def test_removed_motion_late_completion_cannot_resurrect_or_reuse_identity():
    from magic_video_editor.contracts import VideoJobResult
    from magic_video_editor.studio.service import MotionOperationNotReady

    service, store, _, jobs, current = await project()
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, desired)
    original_revision = current.revision
    removed, _ = await apply(service, current, setup([]))
    jobs.records["late"] = JobRecord(
        job_id="late",
        idempotency_key="late",
        operation=JobOperation.GENERATE_MOTION,
        status=JobStatus.COMPLETED,
        source_key="source",
        output_key="output",
        attempts=1,
        project_id="p",
        manifest_operation_id="take",
        payload={
            "requested_revision": original_revision,
            "motion_id": "idle",
            "subject_key": "source",
            "subject_artifact_key": "source",
        },
        result=VideoJobResult(output_key="output"),
    )
    with pytest.raises(MotionOperationNotReady, match="no longer exists"):
        await service.settle_motion_candidate("late", "actor")
    assert (await store.get("p")).revision == removed.revision
    assert (await store.get("p")).manifest.motions == ()
    with pytest.raises(ValueError, match="cannot be reused"):
        await service.add_motion(
            "p",
            removed.revision,
            MotionSpecification(motion_id="idle", name="Idle", preset="idle"),
            "actor",
        )


async def test_staged_first_art_creates_subject_only_when_applied():
    service, store, assets, _jobs, current = await project()
    content = assets.assets[current.manifest.subject.current_revision.artifact_key]
    empty = create_project_manifest("empty", "New", owner_id="actor")
    empty = await store.create(empty)
    staged = await service.stage_setup_reference("empty", content, "image/png", "actor")
    assert (await store.get("empty")).manifest.subject.current_revision is None
    desired = ProjectSetup.model_validate(
        {
            "sprite_layout": "single_view",
            "identity_reference": {"revision_id": staged.revision_id},
            "views": [
                {
                    "view_id": "single",
                    "name": "Original",
                    "reference": {"revision_id": staged.revision_id},
                }
            ],
        }
    )
    plan = await service.preview_project_setup(
        "empty", empty.revision, desired, "actor"
    )
    saved = await service.apply_project_setup(
        "empty", empty.revision, plan.plan_token, [], "actor"
    )
    assert (
        saved.manifest.subject.current_revision.artifact_key
        == saved.manifest.views[0].reference.key
    )
    assert saved.manifest.subject.approval.state is ApprovalState.APPROVED
    with pytest.raises(SetupConflict, match="belong"):
        await service.preview_project_setup("p", current.revision, desired, "actor")


async def test_legacy_unassigned_get_preview_apply_preserves_bytes_and_motions():
    service, _store, _, _, current = await project()
    current = await service.add_motion(
        "p",
        current.revision,
        MotionSpecification(motion_id="idle", name="Idle", preset="idle"),
        "actor",
    )
    before = current.manifest.model_dump_json(exclude_none=True)
    desired = await service.get_project_setup("p", "actor")
    plan = await service.preview_project_setup("p", current.revision, desired, "actor")
    assert plan.impacts == ()
    after = await service.apply_project_setup(
        "p", current.revision, plan.plan_token, [], "actor"
    )
    assert after.revision == current.revision
    assert after.manifest.model_dump_json(exclude_none=True) == before


async def test_export_retains_stable_identity_and_declared_heading_and_rejects_target():
    from magic_video_editor.studio.exports import (
        GAIA_V1,
        ReleaseExporter,
        _package_manifest,
    )
    from magic_video_editor.studio.models import CharacterView, SpriteLayout, ViewId

    from .test_releases import releasable_manifest

    base = releasable_manifest()
    store = MemoryStudioStore()
    saved = await store.create(base)
    assets = MemoryAssetStore()
    service = StudioService(store=store, assets=assets)
    release = await service.create_release("project-1", saved.revision, "v1", "user-1")
    spec = base.motions[0].specification.model_copy(
        update={"view_id": "single", "view_heading": ViewId.E, "setup_version": 1}
    )
    # Validate the serialized snapshot as the supported package path does.
    spec = MotionSpecification.model_validate(spec.model_dump())
    motion = base.motions[0].model_copy(update={"specification": spec})
    view = CharacterView(view_id="single", heading="E", setup_version=1, name="East")
    manifest = base.model_copy(
        update={
            "sprite_layout": SpriteLayout.ISOMETRIC,
            "views": (view,),
            "motions": (motion,),
        }
    )
    release = release.model_copy(update={"manifest": manifest})
    package = _package_manifest(release, {motion.motion_id: {}})
    assert package["views"][0]["view_id"] == "single"
    assert package["views"][0]["heading"] == "E"
    assert package["motions"][0]["view_id"] == "single"
    assert package["motions"][0]["view_heading"] == "E"
    with pytest.raises(ValueError, match="does not support view-scoped"):
        await ReleaseExporter(assets).export(release, profile=GAIA_V1)


async def test_legacy_reference_adapter_preserves_stable_heading_metadata():
    from magic_video_editor.studio.models import ViewId

    service, _, _, _, current = await project()
    desired = setup(
        [{"view_id": "N", "heading": "E", "name": "East", "motions": []}], "isometric"
    )
    current, _ = await apply(service, current, desired)
    renamed = await service.set_character_view(
        "p", current.revision, ViewId.N, "actor", name="Renamed"
    )
    assert renamed.manifest.views[0].heading is ViewId.E
    assert renamed.manifest.views[0].setup_version == 1


async def test_plan_survives_service_replica_restart():
    service, store, assets, jobs, current = await project()
    plan = await service.preview_project_setup("p", current.revision, setup(), "actor")
    replica = StudioService(store=store, assets=assets, jobs=jobs)
    saved = await replica.apply_project_setup(
        "p", current.revision, plan.plan_token, [], "actor"
    )
    assert saved.manifest.views[0].view_id == "single"


async def test_generation_preparation_racing_removal_cannot_submit(monkeypatch):
    import asyncio

    from magic_video_editor.studio.service import MotionRequestBlocked

    from .test_motion_references import _Dispatcher

    service, store, assets, jobs, current = await project()
    service._dispatcher = _Dispatcher()
    service._motion_generation_available = True
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "reference": {"revision_id": "subject"},
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, desired)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = assets.write_once

    async def paused(key, content, content_type):
        if "/opening-frames/" in key:
            entered.set()
            await resume.wait()
        return await original(key, content, content_type)

    monkeypatch.setattr(assets, "write_once", paused)
    pending = asyncio.create_task(
        service.generate_motion("p", current.revision, "idle", "actor")
    )
    await entered.wait()
    await apply(service, current, setup([]))
    resume.set()
    with pytest.raises(MotionRequestBlocked, match="changed before dispatch"):
        await pending
    assert await jobs.list_for_project("p") == ()
    assert (await store.get("p")).manifest.motions == ()


async def test_durable_dispatch_reservation_blocks_other_replica_and_batch_queues_all(
    monkeypatch,
):
    import asyncio

    from .test_motion_references import _Dispatcher

    service, store, assets, jobs, current = await project()
    service._dispatcher = _Dispatcher()
    service._motion_generation_available = True
    service._owner_limits = service._owner_limits.model_copy(
        update={"concurrent_job_limit": 0}
    )
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "reference": {"revision_id": "subject"},
                "motions": [
                    {"motion_id": p, "name": p.title(), "preset": p}
                    for p in ("idle", "death")
                ],
            }
        ]
    )
    current, _ = await apply(service, current, desired)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = jobs.submit

    async def paused(submission):
        entered.set()
        await resume.wait()
        return await original(submission)

    monkeypatch.setattr(jobs, "submit", paused)
    pending = asyncio.create_task(
        service.generate_motions(
            "p", current.revision, ("idle", "death"), "actor", "batch"
        )
    )
    await entered.wait()
    replica = StudioService(store=store, assets=assets, jobs=jobs)
    reserved = await store.get("p")
    plan = await replica.preview_project_setup(
        "p", reserved.revision, setup([]), "actor"
    )
    assert plan.blocked_operations[0].startswith("dispatch:")
    assert "idle" in plan.blocked_operations[0]
    with pytest.raises(SetupBlocked):
        await replica.apply_project_setup(
            "p", reserved.revision, plan.plan_token, [], "actor"
        )
    resume.set()
    queued, failed = await pending
    assert len(queued) == 2 and failed == ()
    records = await jobs.list_for_project("p")
    assert {r.payload["motion_id"] for r in records} == {"idle", "death"}
    assert {r.payload["requested_revision"] for r in records} == {current.revision}


async def test_failed_job_insertion_releases_definite_reservation(monkeypatch):
    from .test_motion_references import _Dispatcher

    service, store, _assets, jobs, current = await project()
    service._dispatcher = _Dispatcher()
    service._motion_generation_available = True
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "reference": {"revision_id": "subject"},
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, desired)

    async def unavailable(_submission):
        raise ConnectionError("definite insertion failure")

    monkeypatch.setattr(jobs, "submit", unavailable)
    with pytest.raises(ConnectionError):
        await service.generate_motion("p", current.revision, "idle", "actor")
    latest = await store.get("p")
    assert latest.manifest.dispatch_reservations == ()
    plan = await service.preview_project_setup("p", latest.revision, setup([]), "actor")
    assert plan.blocked_operations == ()


async def test_explicit_abandon_fences_inflight_dispatch_before_enqueue(monkeypatch):
    import asyncio

    from magic_video_editor.studio.service import MotionRequestBlocked

    from .test_motion_references import _Dispatcher

    service, store, _assets, jobs, current = await project()
    service._dispatcher = _Dispatcher()
    service._motion_generation_available = True
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "reference": {"revision_id": "subject"},
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, desired)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = jobs.submit

    async def paused(submission):
        entered.set()
        await resume.wait()
        return await original(submission)

    monkeypatch.setattr(jobs, "submit", paused)
    pending = asyncio.create_task(
        service.generate_motion("p", current.revision, "idle", "actor", "abandoned")
    )
    await entered.wait()
    latest = await store.get("p")
    operation_id = latest.manifest.dispatch_reservations[0].operation_id
    with pytest.raises(SetupConfirmationRequired):
        await service.abandon_motion_dispatch(
            "p", latest.revision, operation_id, "actor"
        )
    abandoned = await service.abandon_motion_dispatch(
        "p", latest.revision, operation_id, "actor", confirm_dispatch_stopped=True
    )
    assert operation_id in abandoned.manifest.abandoned_dispatches
    resume.set()
    with pytest.raises(MotionRequestBlocked, match="abandoned"):
        await pending
    assert service._dispatcher.job_ids == []
    assert (await jobs.list_for_project("p"))[0].status is JobStatus.CANCELLED
    latest = await store.get("p")
    with pytest.raises(MotionRequestBlocked, match="abandoned"):
        await service.generate_motion(
            "p", latest.revision, "idle", "actor", "abandoned"
        )
    plan = await service.preview_project_setup("p", latest.revision, setup([]), "actor")
    assert plan.blocked_operations == ()


async def test_manual_upload_is_fresh_for_current_setup_spec():
    service, _store, assets, _jobs, current = await project()
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "reference": {"revision_id": "subject"},
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, desired)
    content = io.BytesIO()
    Image.new("RGBA", (128, 32), (255, 0, 0, 255)).save(content, format="PNG")
    key = "sources/studio/p/motion-uploads/manual.png"
    await assets.write_once(key, content.getvalue(), "image/png")
    settled = await service.settle_candidate_upload(
        "p", current.revision, "idle", key, "actor", frame_count=4
    )
    motion = settled.manifest.motions[0]
    assert motion.candidates[0].motion_spec_digest == motion_spec_digest(
        motion.specification
    )


async def test_failed_overlapping_retry_tombstones_shared_dispatch_before_enqueue(
    monkeypatch,
):
    import asyncio

    from magic_video_editor.studio.service import MotionRequestBlocked

    from .test_motion_references import _Dispatcher

    service, store, _assets, jobs, current = await project()
    service._dispatcher = _Dispatcher()
    service._motion_generation_available = True
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "reference": {"revision_id": "subject"},
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, desired)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = jobs.submit
    attempts = 0

    async def overlapping(submission):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await resume.wait()
            return await original(submission)
        raise ConnectionError("overlapping retry insertion failed")

    monkeypatch.setattr(jobs, "submit", overlapping)
    first = asyncio.create_task(
        service.generate_motion("p", current.revision, "idle", "actor", "same")
    )
    await entered.wait()
    reserved = await store.get("p")
    with pytest.raises(ConnectionError):
        await service.generate_motion("p", reserved.revision, "idle", "actor", "same")
    cleaned = await store.get("p")
    assert cleaned.manifest.dispatch_reservations == ()
    assert cleaned.manifest.abandoned_dispatches
    await apply(service, cleaned, setup([]))
    resume.set()
    with pytest.raises(MotionRequestBlocked, match="abandoned"):
        await first
    assert service._dispatcher.job_ids == []
    assert (await jobs.list_for_project("p"))[0].status is JobStatus.CANCELLED


async def test_east_job_blocks_only_affected_direction_edits():
    from magic_video_editor.studio.setup import SetupView

    from .test_motion_references import _Dispatcher

    service, store, _assets, _jobs, current = await project()
    service._dispatcher = _Dispatcher()
    service._motion_generation_available = True
    desired = setup(
        [
            {
                "view_id": "original",
                "heading": "E",
                "name": "East",
                "reference": {"revision_id": "subject"},
                "motions": [
                    {"motion_id": "idle-e", "name": "Idle East", "preset": "idle"}
                ],
            }
        ],
        "isometric",
    )
    current, _ = await apply(service, current, desired)
    job = await service.generate_motion("p", current.revision, "idle-e", "actor")
    current = await store.get("p")
    before = await service.get_project_setup("p", "actor")
    north = SetupView(
        view_id="north",
        heading="N",
        name="North",
        motions=(
            MotionSpecification(motion_id="idle-n", name="Idle North", preset="idle"),
        ),
    )
    addition = before.model_copy(update={"views": (*before.views, north)})
    plan = await service.preview_project_setup("p", current.revision, addition, "actor")
    assert plan.blocked_operations == ()
    current, _ = await apply(service, current, addition)
    before = await service.get_project_setup("p", "actor")
    north = before.views[1].model_copy(
        update={
            "motions": (
                before.views[1]
                .motions[0]
                .model_copy(update={"name": "Idle North Revised"}),
            )
        }
    )
    edited = before.model_copy(update={"views": (before.views[0], north)})
    current, plan = await apply(service, current, edited)
    assert plan.blocked_operations == ()
    removal = edited.model_copy(update={"views": (north,)})
    plan = await service.preview_project_setup("p", current.revision, removal, "actor")
    assert plan.blocked_operations == (job.job_id,)
    with pytest.raises(SetupBlocked):
        await service.apply_project_setup(
            "p",
            current.revision,
            plan.plan_token,
            [i.impact_id for i in plan.impacts],
            "actor",
        )


async def test_removal_counts_original_takes_and_saved_revisions_without_double_count():
    from magic_video_editor.studio.models import CandidateSource, DerivationRecipe

    service, store, _assets, _jobs, current = await project()
    desired = setup(
        [
            {
                "view_id": "single",
                "name": "Original",
                "motions": [{"motion_id": "idle", "name": "Idle", "preset": "idle"}],
            }
        ]
    )
    current, _ = await apply(service, current, desired)
    original = MotionCandidate(
        candidate_id="manual",
        artifact_key="sources/manual.png",
        sha256="a" * 64,
        source=CandidateSource.UPLOAD,
    )
    edited = MotionCandidate(
        candidate_id="frame-edit",
        artifact_key="sources/edit.png",
        sha256="b" * 64,
        parent_revision_ids=("manual",),
        recipe=DerivationRecipe(op="normalize"),
        source=CandidateSource.DERIVED,
    )
    motion = current.manifest.motions[0].model_copy(
        update={"candidates": (original, edited)}
    )
    current = await store.append(
        "p",
        current.revision,
        current.manifest.model_copy(update={"motions": (motion,)}),
    )
    plan = await service.preview_project_setup(
        "p", current.revision, setup([]), "actor"
    )
    impact = next(i for i in plan.impacts if i.kind == "remove_motion")
    assert (impact.candidate_count, impact.revision_count, impact.reference_count) == (
        1,
        1,
        0,
    )
    assert impact.requires_confirmation
    # Recipe-less frame edits still carry immutable parent lineage.
    revised = edited.model_copy(update={"recipe": None})
    motion = motion.model_copy(update={"candidates": (revised,)})
    current = await store.append(
        "p",
        current.revision,
        current.manifest.model_copy(update={"motions": (motion,)}),
    )
    plan = await service.preview_project_setup(
        "p", current.revision, setup([]), "actor"
    )
    impact = next(i for i in plan.impacts if i.kind == "remove_motion")
    assert (impact.candidate_count, impact.revision_count) == (0, 1)
    assert impact.requires_confirmation


async def test_legacy_named_setup_noop_and_one_reference_edit_preserve_unaffected_specs():
    from magic_video_editor.studio.manifest import approve_subject
    from magic_video_editor.studio.models import (
        Approval,
        CharacterView,
        SpriteLayout,
        ViewId,
        ViewReference,
    )

    service, store, assets, _jobs, current = await project()
    subject = current.manifest.subject.current_revision
    views = tuple(
        CharacterView(
            view_id=h,
            name=str(h),
            reference=ViewReference(
                revision_id=f"legacy-{h}",
                key=subject.artifact_key,
                sha256=subject.sha256,
                width=subject.width,
                height=subject.height,
            ),
        )
        for h in (ViewId.N, ViewId.S)
    )
    legacy = approve_subject(
        current.manifest,
        revision_id=subject.revision_id,
        assessment_id="legacy-assessment",
        approver_id="actor",
        policy_version="legacy-policy",
    )
    current = await store.append(
        "p",
        current.revision,
        legacy.model_copy(
            update={"sprite_layout": SpriteLayout.ISOMETRIC, "views": views}
        ),
    )
    for heading in (ViewId.N, ViewId.S):
        current = await service.add_motion(
            "p",
            current.revision,
            MotionSpecification(
                motion_id=f"idle-{heading.lower()}",
                name=f"Idle {heading}",
                preset="idle",
                view_id=heading,
            ),
            "actor",
        )
    motions = tuple(
        m.model_copy(
            update={
                "candidates": (
                    MotionCandidate(
                        candidate_id=f"take-{m.motion_id}",
                        artifact_key=f"outputs/{m.motion_id}.png",
                        sha256="a" * 64,
                        motion_spec_digest=motion_spec_digest(m.specification),
                    ),
                ),
                "selected_candidate_id": f"take-{m.motion_id}",
            }
        )
        for m in current.manifest.motions
    )
    current = await store.append(
        "p",
        current.revision,
        current.manifest.model_copy(
            update={
                "motions": motions,
                "set_approval": Approval(
                    state=ApprovalState.APPROVED,
                    assessment_id="set-assessment",
                    approver_id="actor",
                    policy_version="set-policy",
                ),
            }
        ),
    )
    encoded = current.manifest.model_dump_json(exclude_none=True)
    before_subject_approval = current.manifest.subject.approval
    desired = await service.get_project_setup("p", "actor")
    plan = await service.preview_project_setup("p", current.revision, desired, "actor")
    assert plan.impacts == ()
    unchanged = await service.apply_project_setup(
        "p", current.revision, plan.plan_token, [], "actor"
    )
    assert unchanged.revision == current.revision
    assert unchanged.manifest.model_dump_json(exclude_none=True) == encoded
    assert unchanged.manifest.subject.approval == before_subject_approval
    staged = await service.stage_setup_reference(
        "p", assets.assets[subject.artifact_key], "image/png", "actor"
    )
    replaced_north = desired.views[0].model_copy(
        update={
            "reference": type(desired.identity_reference)(
                revision_id=staged.revision_id
            )
        }
    )
    replacement = desired.model_copy(
        update={"views": (replaced_north, desired.views[1])}
    )
    changed, plan = await apply(service, current, replacement)
    assert not any(i.motion_id == "idle-s" for i in plan.impacts)
    assert changed.manifest.motions[1] == current.manifest.motions[1]
    assert motion_spec_digest(
        changed.manifest.motions[1].specification
    ) == motion_spec_digest(current.manifest.motions[1].specification)
    assert changed.manifest.views[1] == current.manifest.views[1]
    assert changed.manifest.identity_reference is None
    assert changed.manifest.subject.approval == before_subject_approval


async def test_staged_identity_manual_idle_death_quality_approval_release_and_export():
    import json
    import zipfile

    from PIL import ImageDraw

    from magic_video_editor.studio.models import FrameSlot, LoopPolicy

    service, store, assets, _jobs, fixture = await project()
    artwork = assets.assets[fixture.manifest.subject.current_revision.artifact_key]
    current = await store.create(
        create_project_manifest("release-path", "Hero", owner_id="actor")
    )
    staged = await service.stage_setup_reference(
        "release-path", artwork, "image/png", "actor"
    )
    desired = ProjectSetup.model_validate(
        {
            "sprite_layout": "single_view",
            "identity_reference": {"revision_id": staged.revision_id},
            "views": [
                {
                    "view_id": "single",
                    "name": "Original view",
                    "reference": {"revision_id": staged.revision_id},
                    "motions": [
                        {"motion_id": preset, "name": preset.title(), "preset": preset}
                        for preset in ("idle", "death")
                    ],
                }
            ],
        }
    )
    plan = await service.preview_project_setup(
        "release-path", current.revision, desired, "actor"
    )
    current = await service.apply_project_setup(
        "release-path", current.revision, plan.plan_token, [], "actor"
    )
    approval = current.manifest.subject.approval
    assert (
        approval.assessment_id
        and approval.approver_id == "actor"
        and approval.policy_version == "setup-identity-v1"
    )
    assert current.manifest.subject.assessment.artifact_sha256 == staged.sha256
    strip = Image.new("RGBA", (128, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(strip)
    for index in range(4):
        draw.rectangle(
            (index * 32 + 8, 4, index * 32 + 24, 28), fill=(255, 100, 20, 255)
        )
    buffer = io.BytesIO()
    strip.save(buffer, format="PNG")
    for motion_id in ("idle", "death"):
        key = f"sources/studio/release-path/motion-uploads/{motion_id}.png"
        await assets.write_once(key, buffer.getvalue(), "image/png")
        current = await service.settle_candidate_upload(
            "release-path", current.revision, motion_id, key, "actor", frame_count=4
        )
        source = next(
            m for m in current.manifest.motions if m.motion_id == motion_id
        ).candidates[0]
        current = await service.create_motion_revision(
            "release-path",
            current.revision,
            motion_id,
            source.candidate_id,
            "actor",
            ordered_frames=tuple(FrameSlot(index=i, hold=1) for i in range(4)),
            frame_duration_ms=100,
            loop_policy=LoopPolicy.LOOP,
        )
    current = await service.assess_set_quality(
        "release-path", current.revision, "actor"
    )
    current = await service.approve_set("release-path", current.revision, "actor")
    release = await service.create_release(
        "release-path", current.revision, "first", "actor"
    )
    exported = await service.create_export(release.release_id, "png-json", "actor")
    package = assets.assets[exported.artifact_key]
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert {m["motion_id"] for m in manifest["motions"]} == {"idle", "death"}
        assert all(
            m["view_id"] == "single" and m["view_heading"] is None
            for m in manifest["motions"]
        )
        assert archive.read("motions/idle.png").startswith(b"\x89PNG")

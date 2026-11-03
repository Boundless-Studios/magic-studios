"""T17: motion references, style, and script."""

import pytest
from pydantic import ValidationError

from magic_video_editor.memory import MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import approve_subject, create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ExportPackingEstimate,
    MotionCandidate,
    MotionFrameMetadata,
    MotionKind,
    MotionPreset,
    MotionReference,
    MotionReferenceRole,
    MotionSpecification,
    StylePreset,
    SubjectRevision,
)
from magic_video_editor.studio.service import StudioService


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


def test_style_presets_match_the_reference_ten() -> None:
    assert {preset.value for preset in StylePreset} == {
        "high_fantasy", "cyberpunk", "steampunk", "space_corporate", "grimdark",
        "post_apocalyptic", "noir", "psychedelic", "ancient_mythology", "western",
    }


def test_end_reference_without_start_or_context_is_rejected() -> None:
    with pytest.raises(ValidationError, match="end reference requires a start or context"):
        MotionSpecification(
            motion_id="idle", name="Idle", kind=MotionKind.PRESET, preset=MotionPreset.IDLE,
            references=(MotionReference(key="sources/studio/p/motion-references/a.png", sha256="a" * 64, role=MotionReferenceRole.END),),
        )


def test_end_reference_with_a_context_reference_is_accepted() -> None:
    specification = MotionSpecification(
        motion_id="idle", name="Idle", kind=MotionKind.PRESET, preset=MotionPreset.IDLE,
        references=(
            MotionReference(key="sources/studio/p/motion-references/a.png", sha256="a" * 64, role=MotionReferenceRole.CONTEXT),
            MotionReference(key="sources/studio/p/motion-references/b.png", sha256="b" * 64, role=MotionReferenceRole.END),
        ),
    )
    assert len(specification.references) == 2


async def _service_with_approved_subject():
    store = MemoryStudioStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1", artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64, width=1024, height=1024,
    )
    manifest = approve_subject(
        manifest.with_subject(subject), revision_id="subject-1", assessment_id="assessment-1",
        approver_id="user-1", policy_version="subject-v1",
    )
    project = await store.create(manifest)
    assets = MemoryAssetStore()
    service = StudioService(
        store=store, assets=assets, jobs=MemoryJobStore(), dispatcher=_Dispatcher(),
        motion_generation_available=True,
    )
    return service, store, assets, project


async def test_motion_reference_upload_uses_the_motion_references_namespace() -> None:
    service, _, _, project = await _service_with_approved_subject()

    target = await service.create_motion_reference_upload(
        "project-1", "image/jpeg", "user-1", project.revision
    )

    assert target.key.startswith("sources/studio/project-1/motion-references/")
    assert target.key.endswith(".jpg")


async def test_update_motion_persists_references_style_and_script_and_keeps_candidates() -> None:
    service, store, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(motion_id="idle", name="Idle", kind=MotionKind.PRESET, preset=MotionPreset.IDLE),
        "user-1",
    )
    candidate = MotionCandidate(
        candidate_id="idle-a", artifact_key="outputs/studio/project-1/motions/idle/idle-a.mp4",
        sha256="c" * 64,
        metadata=MotionFrameMetadata(
            decodable=True, media_kind="sheet", frame_count=4, width=256, height=64,
            constant_geometry=True, has_alpha=True,
            packing_estimate=ExportPackingEstimate(width=256, height=64),
        ),
    )
    project = await store.append(
        "project-1", project.revision,
        project.manifest.model_copy(update={
            "motions": tuple(
                motion.model_copy(update={"candidates": (candidate,)})
                if motion.motion_id == "idle" else motion
                for motion in project.manifest.motions
            )
        }),
    )

    updated = await service.update_motion(
        "project-1", project.revision, "idle",
        MotionSpecification(
            motion_id="idle", name="Idle", kind=MotionKind.PRESET, preset=MotionPreset.IDLE,
            style=StylePreset.CYBERPUNK, script="A short beat before the loop settles.",
            references=(MotionReference(key="sources/studio/project-1/motion-references/r1.png", sha256="d" * 64, role=MotionReferenceRole.CONTEXT),),
        ),
        "user-1",
    )

    idle = next(m for m in updated.manifest.motions if m.motion_id == "idle")
    assert idle.specification.style is StylePreset.CYBERPUNK
    assert idle.specification.script == "A short beat before the loop settles."
    assert len(idle.specification.references) == 1
    assert len(idle.candidates) == 1
    assert idle.candidates[0].candidate_id == "idle-a"


async def test_dispatch_carries_references_style_and_script_and_prompt_appends_style_clause() -> None:
    service, _, _, project = await _service_with_approved_subject()
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(
            motion_id="cheer", name="Cheer", kind=MotionKind.CUSTOM, prompt="A quick cheer",
            style=StylePreset.NOIR, script="Beat one, beat two.",
            references=(MotionReference(key="sources/studio/project-1/motion-references/r1.png", sha256="d" * 64, role=MotionReferenceRole.CONTEXT),),
        ),
        "user-1",
    )

    job = await service.generate_motion("project-1", project.revision, "cheer", "user-1")

    assert job.payload["style"] == "noir"
    assert job.payload["script"] == "Beat one, beat two."
    assert job.payload["references"] == [
        {"key": "sources/studio/project-1/motion-references/r1.png", "sha256": "d" * 64, "role": "context"}
    ]
    assert "Visual style: Noir." in job.payload["prompt"]
    assert "Screenplay: Beat one, beat two." in job.payload["prompt"]

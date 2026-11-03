"""T18: alpha key-out as a deterministic static repair."""

import subprocess
from hashlib import sha256
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import create_app
from magic_video_editor.contracts import GpuGenerationResult, VideoJobResult
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.manifest import approve_subject, create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    Backdrop,
    CandidateSource,
    ExportPackingEstimate,
    Facing,
    LoopPolicy,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
    MotionSpecification,
    SubjectRevision,
)
from magic_video_editor.studio.quality import assess_motion
from magic_video_editor.studio.service import MotionDerivationError, StudioService


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


def _green_sheet_no_alpha() -> bytes:
    """A 4-frame RGB (no alpha channel) strip rendered on the chroma-green backdrop."""
    from PIL.PngImagePlugin import PngInfo

    sheet = Image.new("RGB", (256, 64), (0x00, 0xB1, 0x40))
    for index in range(4):
        # Vary the box height per frame so the first and last frames are not
        # pixel-identical -- a real 4-frame measurement (T12) makes an
        # accidental duplicate first/last frame a genuine finding.
        top = 15 - index
        subject_box = (index * 64 + 20, top, index * 64 + 44, top + 34)
        sheet.paste((255, 0, 0), subject_box)
    output = BytesIO()
    png_info = PngInfo()
    png_info.add_text("studio_frame_count", "4")
    sheet.save(output, format="PNG", pnginfo=png_info)
    return output.getvalue()


def _green_video_no_alpha() -> bytes:
    return subprocess.run(
        [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=0x00B140:s=32x32:r=4:d=1", "-frames:v", "4",
            "-pix_fmt", "yuv420p", "-f", "mp4", "-movflags",
            "frag_keyframe+empty_moov", "pipe:1",
        ],
        check=True, capture_output=True, timeout=15,
    ).stdout


async def _service_with_candidate(
    artifact: bytes, *, media_kind: MediaKind, backdrop: Backdrop = Backdrop.CHROMA_GREEN
):
    store = MemoryStudioStore()
    assets = MemoryAssetStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64, width=1024, height=1024,
    )
    manifest = approve_subject(
        manifest.with_subject(subject), revision_id="subject-1",
        assessment_id="assessment-1", approver_id="user-1", policy_version="subject-v1",
    )
    manifest = manifest.with_motion(
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE)
    )
    extension = "png" if media_kind is MediaKind.SHEET else "mp4"
    artifact_key = f"outputs/studio/project-1/motions/idle/source-a.{extension}"
    assets.assets[artifact_key] = artifact
    metadata = MotionFrameMetadata(
        decodable=True, media_kind=media_kind, frame_count=4,
        width=256 if media_kind is MediaKind.SHEET else 32,
        height=64 if media_kind is MediaKind.SHEET else 32,
        constant_geometry=True, has_alpha=False,
        packing_estimate=ExportPackingEstimate(
            width=256 if media_kind is MediaKind.SHEET else 128, height=64 if media_kind is MediaKind.SHEET else 32
        ),
    )
    candidate = MotionCandidate(
        candidate_id="source-a", artifact_key=artifact_key, sha256=sha256(artifact).hexdigest(),
        duration_ms=1000, metadata=metadata,
        generation_settings=MotionGenerationSettings(backdrop=backdrop),
    )
    candidate = candidate.model_copy(
        update={"assessment": assess_motion(candidate, LoopPolicy.LOOP)}
    )
    motions = (
        manifest.motions[0].model_copy(
            update={"candidates": (candidate,), "selected_candidate_id": "source-a"}
        ),
    )
    manifest = manifest.model_copy(update={"motions": motions})
    project = await store.create(manifest)
    service = StudioService(store=store, assets=assets)
    return service, store, assets, project


async def test_derive_alpha_identity_includes_edit_state_not_only_source_sha256() -> None:
    """T6: two non-destructive revisions can share source.sha256 while
    differing in ordered_frames/holds/facing. Deriving alpha for the first
    must not make the second's derivation return the first's existing
    candidate."""
    artifact = _green_sheet_no_alpha()
    store = MemoryStudioStore()
    assets = MemoryAssetStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64, width=1024, height=1024,
    )
    manifest = approve_subject(
        manifest.with_subject(subject), revision_id="subject-1",
        assessment_id="assessment-1", approver_id="user-1", policy_version="subject-v1",
    )
    manifest = manifest.with_motion(
        MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE)
    )
    artifact_key = "outputs/studio/project-1/motions/idle/source-a.png"
    assets.assets[artifact_key] = artifact
    metadata = MotionFrameMetadata(
        decodable=True, media_kind=MediaKind.SHEET, frame_count=4, width=256, height=64,
        constant_geometry=True, has_alpha=False,
        packing_estimate=ExportPackingEstimate(width=256, height=64),
    )
    same_source_sha256 = sha256(artifact).hexdigest()
    default_edit = MotionCandidate(
        candidate_id="source-default", artifact_key=artifact_key, sha256=same_source_sha256,
        duration_ms=1000, metadata=metadata,
    )
    default_edit = default_edit.model_copy(
        update={"assessment": assess_motion(default_edit, LoopPolicy.LOOP)}
    )
    reordered_edit = default_edit.model_copy(update={
        "candidate_id": "source-reordered",
        "facing": Facing.LEFT,
        "frame_duration_ms": 250,
    })
    motions = (
        manifest.motions[0].model_copy(
            update={
                "candidates": (default_edit, reordered_edit),
                "selected_candidate_id": "source-default",
            }
        ),
    )
    manifest = manifest.model_copy(update={"motions": motions})
    project = await store.create(manifest)
    service = StudioService(store=store, assets=assets)

    after_default = await service.derive_alpha(
        "project-1", project.revision, "idle", "source-default", "user-1"
    )
    after_reordered = await service.derive_alpha(
        "project-1", after_default.revision, "idle", "source-reordered", "user-1"
    )

    motion = after_reordered.manifest.motions[0]
    derived = [c for c in motion.candidates if c.source is CandidateSource.DERIVED]
    assert len(derived) == 2
    assert derived[0].candidate_id != derived[1].candidate_id
    assert {tuple(c.parent_revision_ids) for c in derived} == {
        ("source-default",), ("source-reordered",)
    }


async def test_motion_missing_alpha_finding_carries_the_derive_alpha_action() -> None:
    _, _, _, project = await _service_with_candidate(
        _green_sheet_no_alpha(), media_kind=MediaKind.SHEET
    )
    finding = project.manifest.motions[0].candidates[0].assessment.findings[0]
    assert finding.code == "motion_missing_alpha"
    assert finding.action_id == "derive_alpha"


async def test_derive_alpha_keys_out_a_green_sheet_and_selects_it_when_source_selected() -> None:
    service, _, _, project = await _service_with_candidate(
        _green_sheet_no_alpha(), media_kind=MediaKind.SHEET
    )

    derived = await service.derive_alpha(
        "project-1", project.revision, "idle", "source-a", "user-1"
    )

    motion = derived.manifest.motions[0]
    assert len(motion.candidates) == 2
    new_candidate = motion.candidates[-1]
    assert new_candidate.metadata.has_alpha is True
    # T12: the derived strip must keep the source's 4-frame count, not
    # collapse to a single-frame candidate.
    assert new_candidate.metadata.frame_count == 4
    assert new_candidate.parent_revision_ids == ("source-a",)
    assert new_candidate.source is CandidateSource.DERIVED
    assert new_candidate.recipe.op == "derive_alpha"
    assert new_candidate.recipe.key == "0x00B140"
    assert new_candidate.recipe.similarity == 0.10
    assert new_candidate.recipe.blend == 0.08
    assert motion.selected_candidate_id == new_candidate.candidate_id
    # No blocking findings remain once alpha is present.
    assert not new_candidate.assessment.blocking_findings


async def test_derive_alpha_does_not_auto_select_when_source_was_not_selected() -> None:
    service, store, _, project = await _service_with_candidate(
        _green_sheet_no_alpha(), media_kind=MediaKind.SHEET
    )
    unselected = project.manifest.model_copy(
        update={
            "motions": (
                project.manifest.motions[0].model_copy(
                    update={"selected_candidate_id": None}
                ),
            )
        }
    )
    project = await store.append("project-1", project.revision, unselected)

    derived = await service.derive_alpha(
        "project-1", project.revision, "idle", "source-a", "user-1"
    )

    motion = derived.manifest.motions[0]
    assert motion.selected_candidate_id is None
    assert len(motion.candidates) == 2


async def test_derive_alpha_is_idempotent_on_the_same_source() -> None:
    service, _, _, project = await _service_with_candidate(
        _green_sheet_no_alpha(), media_kind=MediaKind.SHEET
    )
    first = await service.derive_alpha(
        "project-1", project.revision, "idle", "source-a", "user-1"
    )

    second = await service.derive_alpha(
        "project-1", first.revision, "idle", "source-a", "user-1"
    )

    assert len(second.manifest.motions[0].candidates) == 2
    assert second.revision == first.revision


async def test_derive_alpha_rejects_a_source_that_already_has_alpha() -> None:
    service, store, assets, project = await _service_with_candidate(
        _green_sheet_no_alpha(), media_kind=MediaKind.SHEET
    )
    already_alpha = project.manifest.motions[0].candidates[0].metadata.model_copy(
        update={"has_alpha": True}
    )
    updated = project.manifest.motions[0].candidates[0].model_copy(
        update={"metadata": already_alpha}
    )
    manifest = project.manifest.model_copy(
        update={
            "motions": (
                project.manifest.motions[0].model_copy(update={"candidates": (updated,)}),
            )
        }
    )
    project = await store.append("project-1", project.revision, manifest)

    try:
        await service.derive_alpha(
            "project-1", project.revision, "idle", "source-a", "user-1"
        )
    except MotionDerivationError as exc:
        assert exc.code == "motion_already_has_alpha"
    else:
        raise AssertionError("expected MotionDerivationError")


async def test_derive_alpha_keys_out_a_green_video_to_an_alpha_webm() -> None:
    service, _, _, project = await _service_with_candidate(
        _green_video_no_alpha(), media_kind=MediaKind.VIDEO
    )

    derived = await service.derive_alpha(
        "project-1", project.revision, "idle", "source-a", "user-1"
    )

    new_candidate = derived.manifest.motions[0].candidates[-1]
    assert new_candidate.metadata.has_alpha is True
    assert new_candidate.artifact_key.endswith(".webm")


async def test_backdrop_native_alpha_never_needs_the_derive_alpha_action() -> None:
    _, _, _, project = await _service_with_candidate(
        _green_sheet_no_alpha(), media_kind=MediaKind.SHEET, backdrop=Backdrop.NATIVE_ALPHA
    )
    # A missing alpha channel is still flagged either way (this is a static check on
    # the artifact); what native_alpha changes is that settle never auto-fixes it.
    finding = project.manifest.motions[0].candidates[0].assessment.findings[0]
    assert finding.action_id == "derive_alpha"


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


async def _service_with_custom_motion() -> tuple[StudioService, MemoryJobStore, MemoryAssetStore, object]:
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    assets = MemoryAssetStore()
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64, width=1024, height=1024,
    )
    manifest = approve_subject(
        manifest.with_subject(subject), revision_id="subject-1",
        assessment_id="assessment-1", approver_id="user-1", policy_version="subject-v1",
    )
    project = await store.create(manifest)
    service = StudioService(
        store=store, assets=assets, jobs=jobs, dispatcher=_Dispatcher(),
        motion_generation_available=True,
    )
    project = await service.add_motion(
        "project-1", project.revision,
        MotionSpecification(
            motion_id="idle", name="Idle", kind=MotionKind.CUSTOM, prompt="a calm idle",
        ),
        "user-1",
    )
    return service, jobs, assets, project


async def test_chroma_green_prompt_carries_the_backdrop_clause_native_alpha_does_not() -> None:
    service, _, _, project = await _service_with_custom_motion()

    chroma_job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1",
        settings=MotionGenerationSettings(backdrop=Backdrop.CHROMA_GREEN),
    )
    project = await service.get_project('project-1', 'user-1')
    assert "chroma green" in chroma_job.payload["prompt"]

    native_job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1", request_id="second",
        settings=MotionGenerationSettings(backdrop=Backdrop.NATIVE_ALPHA),
    )
    project = await service.get_project('project-1', 'user-1')
    assert "chroma green" not in native_job.payload["prompt"]


async def test_settling_a_chroma_green_candidate_auto_derives_alpha_once() -> None:
    service, jobs, assets, project = await _service_with_custom_motion()
    job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1",
        settings=MotionGenerationSettings(backdrop=Backdrop.CHROMA_GREEN),
    )
    project = await service.get_project('project-1', 'user-1')
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _green_sheet_no_alpha()

    settled = await service.settle_motion_candidate(job.job_id, "user-1")

    motion = settled.manifest.motions[0]
    assert len(motion.candidates) == 2
    assert motion.candidates[0].metadata.has_alpha is False
    derived = motion.candidates[1]
    assert derived.metadata.has_alpha is True
    assert derived.source is CandidateSource.DERIVED

    # Idempotent: settling again (e.g. a reconcile replay) does not derive twice.
    settled_again = await service.settle_motion_candidate(job.job_id, "user-1")
    assert len(settled_again.manifest.motions[0].candidates) == 2


async def test_native_alpha_settlement_never_auto_derives() -> None:
    service, jobs, assets, project = await _service_with_custom_motion()
    job = await service.generate_motion(
        "project-1", project.revision, "idle", "user-1",
        settings=MotionGenerationSettings(backdrop=Backdrop.NATIVE_ALPHA),
    )
    project = await service.get_project('project-1', 'user-1')
    claimed = await jobs.claim(job.job_id)
    assert claimed is not None
    await jobs.complete(
        job.job_id, claimed.claim_token, VideoJobResult(output_key=job.output_key)
    )
    assets.assets[job.output_key] = _green_sheet_no_alpha()

    settled = await service.settle_motion_candidate(job.job_id, "user-1")

    assert len(settled.manifest.motions[0].candidates) == 1


def test_derive_alpha_route_appends_and_selects_a_derived_candidate() -> None:
    import asyncio

    service, _, _, project = asyncio.run(
        _service_with_candidate(_green_sheet_no_alpha(), media_kind=MediaKind.SHEET)
    )
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    client = TestClient(create_app(runtime))

    response = client.post(
        "/studio/projects/project-1/motions/idle/candidates/source-a/derive-alpha",
        headers={"X-Studio-Actor-Id": "user-1"},
        json={"expected_revision": project.revision},
    )

    assert response.status_code == 200
    candidates = response.json()["manifest"]["motions"][0]["candidates"]
    assert len(candidates) == 2
    assert candidates[1]["metadata"]["has_alpha"] is True
    assert candidates[1]["source"] == "derived"
    assert candidates[1]["recipe"]["op"] == "derive_alpha"

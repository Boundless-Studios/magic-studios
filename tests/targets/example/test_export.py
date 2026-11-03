"""The Example Engine adapter's export profile, exercised through the
generic `ReleaseExporter` + `TargetRegistry` + adapter path: the studio's
core never special-cases a target's name, so proving this for the
reference adapter proves it for any adapter that satisfies the contract.
"""

import io
import json
import zipfile
from hashlib import sha256

from studio.test_releases import PNG_SHEET, releasable_manifest

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.studio.exports import PNG_JSON_V1, ReleaseExporter
from magic_video_editor.studio.manifest import (
    approve_set,
    set_digest,
    set_external_subject,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ExportProfile,
    ExternalSubject,
    Facing,
    FrameSlot,
    LoopPolicy,
    MotionCandidate,
    MotionPreset,
    MotionSpecification,
    MotionState,
    QualityAssessment,
)
from magic_video_editor.studio.service import StudioService

EXAMPLE_V1 = ExportProfile(name="example", version="1")


def _candidate(candidate_id: str, **overrides: object) -> MotionCandidate:
    base = releasable_manifest().motions[0].candidates[0]
    return base.model_copy(update={"candidate_id": candidate_id, **overrides})


def _manifest_with_motions(
    *motions: MotionState, external_subject: ExternalSubject | None = None
):
    manifest = releasable_manifest().model_copy(update={"motions": ()})
    for motion in motions:
        manifest = manifest.with_motion(motion)
    if external_subject is not None:
        manifest = set_external_subject(manifest, external_subject=external_subject)
    digest = set_digest(manifest)
    manifest = manifest.with_set_assessment(
        "example-set", digest, QualityAssessment(policy_version="set-policy-v1")
    )
    return approve_set(
        manifest,
        assessment_id="example-set",
        approver_id="user-1",
        policy_version="set-policy-v1",
    )


async def _export(manifest, *, profile=EXAMPLE_V1):
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(manifest)
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "example-release", "user-1"
    )
    exporter = ReleaseExporter(assets)
    record = await exporter.export(release, profile=profile)
    with zipfile.ZipFile(io.BytesIO(assets.assets[record.artifact_key])) as archive:
        names = archive.namelist()
        package = (
            json.loads(archive.read("example/package.json"))
            if "example/package.json" in names
            else None
        )
        bindings = (
            json.loads(archive.read("example/bindings.json"))
            if "example/bindings.json" in names
            else None
        )
        manifest_json = json.loads(archive.read("manifest.json"))
    return record, names, package, bindings, manifest_json


def _idle_walk_manifest():
    idle = MotionState(
        specification=MotionSpecification(
            motion_id="idle",
            name="Idle",
            preset=MotionPreset.IDLE,
            semantic_slot="idle",
        ),
        candidates=(_candidate("idle-1"),),
        selected_candidate_id="idle-1",
    )
    walk = MotionState(
        specification=MotionSpecification(
            motion_id="walk",
            name="Walk",
            preset=MotionPreset.WALK,
            semantic_slot="walk",
        ),
        candidates=(_candidate("walk-1"),),
        selected_candidate_id="walk-1",
    )
    run = MotionState(
        specification=MotionSpecification(
            motion_id="run",
            name="Run",
            preset=MotionPreset.RUN,
            semantic_slot="run",
        ),
        candidates=(_candidate("run-1"),),
        selected_candidate_id="run-1",
    )
    return _manifest_with_motions(idle, walk, run)


async def test_example_package_is_present_with_the_expected_motions() -> None:
    manifest = _idle_walk_manifest()
    _record, names, package, _bindings, _manifest_json = await _export(manifest)
    assert "example/package.json" in names
    assert "manifest.json" in names  # PNG_JSON_V1's manifest is untouched by the adapter
    assert package["format"] == {"name": "example", "version": "1"}
    by_motion = {strip["motion_id"]: strip for strip in package["strips"]}
    assert by_motion["idle"]["slot"] == "idle"
    assert by_motion["walk"]["slot"] == "walk"
    # RUN has no Example Engine slot: null slot, present but unbound.
    assert by_motion["run"]["slot"] is None


async def test_flip_reflects_facing_left() -> None:
    idle = MotionState(
        specification=MotionSpecification(
            motion_id="idle",
            name="Idle",
            preset=MotionPreset.IDLE,
            semantic_slot="idle",
        ),
        candidates=(_candidate("idle-1", facing=Facing.LEFT),),
        selected_candidate_id="idle-1",
    )
    manifest = _manifest_with_motions(idle)
    _record, _names, package, _bindings, manifest_json = await _export(manifest)
    # Facing is baked into the exported pixels, so the package must not also ask
    # the target to flip at runtime; otherwise the sprite is mirrored twice.
    assert package["strips"][0]["flip"] is False
    assert manifest_json["motions"][0]["facing"] == "left"


async def test_loop_policy_degrades_ping_pong_to_loop() -> None:
    idle = MotionState(
        specification=MotionSpecification(
            motion_id="idle",
            name="Idle",
            preset=MotionPreset.IDLE,
            semantic_slot="idle",
        ),
        candidates=(_candidate("idle-1", loop_policy=LoopPolicy.PING_PONG),),
        selected_candidate_id="idle-1",
    )
    manifest = _manifest_with_motions(idle)
    _record, _names, package, _bindings, _manifest_json = await _export(manifest)
    assert package["strips"][0]["loop_mode"] == "loop"


async def test_frames_reflect_ordered_frames_after_hold_expansion() -> None:
    idle = MotionState(
        specification=MotionSpecification(
            motion_id="idle",
            name="Idle",
            preset=MotionPreset.IDLE,
            semantic_slot="idle",
        ),
        candidates=(
            _candidate(
                "idle-1",
                ordered_frames=(
                    FrameSlot(index=2, hold=3),
                    FrameSlot(index=0, hold=1),
                    FrameSlot(index=1, hold=2),
                ),
            ),
        ),
        selected_candidate_id="idle-1",
    )
    manifest = _manifest_with_motions(idle)
    _record, _names, package, _bindings, _manifest_json = await _export(manifest)
    strip = package["strips"][0]
    assert strip["frames"] == [2, 2, 2, 0, 1, 1]
    assert strip["columns"] == 6
    assert strip["rows"] == 1


async def test_bindings_json_present_only_with_an_external_subject_and_only_for_slotted_motions() -> (
    None
):
    manifest = _idle_walk_manifest()
    _record, names, _package, bindings, _manifest_json = await _export(manifest)
    assert "example/bindings.json" not in names
    assert bindings is None

    external_subject = ExternalSubject(
        system="example",
        entity_kind="character",
        entity_id="char-42",
        display_name="Ser Ember",
    )
    bound_manifest = set_external_subject(
        _idle_walk_manifest(), external_subject=external_subject
    )
    _record, names, _package, bindings, _manifest_json = await _export(bound_manifest)
    assert "example/bindings.json" in names
    # RUN has no Example Engine slot -- it never appears in bindings.
    assert {entry["slot"] for entry in bindings} == {"idle", "walk"}
    assert all(
        entry["entity_kind"] == "character" and entry["entity_id"] == "char-42"
        for entry in bindings
    )
    for entry in bindings:
        assert set(entry["overrides"]) == {"frame_duration_ms", "loop_mode", "flip"}


async def test_example_export_is_byte_identical_on_re_export() -> None:
    manifest = _idle_walk_manifest()
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(manifest)
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "example-release", "user-1"
    )
    exporter = ReleaseExporter(assets)

    first = await exporter.export(release, profile=EXAMPLE_V1)
    first_bytes = assets.assets[first.artifact_key]
    second = await exporter.export(release, profile=EXAMPLE_V1)

    assert first == second
    assert assets.assets[second.artifact_key] == first_bytes
    assert (
        first.export_id
        == sha256(
            f"{release.release_digest}{EXAMPLE_V1.name}{EXAMPLE_V1.version}".encode()
        ).hexdigest()
    )


async def test_example_and_png_json_profiles_of_the_same_release_are_independent_exports() -> (
    None
):
    manifest = _idle_walk_manifest()
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(manifest)
    release = await StudioService(store=store, assets=assets).create_release(
        "project-1", created.revision, "example-release", "user-1"
    )
    exporter = ReleaseExporter(assets)

    png_json = await exporter.export(release, profile=PNG_JSON_V1)
    example = await exporter.export(release, profile=EXAMPLE_V1)

    assert png_json.export_id != example.export_id
    with zipfile.ZipFile(io.BytesIO(assets.assets[png_json.artifact_key])) as archive:
        assert "example/package.json" not in archive.namelist()
    with zipfile.ZipFile(io.BytesIO(assets.assets[example.artifact_key])) as archive:
        assert "example/package.json" in archive.namelist()
        assert "manifest.json" in archive.namelist()


async def test_service_create_export_accepts_the_example_profile_name() -> None:
    manifest = _idle_walk_manifest()
    assets = MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET})
    store = MemoryStudioStore()
    created = await store.create(manifest)
    service = StudioService(store=store, assets=assets)
    release = await service.create_release(
        "project-1", created.revision, "example-release", "user-1"
    )

    record = await service.create_export(release.release_id, "example", "user-1")

    assert record.profile_name == "example"
    fetched = await service.get_export(record.export_id, "user-1")
    assert fetched == record

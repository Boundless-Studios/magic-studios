import functools
import io
import math
import shutil
import subprocess
import tempfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest
from outbound_gpu_worker_pool.contracts import JobStatus as PoolJobStatus
from outbound_gpu_worker_pool.machine_profiles import LAST_FRAME_FEATURE_KEY, STANDARD, describe_profile
from PIL import Image
from pydantic import ValidationError

from magic_video_editor.contracts import JobOperation, JobStatus
from magic_video_editor.studio.models import SpriteLayout, TurnaroundEquipment, ViewId
from magic_video_editor.studio.service import MOTION_POOL_CAPABILITY_ID, _matches_activity_scope, is_settled
from magic_video_editor.studio.turnarounds import TurnaroundUnavailable, turnaround_prompt

from .test_motion_pool import _Pool
from .test_motion_references import _service_with_approved_subject

PROJECT = "project-1"
# A 4 s H3 closed loop decodes to 107 frames at 24 fps.
TURNTABLE_FRAMES = 107
AXE = TurnaroundEquipment(item="axe", attachment="hand", source_picture_side="left")
PAULDRON = TurnaroundEquipment(item="pauldron", attachment="shoulder", source_picture_side="right")


def _png(color=(0, 0, 0, 0), size=(80, 100)):
    image = Image.new("RGBA", size, color)
    for x in range(30, 50):
        for y in range(20, 80):
            image.putpixel((x, y), (200, 60, 40, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@functools.cache
def _turntable() -> bytes:
    """A synthetic closed-loop turntable on chroma green, shaped like a real H3 take.

    The figure turns once in place, starting and ending facing the viewer. Its torso is an
    elliptical cylinder, so the keyed width is widest at S and N and narrowest at the E and W
    profiles: the signal the picker reads. The real orc take this replaced is preserved in the
    private art-archive-2026-11-02 release; its measured widths stay in turnaround_fixtures/.
    """
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is required to encode the turnaround video")
    from PIL import ImageDraw

    with tempfile.TemporaryDirectory(prefix="studio-test-turntable-") as folder:
        for index in range(TURNTABLE_FRAMES):
            angle = 2 * math.pi * index / (TURNTABLE_FRAMES - 1)
            half = round(math.hypot(70 * math.cos(angle), 28 * math.sin(angle)))
            frame = Image.new("RGB", (512, 512), (0x00, 0xB1, 0x40))
            draw = ImageDraw.Draw(frame)
            draw.ellipse((226, 70, 286, 130), fill=(214, 170, 140))
            draw.rectangle((256 - half, 130, 256 + half, 320), fill=(150, 60, 40))
            draw.rectangle((256 - half * 2 // 3, 320, 256 + half * 2 // 3, 450), fill=(60, 50, 90))
            frame.save(Path(folder) / f"{index:05d}.png")
        output = Path(folder) / "turntable.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-framerate", "24", "-i", str(Path(folder) / "%05d.png"),
                        "-pix_fmt", "yuv420p", "-an", str(output)], check=True, capture_output=True, timeout=60)
        return output.read_bytes()


def _worker(*, last_frame=True, worker_id="desk", profile=STANDARD):
    from datetime import UTC, datetime
    from outbound_gpu_worker_pool.contracts import PoolWorkerStatus, PoolWorkerView
    from outbound_gpu_worker_pool.machine_profiles import CONFIG_KEY, RECIPE_KEY
    recipe = describe_profile(profile)
    versions = {RECIPE_KEY: recipe["recipe_version"], CONFIG_KEY: "config-v1"}
    if last_frame:
        versions[LAST_FRAME_FEATURE_KEY] = "1"
    return PoolWorkerView(worker_id, PoolWorkerStatus.ONLINE, datetime.now(UTC), (recipe["capability_id"],), (),
                          None, False, "user-1", vram_mb=max(24564, recipe["min_vram_mb"]), runtime_versions=versions)


def _defaults(*rows):
    from magic_video_editor.machine_defaults import HostedMachineDefaults

    class Pool:
        async def worker_views(self, **kwargs):
            return rows

    async def tenant_for(owner):
        return "user-1"
    return HostedMachineDefaults(Pool(), tenant_for)


async def _project(*, heading=ViewId.S, reference=None, worker=None):
    service, store, assets, project = await _service_with_approved_subject()
    pool = _Pool()
    service._motion_pool = pool
    service.machine_defaults = _defaults(worker or _worker())
    project = await service.set_sprite_layout(PROJECT, project.revision, SpriteLayout.ISOMETRIC, "user-1")
    content = reference or _png()
    key = f"sources/studio/{PROJECT}/views/{heading.value}/references/a.png"
    await assets.write_once(key, content, "image/png")
    project = await service.set_character_view(PROJECT, project.revision, heading, "user-1",
                                               reference=(key, sha256(content).hexdigest()), replace_reference=True)
    return service, pool, assets, project


def _view(project, heading=ViewId.S):
    return next(v for v in project.manifest.views if v.view_id == heading)


async def _start(service, project, request_id="turn-1", equipment=(AXE, PAULDRON), heading=ViewId.S):
    view = _view(project, heading)
    return await service.start_turnaround(PROJECT, project.revision, str(view.view_id),
                                          view.reference.revision_id, equipment, "user-1", request_id)


def test_prompt_keeps_every_attachment_without_naming_a_side():
    prompt = turnaround_prompt(ViewId.S, (
        AXE, PAULDRON, TurnaroundEquipment(item="sword", attachment="hip", source_picture_side="left"),
        TurnaroundEquipment(item="quiver", attachment="back", source_picture_side="unsided")))
    assert "The axe stays in the same hand." in prompt
    assert "The pauldron stays on the same shoulder." in prompt
    assert "The sword stays on the same hip." in prompt
    assert "The quiver stays on the back." in prompt
    assert "first turning to face the right side of the picture" in prompt
    assert not any(word in prompt.lower() for word in ("left hand", "right hand", "left shoulder", "right shoulder"))
    assert prompt == turnaround_prompt(ViewId.S, (AXE, PAULDRON, TurnaroundEquipment(
        item="sword", attachment="hip", source_picture_side="left"), TurnaroundEquipment(
        item="quiver", attachment="back", source_picture_side="unsided")))


@pytest.mark.parametrize("attachment", ["hand", "shoulder", "hip"])
def test_sided_attachments_refuse_unsided(attachment):
    with pytest.raises(ValidationError, match="left or right"):
        TurnaroundEquipment(item="axe", attachment=attachment, source_picture_side="unsided")


async def test_turnaround_is_a_pinned_closed_loop_on_the_prepared_frame():
    service, pool, assets, project = await _project()
    take, record = await _start(service, project)
    submitted = pool.submissions[0]
    frame = take.prepared_frame
    assert record.operation is JobOperation.GENERATE_TURNAROUND
    assert "motion_id" not in record.payload
    assert record.payload["turnaround_id"] == take.turnaround_id
    assert submitted.capability_id == MOTION_POOL_CAPABILITY_ID
    assert submitted.payload["images"] == {"first_frame": frame.key, "last_frame": frame.key}
    assert submitted.input_keys == (frame.key,)
    assert submitted.requirements.worker_ids == ("desk",)
    assert submitted.requirements.runtime_versions[LAST_FRAME_FEATURE_KEY] == "1"
    assert submitted.requirements.min_vram_mb == 24000
    assert submitted.payload["steps"] == 20 and submitted.payload["width"] == submitted.payload["height"] == 512
    stored = await assets.read(frame.key)
    assert sha256(stored).hexdigest() == frame.sha256
    assert (frame.width, frame.height, frame.algorithm, frame.slots) == (512, 512, "full_canvas_contain_v1",
                                                                         ("first_frame", "last_frame"))
    assert record.payload["reference_preparation"]["slots"] == ["first_frame", "last_frame"]
    assert record.payload["start_frame_sha256"] == frame.sha256
    current = await service.get_project(PROJECT, "user-1")
    assert [t.turnaround_id for t in current.manifest.turnarounds] == [take.turnaround_id]
    assert current.manifest.motions == project.manifest.motions
    assert take.source_backdrop == "transparent"


async def test_retry_with_the_same_request_returns_the_same_take():
    service, pool, _, project = await _project()
    take, record = await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    again, again_record = await _start(service, current)
    assert again.turnaround_id == take.turnaround_id and again_record.job_id == record.job_id
    assert len(pool.submissions) == 1


async def test_source_must_be_the_views_current_reference():
    service, _, _, project = await _project()
    with pytest.raises(ValueError, match="current bound reference"):
        await service.start_turnaround(PROJECT, project.revision, "S", "not-the-revision", (), "user-1", "r")


async def test_only_front_and_front_three_quarter_sources_are_qualified():
    service, pool, _, project = await _project(heading=ViewId.E)
    with pytest.raises(TurnaroundUnavailable, match="front three-quarter"):
        await _start(service, project, heading=ViewId.E)
    assert pool.submissions == []


@pytest.mark.parametrize("heading", [ViewId.SE, ViewId.SW])
async def test_a_three_quarter_source_starts_with_the_qualified_s_prompt(heading):
    service, pool, _, project = await _project(heading=heading)
    take, _ = await _start(service, project, heading=heading)
    assert take.source_heading is heading and len(pool.submissions) == 1
    # The qualification ran the S prompt word for word.
    assert take.prompt == turnaround_prompt(ViewId.S, (AXE, PAULDRON))


async def test_scenery_backed_sources_are_refused():
    service, pool, _, project = await _project(reference=_png(color=(90, 90, 200, 255)))
    with pytest.raises(TurnaroundUnavailable, match="transparent or green-screen"):
        await _start(service, project)
    assert pool.submissions == []


async def test_a_green_screen_source_is_accepted():
    service, _, _, project = await _project(reference=_png(color=(0, 200, 30, 255)))
    take, _ = await _start(service, project)
    assert take.source_backdrop == "chroma"


async def test_no_machine_reporting_last_frame_means_no_job():
    service, pool, _, project = await _project(worker=_worker(last_frame=False))
    with pytest.raises(TurnaroundUnavailable, match="No machine supports turnarounds"):
        await _start(service, project)
    assert pool.submissions == [] and (await service.list_turnarounds(PROJECT, "user-1")) == []


async def test_without_hosted_machines_turnarounds_are_unavailable():
    service, _, _, project = await _project()
    service.machine_defaults = None
    with pytest.raises(TurnaroundUnavailable, match="hosted machine"):
        await _start(service, project)


async def test_turnaround_work_is_never_subject_or_motion_activity():
    service, _, _, project = await _project()
    take, record = await _start(service, project)
    assert not _matches_activity_scope(record, "subject")
    assert not _matches_activity_scope(record, "idle")
    assert _matches_activity_scope(record, f"turnaround:{take.turnaround_id}")
    assert not _matches_activity_scope(record, "turnaround:turnaround-" + "0" * 32)
    subject = await service.list_activity_operations(PROJECT, "user-1", motion_id="subject")
    assert all(item.record.operation is not JobOperation.GENERATE_TURNAROUND for item in subject.operations)
    scoped = await service.list_activity_operations(PROJECT, "user-1", motion_id=f"turnaround:{take.turnaround_id}")
    assert [item.record.job_id for item in scoped.operations] == [record.job_id]


async def _complete(service, pool, record, take, *, slots=None):
    content = _turntable()
    await service._assets.write_once(record.output_key, content, "video/mp4")
    images = slots if slots is not None else {"first_frame": take.prepared_frame.key, "last_frame": take.prepared_frame.key}
    pool.records[record.job_id] = replace(
        pool.records[record.job_id], status=PoolJobStatus.COMPLETED, output_key=record.output_key,
        executed_by="desk", execution_metadata={"diagnostics": {
            "runtime_versions": {LAST_FRAME_FEATURE_KEY: "1"}, "effective_inputs": {"images": images}}})
    await service.reconcile_project_operations(PROJECT, "user-1")


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
async def test_settled_turnaround_keeps_its_video_and_frames_off_the_motions():
    service, pool, _, project = await _project()
    take, record = await _start(service, project)
    await _complete(service, pool, record, take)
    current = await service.get_project(PROJECT, "user-1")
    settled = current.manifest.turnarounds[0]
    job = await service._jobs.get(record.job_id)
    assert settled.candidate is not None and settled.candidate.metadata.frame_count == 107
    assert is_settled(job, current.manifest)
    assert all(not m.candidates for m in current.manifest.motions)
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["settled"] and described["frame_count"] == 107 and not described["source_superseded"]
    assert described["prepared_frame"]["key"] == take.prepared_frame.key


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
async def test_unattested_slots_never_settle():
    service, pool, _, project = await _project()
    take, record = await _start(service, project)
    await _complete(service, pool, record, take, slots={"first_frame": take.prepared_frame.key})
    current = await service.get_project(PROJECT, "user-1")
    assert current.manifest.turnarounds[0].candidate is None
    assert "both template slots" in (await service._jobs.get(record.job_id)).settlement_note


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
async def test_a_late_turnaround_is_marked_superseded_when_its_source_changed():
    service, pool, assets, project = await _project()
    take, record = await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    replacement = _png(size=(90, 110))
    key = f"sources/studio/{PROJECT}/views/S/references/b.png"
    await assets.write_once(key, replacement, "image/png")
    await service.set_character_view(PROJECT, current.revision, ViewId.S, "user-1",
                                     reference=(key, sha256(replacement).hexdigest()), replace_reference=True)
    await _complete(service, pool, record, take)
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["settled"] and described["source_superseded"]


async def test_cancel_stops_the_pool_job_and_the_ledger_row():
    service, pool, _, project = await _project()
    take, record = await _start(service, project)
    cancelled = await service.cancel_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert cancelled.status is JobStatus.CANCELLED and pool.cancelled == [record.job_id]


async def test_manifests_without_turnarounds_serialize_as_before():
    service, _, _, project = await _project()
    assert "turnarounds" not in project.manifest.model_dump(mode="json")
    take, _ = await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    assert current.manifest.model_dump(mode="json")["turnarounds"][0]["turnaround_id"] == take.turnaround_id


async def test_rest_creation_returns_an_addressable_turnaround_that_is_not_subject_work():
    from fastapi.testclient import TestClient
    from magic_video_editor.app import create_app
    from magic_video_editor.memory import InMemoryRuntime
    service, _, assets, project = await _project()
    client = TestClient(create_app(InMemoryRuntime(service._jobs, assets, studio_service=service)),
                        headers={"X-Studio-Actor-Id": "user-1"})
    view = _view(project)
    created = client.post(f"/studio/projects/{PROJECT}/turnarounds", json={
        "expected_revision": project.revision, "source_view_id": "S",
        "source_revision_id": view.reference.revision_id, "request_id": "rest-1",
        "equipment": [{"item": "axe", "attachment": "hand", "source_picture_side": "left"}]})
    assert created.status_code == 200, created.text
    body = created.json()
    turnaround_id = body["turnaround_id"]
    assert body["operation"]["operation"] == "generate_turnaround"
    assert body["operation"]["turnaround_id"] == turnaround_id and body["operation"]["motion_id"] is None
    listed = client.get(f"/studio/projects/{PROJECT}/turnarounds").json()["turnarounds"]
    assert [t["turnaround_id"] for t in listed] == [turnaround_id]
    assert client.get(f"/studio/projects/{PROJECT}/turnarounds/{turnaround_id}").json()["status"] == "queued"
    subject = client.get(f"/studio/projects/{PROJECT}/activity", params={"motion_id": "subject"}).json()
    assert all(op["operation"] != "generate_turnaround" for op in subject["operations"])
    unsided = client.post(f"/studio/projects/{PROJECT}/turnarounds", json={
        "expected_revision": project.revision + 1, "source_view_id": "S",
        "source_revision_id": view.reference.revision_id, "request_id": "rest-2",
        "equipment": [{"item": "axe", "attachment": "hand", "source_picture_side": "unsided"}]})
    assert unsided.status_code == 422
    assert client.post(f"/studio/projects/{PROJECT}/turnarounds/{turnaround_id}/cancel").json()["status"] == "cancelled"


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
async def test_a_settled_turnaround_counts_toward_storage_and_is_released_on_delete():
    service, pool, _, project = await _project()
    before = await service._owner_usage.stored_bytes("user-1")
    take, record = await _start(service, project)
    await _complete(service, pool, record, take)
    assert await service._owner_usage.stored_bytes("user-1") == before + len(_turntable())
    await service.reconcile_project_operations(PROJECT, "user-1")
    assert await service._owner_usage.stored_bytes("user-1") == before + len(_turntable())
    current = await service.get_project(PROJECT, "user-1")
    await service.delete_project(PROJECT, "user-1", current.manifest.name)
    assert await service._owner_usage.stored_bytes("user-1") == before


def test_a_lightx_pin_without_its_profile_name_is_refused_when_parsed():
    from outbound_gpu_worker_pool.machine_profiles import CONFIG_KEY, LIGHTX8, RECIPE_KEY
    from magic_video_editor.studio.models import MotionGenerationSettings
    lightx = _worker(worker_id="small", profile=LIGHTX8)
    with pytest.raises(ValidationError, match="last-frame conditioning"):
        MotionGenerationSettings(steps=8, provider_parameters={
            "worker_id": "small", "recipe_version": lightx.runtime_versions[RECIPE_KEY],
            "configuration_version": lightx.runtime_versions[CONFIG_KEY], "closed_loop": True})


async def test_a_lightx_worker_sorting_first_never_blocks_a_standard_turnaround():
    from outbound_gpu_worker_pool.machine_profiles import LIGHTX8
    service, pool, _, project = await _project()
    # "a-small" sorts before "desk" and even claims the feature; only the standard worker may run it.
    service.machine_defaults = _defaults(_worker(worker_id="a-small", profile=LIGHTX8), _worker())
    await _start(service, project)
    assert pool.submissions[0].requirements.worker_ids == ("desk",)


async def test_non_chroma_settings_are_refused_before_any_frame_is_rendered():
    from magic_video_editor.studio.models import MotionGenerationSettings
    service, pool, assets, project = await _project()
    view = _view(project)
    settings = MotionGenerationSettings(backdrop="native_alpha", provider_parameters={
        "worker_id": "desk", "recipe_version": describe_profile(STANDARD)["recipe_version"],
        "configuration_version": "config-v1", "closed_loop": True})
    with pytest.raises(ValueError, match="chroma green"):
        await service.start_turnaround(PROJECT, project.revision, "S", view.reference.revision_id, (),
                                       "user-1", "alpha", settings)
    assert pool.submissions == []
    assert not [k for k in assets.assets if "/opening-frames/" in k]


async def test_the_payload_records_the_chroma_backdrop_it_rendered():
    service, _, _, project = await _project()
    _, record = await _start(service, project)
    assert record.payload["backdrop"] == "chroma_green"
    assert record.payload["reference_preparation"]["backdrop"] == "chroma_green"


def _scenery(translucent_pixels=0):
    image = Image.new("RGBA", (80, 100), (90, 90, 200, 255))
    for i in range(translucent_pixels):
        image.putpixel((i, 0), (90, 90, 200, 100))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.parametrize("translucent", [1, 30])
async def test_a_few_translucent_pixels_never_pass_scenery_as_transparent(translucent):
    service, pool, _, project = await _project(reference=_scenery(translucent))
    with pytest.raises(TurnaroundUnavailable, match="transparent or green-screen"):
        await _start(service, project)
    assert pool.submissions == []


async def test_turnarounds_never_change_the_creative_set_digest():
    from magic_video_editor.studio.manifest import manifest_digest, set_digest
    service, _, _, project = await _project()
    take, _ = await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    assert current.manifest.turnarounds and set_digest(current.manifest) == set_digest(project.manifest)
    assert manifest_digest(current.manifest) != manifest_digest(project.manifest)


class _CappedJobs:
    """A job store whose unpaged listing returns only the newest 100, like PostgreSQL."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def list_for_project(self, project_id, *, cursor=None, limit=None):
        if limit is None:
            return await self._inner.list_for_project(project_id, limit=100)
        return await self._inner.list_for_project(project_id, cursor=cursor, limit=limit)


async def test_an_older_turnaround_is_found_past_a_hundred_newer_jobs():
    from datetime import UTC, datetime, timedelta
    from magic_video_editor.contracts import JobRecord
    service, pool, _, project = await _project()
    service._jobs = _CappedJobs(service._jobs)
    take, record = await _start(service, project)
    later = datetime.now(UTC) + timedelta(minutes=1)
    for index in range(150):
        service._jobs.records[f"later-{index}"] = JobRecord(
            job_id=f"later-{index:03d}", idempotency_key=f"later-{index}", operation=JobOperation.GENERATE_SUBJECT,
            status=JobStatus.COMPLETED, source_key="s", output_key=f"o{index}", attempts=1, project_id=PROJECT,
            manifest_operation_id=f"later-{index}", payload={}, created_at=later + timedelta(seconds=index))
    assert (await service.get_turnaround(PROJECT, take.turnaround_id, "user-1"))["job_id"] == record.job_id
    assert (await service.list_turnarounds(PROJECT, "user-1"))[0]["job_id"] == record.job_id
    current = await service.get_project(PROJECT, "user-1")
    again, again_record = await _start(service, current)
    assert again_record.job_id == record.job_id and len(pool.submissions) == 1
    assert len((await service.get_project(PROJECT, "user-1")).manifest.turnarounds) == 1
    assert (await service.cancel_turnaround(PROJECT, take.turnaround_id, "user-1")).status is JobStatus.CANCELLED


def _newer_jobs(jobs, count=150):
    from datetime import UTC, datetime, timedelta
    from magic_video_editor.contracts import JobRecord
    later = datetime.now(UTC) + timedelta(minutes=1)
    for index in range(count):
        jobs.records[f"later-{index}"] = JobRecord(
            job_id=f"later-{index:03d}", idempotency_key=f"later-{index}", operation=JobOperation.GENERATE_SUBJECT,
            status=JobStatus.FAILED, source_key="s", output_key=f"o{index}", attempts=1, project_id=PROJECT,
            manifest_operation_id=f"later-{index}", payload={}, created_at=later + timedelta(seconds=index))


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
async def test_reconciliation_settles_a_turnaround_behind_a_hundred_newer_jobs():
    service, pool, _, project = await _project()
    service._jobs = _CappedJobs(service._jobs)
    take, record = await _start(service, project)
    _newer_jobs(service._jobs)
    await _complete(service, pool, record, take)
    current = await service.get_project(PROJECT, "user-1")
    assert current.manifest.turnarounds[0].candidate is not None


async def test_a_failed_ledger_write_leaves_no_take_and_the_same_request_can_retry():
    service, pool, _, project = await _project()
    jobs = service._jobs
    original_submit = jobs.submit

    async def failing_submit(submission):
        raise RuntimeError("job store unavailable")
    jobs.submit = failing_submit
    with pytest.raises(RuntimeError):
        await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    assert current.manifest.turnarounds == () and pool.submissions == []
    jobs.submit = original_submit
    take, record = await _start(service, current)
    assert record.status is JobStatus.QUEUED and len(pool.submissions) == 1
    assert (await service.get_project(PROJECT, "user-1")).manifest.turnarounds[0].turnaround_id == take.turnaround_id


async def test_a_crash_between_ledger_and_take_resumes_from_the_queued_job():
    service, pool, _, project = await _project()
    original_append = service._store.append
    calls = {"n": 0}

    async def crashing_append(project_id, revision, manifest):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("process died")
        return await original_append(project_id, revision, manifest)
    service._store.append = crashing_append
    with pytest.raises(OSError):
        await _start(service, project)
    assert pool.submissions == []
    take, record = await _start(service, project)  # the same body, revision unchanged
    assert record.status is JobStatus.QUEUED and len(pool.submissions) == 1
    assert record.payload["turnaround_id"] == take.turnaround_id
    assert take.prepared_frame.key == record.source_key


async def test_a_take_that_cannot_be_stored_fails_its_job_instead_of_running():
    from magic_video_editor.studio.store import RevisionConflict
    service, pool, _, project = await _project()
    original_append = service._store.append

    async def racing_append(project_id, revision, manifest):
        raise RevisionConflict(revision + 1)
    service._store.append = racing_append
    with pytest.raises(RevisionConflict):
        await _start(service, project)
    service._store.append = original_append
    records = [r for r in service._jobs.records.values() if r.operation is JobOperation.GENERATE_TURNAROUND]
    assert [r.status for r in records] == [JobStatus.FAILED] and pool.submissions == []


async def test_a_retry_after_the_revision_moved_returns_the_original():
    service, pool, _, project = await _project()
    take, record = await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    await service._store.append(PROJECT, current.revision, current.manifest)
    again, again_record = await _start(service, project)  # the original body, now stale
    assert (again.turnaround_id, again_record.job_id) == (take.turnaround_id, record.job_id)
    assert len(pool.submissions) == 1


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_oversized_frame_analysis_is_a_bounded_error_and_never_cached(monkeypatch):
    from magic_video_editor.studio import turnarounds
    from magic_video_editor.studio.service import MotionDerivationError
    service, pool, assets, project = await _project()
    take, record = await _start(service, project)
    await _complete(service, pool, record, take)
    monkeypatch.setattr(turnarounds, "_ANALYSIS_LIMIT", 1000)
    with pytest.raises(MotionDerivationError, match="size budget"):
        await service.turnaround_frames(PROJECT, take.turnaround_id, "user-1")
    assert not [k for k in assets.assets if k.endswith(".json") and "/turnarounds/" in k]


async def test_a_stored_take_whose_pool_job_never_started_resumes_on_retry():
    service, pool, _, project = await _project()
    original_submit = pool.submit
    calls = {"n": 0}

    async def dying_submit(submission):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SystemExit("process stopped before the pool submission")
        return await original_submit(submission)
    pool.submit = dying_submit
    with pytest.raises(SystemExit):
        await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    assert len(current.manifest.turnarounds) == 1 and pool.submissions == []
    take, record = await _start(service, project)
    assert record.status is JobStatus.QUEUED and [s.job_id for s in pool.submissions] == [record.job_id]
    await _start(service, project)
    assert len(pool.submissions) == 1


async def test_identical_concurrent_requests_share_one_job_and_one_take():
    import asyncio
    service, pool, _, project = await _project()
    original_append = service._store.append
    both_planned = asyncio.Event()
    waiting = {"n": 0}

    async def synchronised_append(project_id, revision, manifest):
        waiting["n"] += 1
        if waiting["n"] == 2:
            both_planned.set()
        await both_planned.wait()
        return await original_append(project_id, revision, manifest)
    service._store.append = synchronised_append
    first, second = await asyncio.gather(_start(service, project), _start(service, project))
    assert first[0].turnaround_id == second[0].turnaround_id and first[1].job_id == second[1].job_id
    current = await service.get_project(PROJECT, "user-1")
    assert len(current.manifest.turnarounds) == 1
    job = await service._jobs.get(first[1].job_id)
    assert job.status is JobStatus.QUEUED and len(pool.submissions) == 1


class _LostResponsePool(_Pool):
    """Stores the job, then loses the response (a timeout after the commit)."""

    def __init__(self, error, *, stores=True):
        super().__init__()
        self.error, self.stores, self.lost = error, stores, 1

    async def submit(self, submission):
        if self.lost:
            self.lost -= 1
            if self.stores:
                await super().submit(submission)
            raise self.error
        return await super().submit(submission)


@pytest.mark.parametrize("stores", [True, False])
@pytest.mark.parametrize("error", [TimeoutError("read timed out"), ConnectionResetError("reset")])
async def test_an_ambiguous_pool_submission_stays_queued_and_resumes_once(error, stores):
    from magic_video_editor.studio.service import MotionGenerationUnavailable

    service, _, _, project = await _project()
    pool = service._motion_pool = _LostResponsePool(error, stores=stores)
    with pytest.raises(MotionGenerationUnavailable, match="retry the same request"):
        await _start(service, project)
    current = await service.get_project(PROJECT, "user-1")
    [take] = current.manifest.turnarounds
    [record] = [r for r in (await service._jobs.list_for_project(PROJECT))
                if r.operation is JobOperation.GENERATE_TURNAROUND]
    # The pinned worker may run it, so the row must stay where the sync and resume look.
    assert record.status is JobStatus.QUEUED
    again, again_record = await _start(service, current)
    assert again.turnaround_id == take.turnaround_id and again_record.job_id == record.job_id
    assert again_record.status is JobStatus.QUEUED
    assert [s.job_id for s in pool.submissions] == [record.job_id]


async def test_a_definite_pool_refusal_still_fails_the_row():
    from magic_video_editor.studio.service import MotionGenerationUnavailable

    service, _, _, project = await _project()
    service._motion_pool = _LostResponsePool(ValueError("tenant mismatch"), stores=False)
    with pytest.raises(MotionGenerationUnavailable, match="could not accept"):
        await _start(service, project)
    [record] = [r for r in (await service._jobs.list_for_project(PROJECT))
                if r.operation is JobOperation.GENERATE_TURNAROUND]
    assert record.status is JobStatus.FAILED


class _FlakyUsage:
    """The real memory store, failing once in a chosen way."""

    def __init__(self, inner, mode):
        self.inner, self.mode = inner, mode

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def record_artifact(self, owner_id, key, size):
        mode, self.mode = self.mode, None
        if mode == "before_commit":
            raise ConnectionResetError("usage store unavailable")
        recorded = await self.inner.record_artifact(owner_id, key, size)
        if mode == "lost_response":
            raise ConnectionResetError("response lost after the commit")
        return recorded


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
@pytest.mark.parametrize("mode", ["before_commit", "lost_response", "describe_unavailable"])
async def test_storage_accounting_that_fails_after_settling_is_finished_once_by_reconcile(mode):
    from magic_video_editor.contracts import AssetStorageUnavailable

    service, pool, assets, project = await _project()
    usage = service._owner_usage = _FlakyUsage(service._owner_usage, mode)
    if mode == "describe_unavailable":
        describe, failed = assets.describe, []

        async def flaky_describe(key):
            if key.endswith(".mp4") and not failed:
                failed.append(key)
                raise AssetStorageUnavailable("storage is down")
            return await describe(key)
        assets.describe = flaky_describe
    take, record = await _start(service, project)
    with pytest.raises((ConnectionResetError, AssetStorageUnavailable)):
        await _complete(service, pool, record, take)  # the take is stored, then the count fails
    current = await service.get_project(PROJECT, "user-1")
    stored = current.manifest.turnarounds[0]
    assert stored.candidate is not None and not stored.stored_bytes_recorded
    assert not is_settled(await service._jobs.get(record.job_id), current.manifest)
    # Reconciliation finishes it; repeats and an explicit settle add nothing more.
    await service.reconcile_project_operations(PROJECT, "user-1")
    await service.reconcile_project_operations(PROJECT, "user-1")
    await service.settle_turnaround(record.job_id, "user-1")
    size = len(await assets.read(record.output_key))
    assert await usage.stored_bytes("user-1") == size
    current = await service.get_project(PROJECT, "user-1")
    assert current.manifest.turnarounds[0].stored_bytes_recorded
    assert is_settled(await service._jobs.get(record.job_id), current.manifest)


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
@pytest.mark.parametrize("counted", [True, False])
async def test_deleting_a_project_releases_only_turnaround_bytes_that_were_counted(counted):
    service, pool, assets, project = await _project()
    service._owner_usage = _FlakyUsage(service._owner_usage, None if counted else "before_commit")
    take, record = await _start(service, project)
    if counted:
        await _complete(service, pool, record, take)
    else:
        with pytest.raises(ConnectionResetError):
            await _complete(service, pool, record, take)
    # Usage belonging to the owner's other projects.
    await service._owner_usage.add_bytes("user-1", 1_000_000)
    before = await service._owner_usage.stored_bytes("user-1")
    current = await service.get_project(PROJECT, "user-1")
    counted_elsewhere = await service._counted_bytes(current.manifest)
    size = len(await assets.read(record.output_key))
    await service.delete_project(PROJECT, "user-1", current.manifest.name)
    released = (size if counted else 0) + counted_elsewhere
    assert await service._owner_usage.stored_bytes("user-1") == before - released


async def test_a_corrupt_prepared_frame_already_at_its_key_is_never_dispatched():
    service, pool, assets, project = await _project()
    take, _ = await _start(service, project)
    # Same source and settings give the same content-addressed key; damage that object.
    assets.assets[take.prepared_frame.key] = b"not the rendered frame"
    current = await service.get_project(PROJECT, "user-1")
    with pytest.raises(TurnaroundUnavailable, match="integrity"):
        await _start(service, current, request_id="turn-2")
    assert len(pool.submissions) == 1
    assert len([r for r in (await service._jobs.list_for_project(PROJECT))
                if r.operation is JobOperation.GENERATE_TURNAROUND]) == 1


def _green_with_scenery(columns=0, top_patch=0):
    """Chroma green, with scenery in the leftmost `columns` or a `top_patch` along the top edge."""
    image = Image.new("RGBA", (100, 100), (0, 177, 64, 255))
    for x in range(100):
        for y in range(100):
            if x < columns or (y < 4 and 40 <= x < 40 + top_patch):
                image.putpixel((x, y), (140, 110, 90, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# The 4px border holds 1,600 pixels counted per edge strip.
@pytest.mark.parametrize("scenery,accepted", [
    ({}, True),
    ({"top_patch": 20}, True),   # 80 scenery pixels: 95% green
    ({"columns": 40}, False),    # 720 scenery pixels: 55% green, so a median reads green
    ({"columns": 10}, False),    # 480 scenery pixels: 70% green
])
def test_a_chroma_source_needs_nearly_all_border_pixels_green(scenery, accepted):
    from magic_video_editor.studio.turnarounds import source_backdrop
    if accepted:
        assert source_backdrop(_green_with_scenery(**scenery)) == "chroma"
    else:
        with pytest.raises(TurnaroundUnavailable, match="transparent or green-screen"):
            source_backdrop(_green_with_scenery(**scenery))


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
@pytest.mark.parametrize("field,value", [("candidate_id", "another-take"), ("source_sha256", "0" * 64),
                                         ("analysis_version", "pose-cycle-v0")])
async def test_a_cached_analysis_for_another_video_is_never_served(field, value):
    import json as json_module
    from magic_video_editor.studio.service import MotionDerivationError
    service, pool, assets, project = await _project()
    take, record = await _start(service, project)
    await _complete(service, pool, record, take)
    first = await service.turnaround_frames(PROJECT, take.turnaround_id, "user-1")
    [key] = [k for k in assets.assets if k.endswith(".json") and "/turnarounds/" in k]
    assets.assets[key] = json_module.dumps({**first, field: value}).encode()
    with pytest.raises(MotionDerivationError, match="does not match"):
        await service.turnaround_frames(PROJECT, take.turnaround_id, "user-1")


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
def test_an_api_client_settles_a_turnaround_with_the_documented_reconcile_call():
    """Reads never settle work; the documented reconcile call does, with no Studio UI involved."""
    import asyncio

    from fastapi.testclient import TestClient

    from magic_video_editor.app import ServiceRole, create_app
    from magic_video_editor.memory import InMemoryRuntime

    service, pool, assets, project = asyncio.run(_project())
    runtime = InMemoryRuntime(service._jobs, assets, studio_service=service)
    headers = {"X-Studio-Actor-Id": "user-1"}
    with TestClient(create_app(runtime, service_role=ServiceRole.API)) as client:
        view = _view(project)
        started = client.post(f"/studio/projects/{PROJECT}/turnarounds", headers=headers, json={
            "expected_revision": project.revision, "source_view_id": str(view.view_id),
            "source_revision_id": view.reference.revision_id, "request_id": "api-1", "equipment": []})
        assert started.status_code == 200, started.text
        turnaround_id = started.json()["turnaround_id"]
        job_id = started.json()["operation"]["job_id"]
        content = _turntable()
        record = asyncio.run(service._jobs.get(job_id))
        asyncio.run(assets.write_once(record.output_key, content, "video/mp4"))
        frame = asyncio.run(service.get_project(PROJECT, "user-1")).manifest.turnarounds[0].prepared_frame.key
        pool.records[job_id] = replace(pool.records[job_id], status=PoolJobStatus.COMPLETED, output_key=record.output_key,
            executed_by="desk", execution_metadata={"diagnostics": {"runtime_versions": {LAST_FRAME_FEATURE_KEY: "1"},
                "effective_inputs": {"images": {"first_frame": frame, "last_frame": frame}}}})
        read = client.get(f"/studio/projects/{PROJECT}/turnarounds/{turnaround_id}", headers=headers).json()
        assert not read["settled"]
        assert client.post(f"/studio/projects/{PROJECT}/activity/reconcile", headers=headers).status_code == 200
        read = client.get(f"/studio/projects/{PROJECT}/turnarounds/{turnaround_id}", headers=headers).json()
        assert read["settled"] and read["frame_count"] == 107


def test_a_turnaround_operation_reports_the_settings_it_was_submitted_with():
    import asyncio

    from magic_video_editor.studio.routes import OperationResponse

    service, _, _, project = asyncio.run(_project())
    take, record = asyncio.run(_start(service, project))
    response = OperationResponse.from_domain(record)
    settings = response.generation_settings
    assert response.turnaround_id == take.turnaround_id
    assert (settings.width, settings.height, settings.steps, settings.seed) == (
        record.payload["width"], record.payload["height"], record.payload["steps"], record.payload["seed"])
    assert settings.provider_parameters == record.payload["provider_parameters"]
    assert settings.provider_parameters["worker_id"] == "desk"
    assert response.prompt == take.prompt


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
async def test_deleting_from_a_snapshot_taken_before_settlement_releases_the_settled_video():
    from magic_video_editor.studio.service import turnaround_key_prefix
    service, pool, _, project = await _project()
    take, record = await _start(service, project)
    before_settlement = await service.get_project(PROJECT, "user-1")
    baseline = await service._owner_usage.stored_bytes("user-1")
    await _complete(service, pool, record, take)
    assert record.output_key.startswith(turnaround_key_prefix(PROJECT))
    assert await service._owner_usage.stored_bytes("user-1") > baseline
    # The deletion read the project before the turnaround settled.
    await service.purge_project(before_settlement)
    assert await service._owner_usage.stored_bytes("user-1") == baseline


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to measure the turnaround video")
async def test_a_settlement_that_counts_after_the_project_is_deleted_gives_the_count_back():
    from magic_video_editor.studio.service import ProjectNotFound
    service, pool, _, project = await _project()
    usage = service._owner_usage
    take, record = await _start(service, project)
    baseline = await usage.stored_bytes("user-1")
    record_artifact = usage.record_artifact

    async def delete_then_record(owner_id, key, size):
        # The project is deleted between storing the candidate and counting it.
        await service.purge_project(await service._store.get(PROJECT))
        return await record_artifact(owner_id, key, size)

    usage.record_artifact = delete_then_record
    with pytest.raises(ProjectNotFound):
        await _complete(service, pool, record, take)
    assert await usage.stored_bytes("user-1") == baseline


async def _settled(service, pool, project):
    take, record = await _start(service, project)
    await _complete(service, pool, record, take)
    return take, record


def _picks_from(suggested, source="S", **changes):
    from magic_video_editor.studio.models import TurnaroundPick
    frames = {h: entry["frame"] for h, entry in suggested.items() if h != source}
    picks = []
    for heading, frame in frames.items():
        extra = changes.get(heading, {})
        picks.append(TurnaroundPick(heading=heading, frame_index=extra.pop("frame", frame), **extra))
    return tuple(picks)


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_a_settled_turnaround_suggests_picks_and_cards_then_saves_a_pick_set():
    service, pool, assets, project = await _project()
    take, _ = await _settled(service, pool, project)
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    suggested = described["suggested_picks"]
    assert set(suggested) == {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}
    assert suggested["S"]["frame"] == 0 and "profile" in suggested["E"]["reason"]
    assert described["placement_cards"]["W"][1] == "Axe in the far hand."
    assert described["synthesized_parts"]["N"] and described["pick_set"] is None
    # Measured once: the cached analysis is reused on the next read.
    [cache] = [k for k in assets.assets if "/picks-turnaround-picks-v2-" in k]
    saved = await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, _picks_from(suggested), "user-1")
    assert saved["pick_revision"] == 1 and saved["findings"] == [] and set(saved["picks"]) == set(suggested) - {"S"}
    again = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert again["pick_set"]["pick_revision"] == 1
    assert [k for k in assets.assets if "/picks-turnaround-picks-v2-" in k] == [cache]


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_a_stale_pick_revision_is_refused_and_overrides_last_one_revision():
    from magic_video_editor.studio.turnaround_picks import PickSetRejected
    from magic_video_editor.studio.turnarounds import PickRevisionConflict
    service, pool, _, project = await _project()
    take, _ = await _settled(service, pool, project)
    suggested = (await service.get_turnaround(PROJECT, take.turnaround_id, "user-1"))["suggested_picks"]
    far = suggested["NW"]["frame"]
    confirmed = _picks_from(suggested, NE={"frame": far, "confirm_out_of_region": True, "confirm_out_of_order": True},
                            N={"confirm_out_of_order": True})
    saved = await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, confirmed, "user-1")
    assert {f["heading"] for f in saved["findings"]} >= {"NE"}
    with pytest.raises(PickRevisionConflict):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, confirmed, "user-1")
    # Replacing the set without repeating the override drops it: the same pick is refused.
    with pytest.raises(PickSetRejected):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 1,
                                            _picks_from(suggested, NE={"frame": far}), "user-1")


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_a_cached_pick_analysis_for_another_video_is_never_used():
    import json as json_module
    from magic_video_editor.studio.service import MotionDerivationError
    service, pool, assets, project = await _project()
    take, _ = await _settled(service, pool, project)
    first = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    [key] = [k for k in assets.assets if "/picks-turnaround-picks-v2-" in k]
    cached = json_module.loads(assets.assets[key])
    assets.assets[key] = json_module.dumps({**cached, "video_sha256": "0" * 64}).encode()
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"] is None and "does not match" in described["suggestions_error"]
    with pytest.raises(MotionDerivationError):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0,
                                            _picks_from(first["suggested_picks"]), "user-1")


async def test_picks_cannot_be_saved_before_the_take_settles():
    from magic_video_editor.studio.models import TurnaroundPick
    from magic_video_editor.studio.service import MotionOperationNotReady
    service, _, _, project = await _project()
    take, _ = await _start(service, project)
    with pytest.raises(MotionOperationNotReady):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0,
                                            (TurnaroundPick(heading=ViewId.N, frame_index=1),), "user-1")


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
def test_the_picks_route_reports_conflicts_and_rule_problems():
    import asyncio

    from fastapi.testclient import TestClient

    from magic_video_editor.app import ServiceRole, create_app
    from magic_video_editor.memory import InMemoryRuntime

    service, pool, assets, project = asyncio.run(_project())
    take, _ = asyncio.run(_settled(service, pool, project))
    headers = {"X-Studio-Actor-Id": "user-1"}
    path = f"/studio/projects/{PROJECT}/turnarounds/{take.turnaround_id}"
    with TestClient(create_app(InMemoryRuntime(service._jobs, assets, studio_service=service),
                               service_role=ServiceRole.API)) as client:
        suggested = client.get(path, headers=headers).json()["suggested_picks"]
        body = {h: {"frame_index": e["frame"]} for h, e in suggested.items() if h != "S"}
        saved = client.put(path + "/picks", headers=headers, json={"expected_pick_revision": 0, "picks": body})
        assert saved.status_code == 200 and saved.json()["pick_revision"] == 1
        stale = client.put(path + "/picks", headers=headers, json={"expected_pick_revision": 0, "picks": body})
        assert stale.status_code == 409 and stale.json()["detail"]["pick_revision"] == 1
        swapped = {**body, "NE": {"frame_index": suggested["NW"]["frame"]}}
        refused = client.put(path + "/picks", headers=headers, json={"expected_pick_revision": 1, "picks": swapped})
        assert refused.status_code == 422
        assert {p["heading"] for p in refused.json()["detail"]["problems"]} >= {"NE"}


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_a_frame_that_cannot_be_keyed_leaves_the_take_readable(monkeypatch):
    from magic_video_editor.plugins import subject_pool
    from magic_video_editor.plugins.subject_http import TerminalSubjectProviderError
    service, pool, _, project = await _project()
    take, _ = await _settled(service, pool, project)

    def unkeyable(frame):
        raise TerminalSubjectProviderError("no green-dominant border")

    monkeypatch.setattr(subject_pool, "key_green_screen", unkeyable)
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"] is None
    assert "could not be keyed: frame 0:" in described["suggestions_error"]


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_a_stale_pick_request_is_fenced_before_it_is_analysed_or_validated(monkeypatch):
    from magic_video_editor.studio import turnarounds
    from magic_video_editor.studio.models import TurnaroundPick
    from magic_video_editor.studio.turnarounds import PickRevisionConflict
    service, pool, _, project = await _project()
    take, _ = await _settled(service, pool, project)
    suggested = (await service.get_turnaround(PROJECT, take.turnaround_id, "user-1"))["suggested_picks"]
    await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, _picks_from(suggested), "user-1")

    async def no_analysis(*args):
        raise AssertionError("analysed a stale request")

    monkeypatch.setattr(turnarounds, "_pick_analysis", no_analysis)
    # Stale and invalid: the 409 comes first, not the validation of an obsolete set.
    with pytest.raises(PickRevisionConflict):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0,
                                            (TurnaroundPick(heading=ViewId.N, frame_index=1),), "user-1")



@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_without_suggestions_a_person_can_still_save_confirmed_picks_by_hand(monkeypatch):
    from magic_video_editor.plugins import subject_pool
    from magic_video_editor.plugins.subject_http import TerminalSubjectProviderError
    from magic_video_editor.studio.models import TurnaroundPick
    from magic_video_editor.studio.turnaround_picks import PickSetRejected
    service, pool, _, project = await _project()
    take, _ = await _settled(service, pool, project)

    def unkeyable(frame):
        raise TerminalSubjectProviderError("no green-dominant border")

    monkeypatch.setattr(subject_pool, "key_green_screen", unkeyable)
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"] is None and "frame 0:" in described["suggestions_error"]
    order = ["SE", "E", "NE", "N", "NW", "W", "SW"]
    by_hand = {heading: 10 + 12 * index for index, heading in enumerate(order)}

    def picks(confirm=True, **frames):
        return tuple(TurnaroundPick(heading=ViewId(h), frame_index=frames.get(h, f), confirm_out_of_region=confirm)
                     for h, f in by_hand.items())

    with pytest.raises(PickSetRejected, match="no suggestion"):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, picks(confirm=False), "user-1")
    with pytest.raises(PickSetRejected, match="out of turn order"):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, picks(N=5), "user-1")
    saved = await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, picks(), "user-1")
    assert saved["pick_revision"] == 1
    assert {f["kind"] for f in saved["findings"]} == {"unsuggested"}


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_a_storage_outage_leaves_the_take_readable(monkeypatch):
    from magic_video_editor.contracts import AssetStorageUnavailable
    service, pool, assets, project = await _project()
    take, _ = await _settled(service, pool, project)
    describe = assets.describe

    async def unavailable(key):
        if "/picks-" in key:
            raise AssetStorageUnavailable("storage is down")
        return await describe(key)

    monkeypatch.setattr(assets, "describe", unavailable)
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"] is None and "unavailable" in described["suggestions_error"]
    assert described["placement_cards"]


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_suggestions_stand_when_their_cache_cannot_be_written(monkeypatch):
    from magic_video_editor.contracts import AssetStorageUnavailable
    service, pool, assets, project = await _project()
    take, _ = await _settled(service, pool, project)
    write_once = assets.write_once

    async def unavailable(key, *args, **kwargs):
        if "/picks-" in key:
            raise AssetStorageUnavailable("storage is down")
        return await write_once(key, *args, **kwargs)

    monkeypatch.setattr(assets, "write_once", unavailable)
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"]["S"]["frame"] == 0
    assert not [k for k in assets.assets if "/picks-" in k]


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_concurrent_cold_reads_measure_the_take_once(monkeypatch):
    import asyncio
    import json

    from magic_video_editor.studio import turnarounds
    service, pool, _, project = await _project()
    take, _ = await _settled(service, pool, project)
    measure, calls = turnarounds._measure_widths, []

    def counted(content):
        calls.append(1)
        return measure(content)

    monkeypatch.setattr(turnarounds, "_measure_widths", counted)
    reads = await asyncio.gather(*(service.get_turnaround(PROJECT, take.turnaround_id, "user-1") for _ in range(4)))
    assert len(calls) == 1
    assert len({json.dumps(read["suggested_picks"], sort_keys=True) for read in reads}) == 1
    assert not turnarounds._ANALYSIS_LOCKS


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_an_analysis_whose_frame_count_changed_is_refused(monkeypatch):
    from magic_video_editor.studio import turnarounds
    from magic_video_editor.studio.service import MotionDerivationError
    service, pool, assets, project = await _project()
    take, _ = await _settled(service, pool, project)
    measure = turnarounds._measure_widths
    monkeypatch.setattr(turnarounds, "_measure_widths", lambda content: measure(content)[:-1])
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"] is None and "decoded" in described["suggestions_error"]
    with pytest.raises(MotionDerivationError, match="decoded"):
        await service.save_turnaround_picks(PROJECT, take.turnaround_id, 0, (), "user-1")
    assert not [k for k in assets.assets if "/picks-" in k]


async def test_the_analysis_lock_stays_registered_until_every_waiter_has_run():
    import asyncio
    from types import SimpleNamespace

    from magic_video_editor.studio import turnarounds
    take = SimpleNamespace(turnaround_id="t", candidate=SimpleNamespace(sha256="a" * 64))
    gates, running, peak = [], 0, 0

    async def analysis(service, take, key):
        # An analysis that leaves no cache, such as one whose cache write failed.
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        gate = asyncio.Event()
        gates.append(gate)
        await gate.wait()
        running -= 1
        return {}

    original = turnarounds._cached_or_measured
    turnarounds._cached_or_measured = analysis
    try:
        first = asyncio.create_task(turnarounds._pick_analysis(None, PROJECT, take))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(turnarounds._pick_analysis(None, PROJECT, take))
        await asyncio.sleep(0)
        gates[0].set()
        await first
        # The released lock looks free while the woken waiter has yet to finish:
        # a request arriving now must still queue behind it.
        late = asyncio.create_task(turnarounds._pick_analysis(None, PROJECT, take))
        while not (waiter.done() and late.done()):
            for gate in gates:
                gate.set()
            await asyncio.sleep(0)
    finally:
        turnarounds._cached_or_measured = original
    assert peak == 1 and not turnarounds._ANALYSIS_LOCKS


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is required to decode the turnaround video")
async def test_an_analysis_cached_by_the_previous_picker_is_never_used():
    import json as json_module
    service, pool, assets, project = await _project()
    take, _ = await _settled(service, pool, project)
    candidate = (await service.get_project(PROJECT, "user-1")).manifest.turnarounds[0].candidate
    old = f"outputs/studio/{PROJECT}/turnarounds/{take.turnaround_id}/picks-turnaround-picks-v1-{candidate.sha256[:16]}.json"
    stale = {"algorithm": "turnaround-picks-v1", "candidate_id": candidate.candidate_id,
             "video_sha256": candidate.sha256, "source_heading": "S", "frame_count": 7,
             "suggested_picks": {"S": {"frame": 0, "reason": "old"}, "N": {"frame": 3, "reason": "old"}}}
    await assets.write_once(old, json_module.dumps(stale).encode(), "application/json")
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"]["N"]["reason"] != "old"
    # A cache from this picker whose frame count does not match the take is refused too.
    [current] = [k for k in assets.assets if "/picks-turnaround-picks-v2-" in k]
    assets.assets[current] = json_module.dumps({**json_module.loads(assets.assets[current]), "frame_count": 7}).encode()
    described = await service.get_turnaround(PROJECT, take.turnaround_id, "user-1")
    assert described["suggested_picks"] is None and "frame count" in described["suggestions_error"]

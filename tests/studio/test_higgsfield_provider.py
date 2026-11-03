"""Bring-your-own-key Higgsfield generation: key custody, dispatch, sync, cancel."""

import io
import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.contracts import JobFailureCode, JobStatus
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.plugins import higgsfield
from magic_video_editor.plugins.higgsfield import (
    HiggsfieldClient,
    HiggsfieldError,
    HiggsfieldStatus,
)
from magic_video_editor.studio.manifest import approve_subject, create_project_manifest
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.models import (
    ApprovalState,
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
    MotionSpecification,
    SubjectRevision,
)
from magic_video_editor.studio.provider_accounts import (
    HIGGSFIELD,
    CredentialCipher,
    MemoryProviderAccountStore,
    ProviderAccounts,
    ProviderCredentialRejected,
)
from magic_video_editor.studio.service import (
    MotionGenerationUnavailable,
    MotionRequestBlocked,
    StudioService,
)

PROJECT = "project-1"
OWNER = "user-1"
KEY = "key-id-123:very-secret-value-9876"
SECRET = "s" * 40


class _Dispatcher:
    def __init__(self) -> None:
        self.job_ids: list[str] = []

    async def enqueue(self, job_id: str) -> None:
        self.job_ids.append(job_id)


class _Pool:
    def __init__(self) -> None:
        self.submissions: list[object] = []

    async def submit(self, submission):  # pragma: no cover - must never be called
        self.submissions.append(submission)
        raise AssertionError("a Higgsfield job must never reach the GPU pool")

    async def get(self, job_id):
        return None

    async def cancel(self, job_id):
        return True


def _tiny_mp4() -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x00B140:s=16x16:r=8",
         "-frames:v", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mp4",
         "-movflags", "frag_keyframe+empty_moov", "pipe:1"],
        check=True, capture_output=True, timeout=30,
    ).stdout


_VIDEO = _tiny_mp4()


class _FakeHiggsfield:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.uploads: list[bytes] = []
        self.submissions: list[tuple[str, dict]] = []
        self.status_value = HiggsfieldStatus("queued")
        self.cancelled: list[str] = []
        self.video = _VIDEO
        self.fail_submit: Exception | None = None

    def __call__(self, key: str) -> "_FakeHiggsfield":
        self.keys.append(key)
        return self

    async def upload(self, content: bytes, content_type: str) -> str:
        assert content_type == "image/png"
        self.uploads.append(content)
        return "https://cdn.higgsfield.test/frame.png"

    async def submit(self, path: str, arguments: dict) -> str:
        if self.fail_submit is not None:
            raise self.fail_submit
        self.submissions.append((path, arguments))
        return "req-1"

    async def status(self, request_id: str) -> HiggsfieldStatus:
        assert request_id == "req-1"
        return self.status_value

    async def cancel(self, request_id: str) -> bool:
        self.cancelled.append(request_id)
        return True

    async def download(self, url: str, max_bytes: int) -> bytes:
        return self.video


def _subject_png() -> bytes:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    for x in range(16, 48):
        for y in range(8, 56):
            image.putpixel((x, y), (200, 40, 40, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _accounts(cipher: CredentialCipher | None = None) -> ProviderAccounts:
    return ProviderAccounts(MemoryProviderAccountStore(), cipher or CredentialCipher((SECRET,)))


async def _service(*, with_key: bool = True, pool: _Pool | None = None):
    store = MemoryStudioStore()
    jobs = MemoryJobStore()
    assets = MemoryAssetStore()
    dispatcher = _Dispatcher()
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/studio/project-1/subjects/source.png",
        sha256="a" * 64, width=64, height=64,
    )
    manifest = create_project_manifest(PROJECT, "Ember", owner_id=OWNER)
    approved = approve_subject(
        manifest.with_subject(subject), revision_id="subject-1",
        assessment_id="assessment-1", approver_id=OWNER, policy_version="subject-v1",
    )
    assert approved.subject.approval.state is ApprovalState.APPROVED
    await store.create(approved)
    assets.assets[subject.artifact_key] = _subject_png()
    accounts = _accounts()
    if with_key:
        await accounts.save_key(OWNER, HIGGSFIELD, KEY)
    service = StudioService(
        store=store, assets=assets, jobs=jobs, dispatcher=dispatcher,
        motion_generation_available=True, motion_pool=pool, provider_accounts=accounts,
    )
    fake = _FakeHiggsfield()
    service.higgsfield_client = fake
    project = await service.get_project(PROJECT, OWNER)
    project = await service.add_motion(
        PROJECT, project.revision,
        MotionSpecification(motion_id="idle", name="Idle", kind=MotionKind.PRESET,
                            preset=MotionPreset.IDLE, loop_policy="loop", required=True),
        OWNER,
    )
    return service, jobs, dispatcher, fake, project


def _hosted_settings(**parameters) -> MotionGenerationSettings:
    return MotionGenerationSettings.model_validate(
        {**higgsfield.default_settings(), "provider_parameters": {
            **higgsfield.default_settings()["provider_parameters"], **parameters}}
    )


# --- key custody -----------------------------------------------------------


async def test_a_saved_key_is_stored_encrypted_and_only_described_by_a_hint() -> None:
    accounts = _accounts()

    saved = await accounts.save_key(OWNER, HIGGSFIELD, f"  {KEY}  ")

    assert saved.key_hint == "…9876"
    _, ciphertext = await accounts.store.get_credential(OWNER, HIGGSFIELD)
    assert KEY.encode() not in ciphertext and b"very-secret" not in ciphertext
    assert await accounts.key_for(OWNER, HIGGSFIELD) == KEY
    assert await accounts.key_for("someone-else", HIGGSFIELD) is None
    assert await accounts.delete_key(OWNER, HIGGSFIELD) is True
    assert await accounts.key_for(OWNER, HIGGSFIELD) is None


@pytest.mark.parametrize("bad", ["", "   ", "id: secret", "x" * 513])
async def test_blank_spaced_or_oversized_keys_are_rejected(bad: str) -> None:
    with pytest.raises(ProviderCredentialRejected):
        await _accounts().save_key(OWNER, HIGGSFIELD, bad)


async def test_a_single_opaque_key_is_accepted_as_copied() -> None:
    accounts = _accounts()

    await accounts.save_key(OWNER, HIGGSFIELD, "hf_opaque_key_without_colon_4321")

    assert await accounts.key_for(OWNER, HIGGSFIELD) == "hf_opaque_key_without_colon_4321"


async def test_a_rotated_secret_still_decrypts_keys_saved_under_the_old_one() -> None:
    store = MemoryProviderAccountStore()
    await ProviderAccounts(store, CredentialCipher((SECRET,))).save_key(OWNER, HIGGSFIELD, KEY)

    rotated = ProviderAccounts(store, CredentialCipher(("n" * 40, SECRET)))

    assert await rotated.key_for(OWNER, HIGGSFIELD) == KEY
    with pytest.raises(ProviderCredentialRejected):
        await ProviderAccounts(store, CredentialCipher(("n" * 40,))).key_for(OWNER, HIGGSFIELD)


def test_without_an_operator_secret_provider_keys_are_disabled() -> None:
    assert CredentialCipher.from_env({}) is None
    with pytest.raises(ValueError):
        CredentialCipher(("short",))


# --- dispatch ----------------------------------------------------------------


async def test_dispatch_sends_the_opening_frame_to_higgsfield_and_never_to_a_worker() -> None:
    pool = _Pool()
    service, jobs, dispatcher, fake, project = await _service(pool=pool)

    job = await service.generate_motion(
        PROJECT, project.revision, "idle", OWNER, request_id="request-1",
        settings=_hosted_settings(closed_loop=True),
    )

    assert dispatcher.job_ids == [] and pool.submissions == []
    assert fake.keys == [KEY]
    assert len(fake.uploads) == 1
    path, arguments = fake.submissions[0]
    assert path == "kling-video/v3.0/std/image-to-video"
    assert arguments["image_url"] == arguments["last_image_url"] == "https://cdn.higgsfield.test/frame.png"
    assert arguments["duration"] == 4 and arguments["sound"] == "off"
    stored = await jobs.get(job.job_id)
    assert arguments["prompt"] == stored.payload["prompt"]
    assert stored.status is JobStatus.QUEUED
    assert stored.payload["resolved_recipe"]["provider"] == HIGGSFIELD
    assert KEY not in json.dumps(stored.payload) and "very-secret" not in json.dumps(stored.payload)
    request = await service.provider_accounts.store.get_request(job.job_id)
    assert request.request_id == "req-1" and request.owner_id == OWNER

    # A replay of the same request returns the same job and does not resubmit.
    project = await service.get_project(PROJECT, OWNER)
    again = await service.generate_motion(
        PROJECT, project.revision, "idle", OWNER, request_id="request-1",
        settings=_hosted_settings(closed_loop=True),
    )
    assert again.job_id == job.job_id
    assert len(fake.submissions) == 1 and dispatcher.job_ids == []


async def test_without_a_saved_key_no_job_is_created() -> None:
    service, jobs, _, fake, project = await _service(with_key=False)

    with pytest.raises(MotionRequestBlocked, match="Higgsfield API key"):
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    assert await jobs.list_for_project(PROJECT) == ()
    assert fake.submissions == []


@pytest.mark.parametrize(
    ("parameters", "duration", "message"),
    [
        ({"worker_id": "w-1"}, 4, "machine parameters"),
        ({"api_key": "pasted-by-mistake"}, 4, "does not accept: api_key"),
        ({"higgsfield_model": "nope"}, 4, "unknown Higgsfield model"),
        ({"higgsfield_model": "seedance-2"}, 3, "4–15 seconds"),
    ],
)
async def test_invalid_hosted_requests_are_rejected_before_a_job_exists(parameters, duration, message) -> None:
    service, jobs, _, fake, project = await _service()
    settings = _hosted_settings(**parameters).model_copy(update={"duration_seconds": duration})

    with pytest.raises(ValueError, match=message):
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=settings)

    assert await jobs.list_for_project(PROJECT) == ()
    assert fake.submissions == []


async def test_a_rejected_submission_fails_the_job_without_retrying_it() -> None:
    service, jobs, dispatcher, fake, project = await _service()
    fake.fail_submit = HiggsfieldError("Higgsfield reports insufficient balance on this key", status_code=402)

    with pytest.raises(MotionGenerationUnavailable, match="insufficient balance"):
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    (job,) = await jobs.list_for_project(PROJECT)
    assert job.status is JobStatus.FAILED
    assert job.failure_code is JobFailureCode.PROVIDER_UNAVAILABLE
    assert "insufficient balance" in job.failure_message
    assert "may have received it" not in job.failure_message
    assert dispatcher.job_ids == []


@pytest.mark.parametrize("status_code", [None, 502])
async def test_an_unanswered_submission_warns_that_it_may_have_been_billed(status_code) -> None:
    service, jobs, _, fake, project = await _service()
    fake.fail_submit = HiggsfieldError("Higgsfield could not be reached", status_code=status_code)

    with pytest.raises(MotionGenerationUnavailable):
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    (job,) = await jobs.list_for_project(PROJECT)
    assert "may have received it" in job.failure_message


# --- sync --------------------------------------------------------------------


async def test_listing_folds_a_completed_result_into_the_ledger_with_honest_provenance() -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    await service.list_operations(PROJECT, OWNER)
    assert (await jobs.get(job.job_id)).status is JobStatus.QUEUED

    fake.status_value = HiggsfieldStatus("completed", video_url="https://cdn.higgsfield.test/out.mp4")
    await service.list_operations(PROJECT, OWNER)

    completed = await jobs.get(job.job_id)
    assert completed.status is JobStatus.COMPLETED
    assert service._assets.assets[job.output_key] == fake.video
    provenance = completed.result.execution_provenance
    assert provenance["completion_path"] == "higgsfield_api"
    assert provenance["provider_path"] == "kling-video/v3.0/std/image-to-video"
    assert provenance["provider_request_id"] == "req-1"
    assert provenance["executing_worker_id"] is None and provenance["lease_fenced"] is False
    assert "seed" in provenance["not_sent"]
    assert KEY not in json.dumps(provenance)


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [("nsfw", JobStatus.FAILED, False), ("failed", JobStatus.FAILED, True),
     ("canceled", JobStatus.CANCELLED, None)],
)
async def test_terminal_provider_outcomes_are_recorded(status, expected, retryable) -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    fake.status_value = HiggsfieldStatus(status)

    await service.reconcile_project_operations(PROJECT, OWNER)

    record = await jobs.get(job.job_id)
    assert record.status is expected
    if retryable is not None:
        assert record.retryable is retryable


@pytest.mark.parametrize(("permanent", "expected"), [(True, JobStatus.FAILED), (False, JobStatus.QUEUED)])
async def test_only_permanent_download_failures_end_the_job(permanent, expected) -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    fake.status_value = HiggsfieldStatus("completed", video_url="https://cdn.higgsfield.test/out.mp4")

    async def failing_download(url: str, max_bytes: int) -> bytes:
        raise HiggsfieldError("the result download failed", permanent=permanent)

    fake.download = failing_download
    await service.list_operations(PROJECT, OWNER)

    assert (await jobs.get(job.job_id)).status is expected
    assert job.output_key not in service._assets.assets


async def test_an_unreadable_saved_key_fails_the_job_instead_of_retrying_forever() -> None:
    service, jobs, _, _fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    # The secret was rotated away before this key was re-saved.
    service.provider_accounts = ProviderAccounts(service.provider_accounts.store, CredentialCipher(("o" * 40,)))

    await service.list_operations(PROJECT, OWNER)

    record = await jobs.get(job.job_id)
    assert record.status is JobStatus.FAILED and "can no longer be read" in record.failure_message


async def test_a_cancel_while_the_result_downloads_publishes_nothing() -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    fake.status_value = HiggsfieldStatus("completed", video_url="https://cdn.higgsfield.test/out.mp4")

    async def download_then_cancelled(url: str, max_bytes: int) -> bytes:
        await jobs.cancel(job.job_id)
        return _VIDEO

    fake.download = download_then_cancelled
    await service.list_operations(PROJECT, OWNER)

    assert (await jobs.get(job.job_id)).status is JobStatus.CANCELLED
    assert job.output_key not in service._assets.assets


async def test_a_refused_provider_cancel_is_logged(caplog) -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    async def refuse(request_id: str) -> bool:
        return False

    fake.cancel = refuse
    await service.cancel_motion_operation(PROJECT, "idle", job.job_id, OWNER)

    assert (await jobs.get(job.job_id)).status is JobStatus.CANCELLED
    assert "did not confirm cancellation" in caplog.text


async def test_several_ready_results_are_all_collected_in_one_read() -> None:
    service, jobs, _, fake, project = await _service()
    first = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, request_id="r1",
                                          settings=_hosted_settings())
    project = await service.get_project(PROJECT, OWNER)
    second = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, request_id="r2",
                                           settings=_hosted_settings())
    fake.status_value = HiggsfieldStatus("completed", video_url="https://cdn.higgsfield.test/out.mp4")

    await service.list_operations(PROJECT, OWNER)

    assert {(await jobs.get(j.job_id)).status for j in (first, second)} == {JobStatus.COMPLETED}


async def test_provenance_names_the_route_that_was_called_even_if_the_catalog_changed(monkeypatch) -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    monkeypatch.setattr(higgsfield, "MODELS", {})  # a later deploy dropped the model
    fake.status_value = HiggsfieldStatus("completed", video_url="https://cdn.higgsfield.test/out.mp4")

    await service.list_operations(PROJECT, OWNER)

    provenance = (await jobs.get(job.job_id)).result.execution_provenance
    assert provenance["provider_path"] == "kling-video/v3.0/std/image-to-video"


async def test_overlapping_reads_collect_a_result_once() -> None:
    import asyncio

    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    fake.status_value = HiggsfieldStatus("completed", video_url="https://cdn.higgsfield.test/out.mp4")
    downloads: list[str] = []

    async def slow_download(url: str, max_bytes: int) -> bytes:
        downloads.append(url)
        await asyncio.sleep(0.05)
        return _VIDEO

    fake.download = slow_download
    await asyncio.gather(service.list_operations(PROJECT, OWNER), service.list_operations(PROJECT, OWNER))

    assert len(downloads) == 1
    assert (await jobs.get(job.job_id)).status is JobStatus.COMPLETED


async def test_an_unplayable_result_is_never_stored() -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    fake.status_value = HiggsfieldStatus("completed", video_url="https://cdn.higgsfield.test/out.mp4")
    fake.video = b"<html>CDN error</html>"

    await service.list_operations(PROJECT, OWNER)

    record = await jobs.get(job.job_id)
    assert record.status is JobStatus.FAILED and "not a playable video" in record.failure_message
    assert job.output_key not in service._assets.assets


async def test_a_setup_failure_before_submission_fails_the_job_as_not_sent() -> None:
    service, jobs, _, fake, project = await _service()

    async def broken_reservation(*_args):
        raise RuntimeError("database unavailable")

    service.provider_accounts.store.reserve_request = broken_reservation
    with pytest.raises(RuntimeError):
        await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    (job,) = await jobs.list_for_project(PROJECT)
    assert job.status is JobStatus.FAILED and "not sent to Higgsfield" in job.failure_message
    assert fake.submissions == []


async def test_a_cancel_during_submission_cancels_the_provider_request() -> None:
    service, jobs, _, fake, project = await _service()
    original = fake.submit

    async def submit_while_cancelled(path: str, arguments: dict) -> str:
        (pending,) = await jobs.list_for_project(PROJECT)
        await jobs.cancel(pending.job_id)  # another tab cancels mid-flight
        return await original(path, arguments)

    fake.submit = submit_while_cancelled
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    assert (await jobs.get(job.job_id)).status is JobStatus.CANCELLED
    assert fake.cancelled == ["req-1"]


async def test_an_unconfirmed_submission_is_failed_and_never_resubmitted() -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())
    store = service.provider_accounts.store
    # Simulate a crash between reservation and the provider's answer.
    store._requests[job.job_id] = store._requests[job.job_id].model_copy(
        update={"request_id": None, "reserved_at": datetime.now(UTC) - timedelta(minutes=11)}
    )

    await service.list_operations(PROJECT, OWNER)

    record = await jobs.get(job.job_id)
    assert record.status is JobStatus.FAILED
    assert "not resubmitted" in record.failure_message
    assert len(fake.submissions) == 1


@pytest.mark.parametrize(("status_code", "expected"), [(401, JobStatus.FAILED), (402, JobStatus.FAILED),
                                                      (404, JobStatus.FAILED), (422, JobStatus.FAILED),
                                                      (429, JobStatus.QUEUED), (503, JobStatus.QUEUED),
                                                      (None, JobStatus.QUEUED)])
async def test_only_permanent_status_errors_end_the_job(status_code, expected) -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    async def unreadable(request_id: str) -> HiggsfieldStatus:
        raise HiggsfieldError("status failed", status_code=status_code)

    fake.status = unreadable
    await service.list_operations(PROJECT, OWNER)

    record = await jobs.get(job.job_id)
    assert record.status is expected
    if expected is JobStatus.FAILED:
        assert "may still complete and bill it" in record.failure_message


async def test_a_batch_submits_every_hosted_motion_after_its_ledger_rows_exist() -> None:
    service, jobs, dispatcher, fake, project = await _service()
    project = await service.add_motion(
        PROJECT, project.revision,
        MotionSpecification(motion_id="walk", name="Walk", kind=MotionKind.PRESET,
                            preset=MotionPreset.WALK, loop_policy="loop", required=True),
        OWNER,
    )
    submitted_paths: list[str] = []
    original = fake.submit

    async def submit_or_reject(path: str, arguments: dict) -> str:
        if len(submitted_paths) == 1:
            raise HiggsfieldError("Higgsfield answered 400: bad duration", status_code=400)
        submitted_paths.append(path)
        return await original(path, arguments)

    fake.submit = submit_or_reject
    queued, failed = await service.generate_motions(
        PROJECT, project.revision, ("idle", "walk"), OWNER, "batch-1", _hosted_settings()
    )

    assert len(queued) == 1 and len(failed) == 1
    assert failed[0].code == "dispatch_failed" and "bad duration" in failed[0].message
    assert dispatcher.job_ids == []
    statuses = sorted(job.status.value for job in await jobs.list_for_project(PROJECT))
    assert statuses == ["failed", "queued"]


async def test_cancelling_a_hosted_motion_cancels_the_provider_request() -> None:
    service, jobs, _, fake, project = await _service()
    job = await service.generate_motion(PROJECT, project.revision, "idle", OWNER, settings=_hosted_settings())

    await service.cancel_motion_operation(PROJECT, "idle", job.job_id, OWNER)

    assert fake.cancelled == ["req-1"]
    assert (await jobs.get(job.job_id)).status is JobStatus.CANCELLED


async def test_discovery_offers_higgsfield_only_once_a_key_is_saved() -> None:
    service, *_ = await _service(with_key=False)

    before = (await service.hosted_provider_options(OWNER))[HIGGSFIELD]
    await service.provider_accounts.save_key(OWNER, HIGGSFIELD, KEY)
    after = (await service.hosted_provider_options(OWNER))[HIGGSFIELD]

    assert before["configured"] is False and before["settings"] is None
    assert after["configured"] is True and after["key_hint"] == "…9876"
    assert after["settings"]["provider_parameters"] == {"execution_provider": "higgsfield",
                                                        "higgsfield_model": "kling-3-std"}
    assert KEY not in json.dumps(after)


# --- HTTP client ---------------------------------------------------------------


async def test_client_speaks_the_documented_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url.endswith("/files/generate-upload-url"):
            return httpx.Response(200, json={"upload_url": "https://upload.test/put", "public_url": "https://cdn.test/f.png"})
        if url == "https://upload.test/put":
            return httpx.Response(200)
        if url.endswith("/kling-video/v3.0/std/image-to-video"):
            return httpx.Response(200, json={"request_id": "r-9", "status": "queued"})
        if url.endswith("/requests/r-9/status"):
            return httpx.Response(200, json={"status": "completed", "video": {"url": "https://cdn.test/o.mp4"}})
        if url == "https://cdn.test/o.mp4":
            return httpx.Response(200, content=b"video")
        return httpx.Response(404)

    client = HiggsfieldClient(KEY, transport=httpx.MockTransport(handler))

    assert await client.upload(b"png", "image/png") == "https://cdn.test/f.png"
    assert await client.submit("kling-video/v3.0/std/image-to-video", {"prompt": "p"}) == "r-9"
    status = await client.status("r-9")
    assert status.terminal and status.video_url == "https://cdn.test/o.mp4"
    assert await client.download("https://cdn.test/o.mp4", 100) == b"video"
    api_calls = [r for r in seen if r.url.host == "api.higgsfield.ai"]
    assert all(r.headers["Authorization"] == f"Key {KEY}" for r in api_calls)
    # The key goes only to Higgsfield's API, never to upload or CDN hosts.
    assert all("Authorization" not in r.headers for r in seen if r.url.host != "api.higgsfield.ai")


async def test_downloads_follow_https_redirects_and_refuse_anything_but_a_video() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://cdn.test/redirect.mp4":
            return httpx.Response(302, headers={"Location": "https://cdn2.test/final.mp4"})
        if url == "https://cdn2.test/final.mp4":
            return httpx.Response(200, content=b"video")
        if url == "https://cdn.test/downgrade.mp4":
            return httpx.Response(302, headers={"Location": "http://cdn2.test/final.mp4"})
        if url == "http://cdn2.test/final.mp4":
            return httpx.Response(200, content=b"video")
        if url == "https://cdn.test/empty.mp4":
            return httpx.Response(200, content=b"")
        if url == "https://cdn.test/busy.mp4":
            return httpx.Response(503)
        return httpx.Response(404)

    client = HiggsfieldClient(KEY, transport=httpx.MockTransport(handler))

    assert await client.download("https://cdn.test/redirect.mp4", 100) == b"video"
    with pytest.raises(HiggsfieldError) as too_large:
        await client.download("https://cdn.test/redirect.mp4", 2)
    assert too_large.value.permanent
    for url, message, permanent in (("https://cdn.test/downgrade.mp4", "download failed", True),
                                    ("https://cdn.test/empty.mp4", "empty", True),
                                    ("https://cdn.test/gone.mp4", "download failed", True),
                                    ("https://cdn.test/busy.mp4", "download failed", False)):
        with pytest.raises(HiggsfieldError, match=message) as failure:
            await client.download(url, 100)
        assert failure.value.permanent is permanent


async def test_the_opening_frame_is_never_uploaded_over_plain_http() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"upload_url": "http://upload.test/put", "public_url": "https://cdn.test/f.png"})

    client = HiggsfieldClient(KEY, transport=httpx.MockTransport(handler))

    with pytest.raises(HiggsfieldError, match="non-HTTPS upload grant"):
        await client.upload(b"png", "image/png")


async def test_client_errors_never_echo_the_key_and_downloads_are_bounded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.test":
            return httpx.Response(200, content=b"x" * 50)
        return httpx.Response(401, json={"detail": "bad key"})

    client = HiggsfieldClient(KEY, transport=httpx.MockTransport(handler))

    with pytest.raises(HiggsfieldError) as rejected:
        await client.submit("kling-video/v3.0/std/image-to-video", {})
    assert "rejected the saved API key" in str(rejected.value) and KEY not in str(rejected.value)
    with pytest.raises(HiggsfieldError, match="size limit"):
        await client.download("https://cdn.test/big.mp4", 10)
    with pytest.raises(HiggsfieldError, match="non-HTTPS"):
        await client.download("http://cdn.test/o.mp4", 100)


# --- routes ----------------------------------------------------------------------


async def test_a_key_can_be_deleted_while_the_credential_secret_is_unavailable() -> None:
    store = MemoryProviderAccountStore()
    await ProviderAccounts(store, CredentialCipher((SECRET,))).save_key(OWNER, HIGGSFIELD, KEY)
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore(),
                            jobs=MemoryJobStore(), provider_accounts=ProviderAccounts(store, None))
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    client = TestClient(create_app(runtime, service_role=ServiceRole.API),
                        headers={"X-Studio-Actor-Id": OWNER})

    assert client.delete("/studio/providers/higgsfield").status_code == 200
    assert await store.get_credential(OWNER, HIGGSFIELD) is None


def test_a_studio_api_key_cannot_set_or_remove_a_provider_key() -> None:
    from magic_video_editor.studio.api_keys import MemoryStudioApiKeyStore

    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore(),
                            jobs=MemoryJobStore(), provider_accounts=_accounts())
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service,
                              studio_api_keys=MemoryStudioApiKeyStore())
    app = create_app(runtime, service_role=ServiceRole.API)
    browser = TestClient(app, headers={"X-Studio-Actor-Id": OWNER})
    bearer = TestClient(app, headers={
        "Authorization": f"Bearer {browser.post('/studio/api-keys', json={'name': 'agent'}).json()['key']}"})

    assert bearer.get("/studio/providers").status_code == 200
    # Management routes ignore the bearer header; without a browser identity they refuse.
    assert bearer.put("/studio/providers/higgsfield", json={"api_key": KEY}).status_code in {401, 403, 422}
    assert bearer.delete("/studio/providers/higgsfield").status_code in {401, 403, 422}
    assert bearer.get("/studio/providers").json()[0]["configured"] is False


def test_routes_save_describe_and_forget_a_key_without_returning_it() -> None:
    accounts = _accounts()
    service = StudioService(store=MemoryStudioStore(), assets=MemoryAssetStore(),
                            jobs=MemoryJobStore(), provider_accounts=accounts)
    runtime = InMemoryRuntime(MemoryJobStore(), MemoryAssetStore(), studio_service=service)
    client = TestClient(create_app(runtime, service_role=ServiceRole.API),
                        headers={"X-Studio-Actor-Id": OWNER})

    saved = client.put("/studio/providers/higgsfield", json={"api_key": KEY})
    listed = client.get("/studio/providers")
    config = client.get("/studio/generation-config")
    invalid = client.put("/studio/providers/higgsfield", json={"api_key": "has a space"})
    unknown = client.put("/studio/providers/other", json={"api_key": KEY})
    deleted = client.delete("/studio/providers/higgsfield")

    assert saved.status_code == 200 and saved.json()["configured"] is True
    assert saved.json()["key_hint"] == "…9876"
    assert listed.json()[0]["provider"] == "higgsfield" and listed.json()[0]["configured"] is True
    assert config.status_code == 200
    assert config.json()["providers"]["higgsfield"]["configured"] is True
    assert config.json()["settings"] is None  # no machine default is invented
    for response in (saved, listed, config):
        assert "very-secret" not in response.text
    assert invalid.status_code == 422
    assert unknown.status_code == 404
    assert deleted.json()["configured"] is False


# --- PostgreSQL ------------------------------------------------------------------


async def test_postgres_store_keeps_ciphertext_and_reserves_each_job_once() -> None:
    import asyncio
    import os
    from uuid import uuid4

    import asyncpg

    from magic_video_editor.studio.provider_accounts import PostgresProviderAccountStore

    url = os.environ.get(
        "MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL",
        "postgresql://postgres:test@127.0.0.1:15432/magic_video_editor",
    )
    try:
        admin = await asyncpg.connect(url, timeout=2)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Local PostgreSQL test service unavailable")
    schema = "provider_test_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(url, server_settings={"search_path": schema})
    try:
        store = PostgresProviderAccountStore(url, pool=pool)
        await store.start()
        await store.start()  # the migration is idempotent
        accounts = ProviderAccounts(store, CredentialCipher((SECRET,)))

        first = await accounts.save_key(OWNER, HIGGSFIELD, KEY)
        replaced = await accounts.save_key(OWNER, HIGGSFIELD, "other-id:other-secret-1111")
        raw = await pool.fetchval(
            "SELECT ciphertext FROM studio_provider_credentials WHERE owner_id = $1", OWNER
        )

        assert replaced.created_at == first.created_at and replaced.key_hint == "…1111"
        assert b"other-secret" not in bytes(raw)
        assert await accounts.key_for(OWNER, HIGGSFIELD) == "other-id:other-secret-1111"
        assert [c.provider for c in await accounts.describe(OWNER)] == [HIGGSFIELD]
        assert await accounts.describe("someone-else") == ()

        reservations = await asyncio.gather(*(
            store.reserve_request("job-1", OWNER, HIGGSFIELD, "kling-3-std") for _ in range(5)
        ))
        assert sum(r is not None for r in reservations) == 1
        assert (await store.record_request_id("job-1", "req-1")).request_id == "req-1"
        assert (await store.record_request_id("job-1", "req-2")).request_id == "req-1"
        assert await store.record_request_id("missing", "req-3") is None

        assert await accounts.delete_key(OWNER, HIGGSFIELD) is True
        assert await accounts.delete_key(OWNER, HIGGSFIELD) is False
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()

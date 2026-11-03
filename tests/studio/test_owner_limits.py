"""Per-owner storage and concurrency caps.

Both refuse only the start of new work. The last group here is the important
one: at and over either cap, approving, releasing, exporting and downloading
what already exists must still go through.
"""

from __future__ import annotations

from importlib.resources import files
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.contracts import (
    JobOperation,
    JobStatus,
    JobSubmission,
    VideoJobResult,
)
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.studio.limits import (
    DEFAULT_OWNER_CONCURRENT_JOB_LIMIT,
    DEFAULT_OWNER_STORAGE_LIMIT_BYTES,
    OWNER_CONCURRENT_JOB_LIMIT_ENV,
    OWNER_STORAGE_LIMIT_ENV,
    UNLIMITED,
    MemoryStudioOwnerUsageStore,
    OwnerLimits,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService
from magic_video_editor.task_dispatch import NullTaskDispatcher

OWNER = "owner-1"

_MOTION = {
    "motion_id": "idle",
    "name": "Idle",
    "kind": "preset",
    "preset": "idle",
    "prompt_source": "preset",
}


class _Studio:
    """A signed-in operator with an approved subject, ready to start work."""

    def __init__(self, limits: OwnerLimits) -> None:
        self.assets = MemoryAssetStore()
        self.jobs = MemoryJobStore()
        self.usage = MemoryStudioOwnerUsageStore()
        self.service = StudioService(
            store=MemoryStudioStore(),
            assets=self.assets,
            jobs=self.jobs,
            dispatcher=NullTaskDispatcher(),
            motion_generation_available=True,
            subject_generation_available=True,
            composition_generation_available=True,
            owner_usage=self.usage,
            owner_limits=limits,
        )
        runtime = InMemoryRuntime(self.jobs, self.assets, studio_service=self.service)
        self.client = TestClient(
            create_app(runtime, service_role=ServiceRole.API),
            headers={"X-Studio-Actor-Id": OWNER},
        )
        self.project_id = str(
            self.client.post("/studio/projects", json={"name": "Ember"}).json()[
                "project_id"
            ]
        )
        self.revision = 1

    def _post(self, path: str, body: dict[str, object]) -> object:
        return self.client.post(f"/studio/projects/{self.project_id}{path}", json=body)

    def approve_subject(self) -> None:
        upload = self._post(
            "/subject/uploads",
            {"content_type": "image/png", "expected_revision": self.revision},
        )
        assert upload.status_code == 200, upload.text
        key = upload.json()["key"]
        self.assets.assets[key] = _png_bytes()
        settled = self._post(
            "/subject/settle",
            {"expected_revision": self.revision, "asset_key": key},
        )
        assert settled.status_code == 200, settled.text
        self.revision = settled.json()["revision"]
        approved = self._post("/subject/approve", {"expected_revision": self.revision})
        assert approved.status_code == 200, approved.text
        self.revision = approved.json()["revision"]

    def add_motion(self) -> None:
        added = self._post("/motions", {**_MOTION, "expected_revision": self.revision})
        assert added.status_code == 200, added.text
        self.revision = added.json()["revision"]

    async def fill_running_jobs(self, count: int) -> None:
        for index in range(count):
            await self.jobs.submit(
                JobSubmission(
                    job_id=f"00000000-0000-0000-0000-{index:012d}",
                    idempotency_key=f"filler-{index}",
                    operation=JobOperation.PACKAGE,
                    source_key=f"sources/studio/{self.project_id}/a.png",
                    output_key=f"outputs/studio/{self.project_id}/{index}.mp4",
                    requested_outputs={},
                    project_id=self.project_id,
                    manifest_operation_id=f"op-{index}",
                )
            )

    def generate_motion(self) -> object:
        return self._post(
            "/motions/idle/generate",
            {"expected_revision": self.revision, "request_id": "req1"},
        )

    def request_subject_upload(self) -> object:
        return self._post(
            "/subject/uploads",
            {"content_type": "image/png", "expected_revision": self.revision},
        )


def _png_bytes() -> bytes:
    image = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
    image.paste(Image.new("RGBA", (624, 624), (255, 64, 64, 255)), (200, 200))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- configuration -------------------------------------------------------------


def test_limits_default_to_the_documented_internal_values() -> None:
    limits = OwnerLimits.from_env({})

    assert limits.storage_limit_bytes == DEFAULT_OWNER_STORAGE_LIMIT_BYTES
    assert limits.storage_limit_bytes == 50 * 1024**3
    assert limits.concurrent_job_limit == DEFAULT_OWNER_CONCURRENT_JOB_LIMIT == 8


def test_limits_read_their_env_vars() -> None:
    limits = OwnerLimits.from_env(
        {
            OWNER_STORAGE_LIMIT_ENV: "1024",
            OWNER_CONCURRENT_JOB_LIMIT_ENV: "2",
        }
    )

    assert limits.storage_limit_bytes == 1024
    assert limits.concurrent_job_limit == 2


@pytest.mark.parametrize("raw", ["", "   ", "not-a-number", "-5"])
def test_an_unusable_env_value_falls_back_to_the_default(raw: str) -> None:
    limits = OwnerLimits.from_env({OWNER_CONCURRENT_JOB_LIMIT_ENV: raw})

    assert limits.concurrent_job_limit == DEFAULT_OWNER_CONCURRENT_JOB_LIMIT


def test_zero_means_unlimited() -> None:
    limits = OwnerLimits.from_env(
        {OWNER_STORAGE_LIMIT_ENV: "0", OWNER_CONCURRENT_JOB_LIMIT_ENV: "0"}
    )

    assert limits.storage_limit_bytes == UNLIMITED
    assert limits.concurrent_job_limit == UNLIMITED


def test_the_usage_migration_is_idempotent_and_add_only() -> None:
    """A migration that adds then drops a column burns a catalog slot on every
    start; ~320 starts exhausted Postgres's 1600-column ceiling on 2026-09-06."""
    sql = (
        files("magic_video_editor.migrations")
        .joinpath("014_studio_owner_usage.sql")
        .read_text()
        .upper()
    )

    assert "CREATE TABLE IF NOT EXISTS STUDIO_OWNER_USAGE" in sql
    assert "DROP " not in sql
    assert "ALTER " not in sql


# --- storage accounting --------------------------------------------------------


async def test_stored_bytes_accumulate_and_floor_at_zero() -> None:
    usage = MemoryStudioOwnerUsageStore()

    assert await usage.add_bytes(OWNER, 400) == 400
    assert await usage.add_bytes(OWNER, 600) == 1000
    assert await usage.add_bytes(OWNER, -2000) == 0
    assert await usage.stored_bytes(OWNER) == 0


async def test_settling_a_subject_charges_the_owner_for_its_bytes() -> None:
    studio = _Studio(OwnerLimits())
    studio.approve_subject()

    assert await studio.usage.stored_bytes(OWNER) == len(_png_bytes())


# --- storage cap ---------------------------------------------------------------


async def test_under_the_storage_cap_new_work_is_allowed() -> None:
    studio = _Studio(OwnerLimits(storage_limit_bytes=10_000_000))
    await studio.usage.add_bytes(OWNER, 9_999_999)

    assert studio.request_subject_upload().status_code == 200


async def test_at_the_storage_cap_new_work_is_refused() -> None:
    studio = _Studio(OwnerLimits(storage_limit_bytes=1_000))
    await studio.usage.add_bytes(OWNER, 1_000)

    response = studio.request_subject_upload()

    assert response.status_code == 507
    assert response.json()["detail"]["code"] == "owner_storage_limit_reached"


async def test_over_the_storage_cap_the_reason_is_plain_language() -> None:
    studio = _Studio(OwnerLimits(storage_limit_bytes=1_000))
    await studio.usage.add_bytes(OWNER, 5_000)

    message = studio.request_subject_upload().json()["detail"]["message"]

    assert "storage" in message
    assert "Delete a project" in message


async def test_an_unlimited_storage_cap_never_refuses() -> None:
    studio = _Studio(OwnerLimits(storage_limit_bytes=UNLIMITED))
    await studio.usage.add_bytes(OWNER, 10**15)

    assert studio.request_subject_upload().status_code == 200


# --- concurrency cap -----------------------------------------------------------


async def test_under_the_concurrency_cap_a_generation_starts() -> None:
    studio = _Studio(OwnerLimits(concurrent_job_limit=3))
    studio.approve_subject()
    studio.add_motion()
    await studio.fill_running_jobs(2)

    assert studio.generate_motion().status_code == 200


async def test_at_the_concurrency_cap_a_generation_is_refused_with_429() -> None:
    studio = _Studio(OwnerLimits(concurrent_job_limit=3))
    studio.approve_subject()
    studio.add_motion()
    await studio.fill_running_jobs(3)

    response = studio.generate_motion()

    assert response.status_code == 429
    detail = response.json()["detail"]
    assert detail["code"] == "owner_concurrent_job_limit_reached"
    assert detail["message"] == "You have 3 generations running. Wait for one to finish."


async def test_over_the_concurrency_cap_a_generation_is_still_refused() -> None:
    studio = _Studio(OwnerLimits(concurrent_job_limit=2))
    studio.approve_subject()
    studio.add_motion()
    await studio.fill_running_jobs(5)

    assert studio.generate_motion().status_code == 429


async def test_an_unlimited_concurrency_cap_never_refuses() -> None:
    studio = _Studio(OwnerLimits(concurrent_job_limit=UNLIMITED))
    studio.approve_subject()
    studio.add_motion()
    await studio.fill_running_jobs(50)

    assert studio.generate_motion().status_code == 200


async def test_a_finished_job_frees_a_slot() -> None:
    studio = _Studio(OwnerLimits(concurrent_job_limit=1))
    studio.approve_subject()
    studio.add_motion()
    await studio.fill_running_jobs(1)
    assert studio.generate_motion().status_code == 429

    filler = "00000000-0000-0000-0000-000000000000"
    claimed = await studio.jobs.claim(filler)
    assert claimed is not None
    await studio.jobs.complete(
        filler, claimed.claim_token, VideoJobResult(output_key="outputs/a.mp4")
    )
    assert studio.jobs.records[filler].status is JobStatus.COMPLETED

    assert studio.generate_motion().status_code == 200


async def test_a_batch_dispatch_stops_at_the_cap_instead_of_walking_past_it() -> None:
    studio = _Studio(OwnerLimits(concurrent_job_limit=2))
    studio.approve_subject()
    for motion_id in ("idle", "walk", "run"):
        added = studio.client.post(
            f"/studio/projects/{studio.project_id}/motions",
            json={
                **_MOTION,
                "motion_id": motion_id,
                "preset": motion_id,
                "name": motion_id.title(),
                "expected_revision": studio.revision,
            },
        )
        assert added.status_code == 200, added.text
        studio.revision = added.json()["revision"]

    response = studio.client.post(
        f"/studio/projects/{studio.project_id}/motions/generate-batch",
        json={
            "expected_revision": studio.revision,
            "request_id": "req1",
            "motion_ids": ["idle", "walk", "run"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["queued"]) == 2
    assert [item["code"] for item in body["failed"]] == [
        "owner_concurrent_job_limit_reached"
    ]


# --- warnings, not blockers ----------------------------------------------------


async def test_existing_work_is_never_blocked_by_either_cap() -> None:
    """The operator's rule: a cap refuses new work, never what already exists."""
    studio = _Studio(OwnerLimits(storage_limit_bytes=10, concurrent_job_limit=1))
    studio.approve_subject()
    studio.add_motion()
    subject_key = studio.client.get(
        f"/studio/projects/{studio.project_id}"
    ).json()["manifest"]["subject"]["revisions"][0]["artifact_key"]
    await studio.usage.add_bytes(OWNER, 10_000_000)
    await studio.fill_running_jobs(50)

    preview = studio.client.post(
        f"/studio/projects/{studio.project_id}/assets/preview",
        json={"asset_key": subject_key},
    )
    download = studio.client.post(
        f"/studio/projects/{studio.project_id}/assets/download",
        json={"asset_key": subject_key},
    )
    listed = studio.client.get(f"/studio/projects/{studio.project_id}")
    history = studio.client.get(f"/studio/projects/{studio.project_id}/history")
    releases = studio.client.get(f"/studio/projects/{studio.project_id}/releases")

    assert preview.status_code == 200
    assert download.status_code == 200
    assert listed.status_code == 200
    assert history.status_code == 200
    assert releases.status_code == 200


# --- surfaced on the API -------------------------------------------------------


async def test_the_session_route_reports_usage_and_limits() -> None:
    studio = _Studio(OwnerLimits(storage_limit_bytes=2_048, concurrent_job_limit=4))
    await studio.usage.add_bytes(OWNER, 512)
    await studio.fill_running_jobs(2)

    usage = studio.client.get("/studio/auth/session").json()["usage"]

    assert usage == {
        "stored_bytes": 512,
        "storage_limit_bytes": 2_048,
        "running_jobs": 2,
        "concurrent_job_limit": 4,
        "account_tier": "invited",
    }


async def test_at_the_storage_cap_staging_a_setup_reference_is_refused_with_507() -> None:
    import base64

    studio = _Studio(OwnerLimits(storage_limit_bytes=1_000))
    await studio.usage.add_bytes(OWNER, 1_000)

    response = studio.client.post(
        f"/studio/projects/{studio.project_id}/setup/references",
        json={
            "content_base64": base64.b64encode(_png_bytes()).decode(),
            "content_type": "image/png",
        },
        headers={"X-Studio-Setup-Version": "1"},
    )

    assert response.status_code == 507
    assert response.json()["detail"]["code"] == "owner_storage_limit_reached"

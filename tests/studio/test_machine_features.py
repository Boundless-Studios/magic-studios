"""Subjects go to the route an owner's machines actually serve, or are refused."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from outbound_gpu_worker_pool.contracts import GpuTelemetry
from outbound_gpu_worker_pool.contracts import JobStatus as PoolJobStatus

from magic_video_editor.contracts import SubjectGenerationRequest
from magic_video_editor.machine_features import (
    SUBJECT_12GB_CAPABILITY_ID,
    SUBJECT_CAPABILITY_ID,
    MachineCannotRun,
    subject_route,
)
from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.plugins.subject_http import TerminalSubjectProviderError
from magic_video_editor.plugins.subject_pool import PoolSubjectGenerator, _pool_job_id

OWNER = "user-1"


@dataclass
class _View:
    worker_id: str
    capability_ids: tuple[str, ...] = ()
    vram_mb: int | None = None
    gpus: tuple[GpuTelemetry, ...] = ()
    tenant_id: str | None = OWNER
    draining: bool = False
    status: SimpleNamespace = field(default_factory=lambda: SimpleNamespace(value="online"))
    last_heartbeat_at: datetime | None = field(default_factory=lambda: datetime.now(UTC))


def _gpu(total_mb: int) -> GpuTelemetry:
    return GpuTelemetry(index=0, name="RTX", utilization_pct=0, memory_used_mb=0, memory_total_mb=total_mb)


CPU = "movie.ffmpeg.process.v3"


@dataclass
class _Pool:
    views: list[_View] = field(default_factory=list)
    submissions: list = field(default_factory=list)

    async def worker_views(self, *, tenant_id=None):
        # The pool's None means every tenant, so the caller must filter.
        return tuple(self.views)

    async def submit(self, submission):
        self.submissions.append(submission)
        return None

    async def get(self, job_id):
        return None

    async def cancel(self, job_id):
        return True


async def test_house_work_keeps_the_16gb_route() -> None:
    pool = _Pool([_View("box", (CPU,), tenant_id=None)])
    assert (await subject_route(pool, None))[0] == SUBJECT_CAPABILITY_ID
    assert (await subject_route(pool, "cluster", house_tenants=frozenset({"cluster"})))[0] == SUBJECT_CAPABILITY_ID


async def test_a_machine_serving_the_16gb_route_keeps_it() -> None:
    pool = _Pool([_View("big", (SUBJECT_CAPABILITY_ID, SUBJECT_12GB_CAPABILITY_ID), vram_mb=24564)])
    capability, requirements = await subject_route(pool, OWNER)
    assert (capability, requirements.min_vram_mb) == (SUBJECT_CAPABILITY_ID, 16_000)


async def test_a_12gb_profile_machine_gets_the_12gb_route() -> None:
    pool = _Pool([_View("mid", (SUBJECT_12GB_CAPABILITY_ID, CPU), vram_mb=12282)])
    capability, requirements = await subject_route(pool, OWNER)
    assert (capability, requirements.min_vram_mb) == (SUBJECT_12GB_CAPABILITY_ID, 12_000)


@pytest.mark.parametrize(
    ("view", "reason"),
    [
        (_View("cpu", (CPU,)), "reports no GPU"),
        (_View("small", (CPU,), gpus=(_gpu(8192),)), "need a 12 GB card and this one has 8 GB"),
        (_View("mid", (CPU,), gpus=(_gpu(12282),)), "12 GB card can make subject images once it is attached with the Subjects 12GB"),
        # Advertising the capability is not enough without the VRAM to lease the job.
        (_View("unreported", (SUBJECT_12GB_CAPABILITY_ID,)), "reports no GPU"),
        (_View("going", (SUBJECT_12GB_CAPABILITY_ID,), vram_mb=12282, draining=True), "shutting down"),
    ],
)
async def test_machines_that_cannot_make_subjects_are_refused_with_the_reason(view, reason) -> None:
    with pytest.raises(MachineCannotRun) as refused:
        await subject_route(_Pool([view]), OWNER, labels={view.worker_id: "Desk PC"})
    assert refused.value.message.startswith("None of your machines can make subject images. Desk PC: ")
    assert reason in refused.value.message


async def test_another_tenants_machine_does_not_count() -> None:
    pool = _Pool([_View("theirs", (SUBJECT_CAPABILITY_ID,), vram_mb=24564, tenant_id="someone-else")])
    with pytest.raises(MachineCannotRun, match="checked in during the last day"):
        await subject_route(pool, OWNER)


def _request() -> SubjectGenerationRequest:
    return SubjectGenerationRequest(
        reference_keys=("sources/studio/p/subject-references/a.png",),
        output_key="outputs/studio/p/subject.png", direction=None,
        idempotency_key="idem", owner_id=OWNER,
    )


class _Completing(_Pool):
    async def get(self, job_id):
        from outbound_gpu_worker_pool.contracts import JobRecord

        if not any(s.job_id == job_id for s in self.submissions):
            return None
        return JobRecord(
            job_id=job_id, idempotency_key="idem", capability_id=SUBJECT_12GB_CAPABILITY_ID,
            status=PoolJobStatus.FAILED, input_keys=(), output_key="x", attempts=1,
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC), error="stop", retryable=False,
        )


async def test_the_generator_submits_to_the_route_the_machine_serves() -> None:
    pool = _Completing([_View("mid", (SUBJECT_12GB_CAPABILITY_ID,), vram_mb=12282)])
    generator = PoolSubjectGenerator(pool, MemoryAssetStore(), poll_interval_seconds=0)
    with pytest.raises(TerminalSubjectProviderError, match="stop"):
        await generator.generate_subject(_request())
    [submission] = pool.submissions
    assert submission.capability_id == SUBJECT_12GB_CAPABILITY_ID
    assert submission.requirements.min_vram_mb == 12_000
    assert submission.payload["steps"] == 20


async def test_the_generator_fails_with_the_reason_instead_of_queueing() -> None:
    pool = _Pool([_View("cpu", (CPU,))])
    generator = PoolSubjectGenerator(pool, MemoryAssetStore(), poll_interval_seconds=0)
    with pytest.raises(TerminalSubjectProviderError, match="reports no GPU"):
        await generator.generate_subject(_request())
    assert pool.submissions == []


def test_a_12gb_machine_lists_what_it_can_run_and_why_not_the_rest() -> None:
    from magic_video_editor.machine_features import features_for

    view = _View("mid", (SUBJECT_12GB_CAPABILITY_ID, CPU), vram_mb=12282)
    rows = {row["key"]: row for row in features_for(view)}

    assert rows["subjects"] == {"key": "subjects", "label": "subject images", "available": True,
                                "capability_id": SUBJECT_12GB_CAPABILITY_ID, "reason": None}
    assert rows["motions"]["reason"] == "motions need a 16 GB card and this one has 12 GB"
    assert rows["movie_takes"]["reason"] == "movie takes need a 24 GB card and this one has 12 GB"
    assert rows["movie_processing"]["available"] is True


def test_a_machine_without_ffmpeg_is_told_how_to_process_movies() -> None:
    from magic_video_editor.machine_features import features_for

    rows = {row["key"]: row for row in features_for(_View("bare", (), gpus=(_gpu(24564),)))}
    assert rows["movie_processing"]["reason"].startswith("it has no FFmpeg")
    assert rows["motions"]["reason"] == (
        "its 24 GB card can make motions once it is attached with the H3 16GB configuration")


def _offline(view: _View) -> _View:
    view.status = SimpleNamespace(value="offline")
    view.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    return view


async def test_an_offline_16gb_machine_does_not_pull_work_off_an_online_12gb_one() -> None:
    pool = _Pool([
        _offline(_View("big", (SUBJECT_CAPABILITY_ID,), vram_mb=24564)),
        _View("mid", (SUBJECT_12GB_CAPABILITY_ID,), vram_mb=12282),
    ])
    assert (await subject_route(pool, OWNER))[0] == SUBJECT_12GB_CAPABILITY_ID
    # A stale heartbeat counts as offline even before the pool says so.
    pool.views = [_View("big", (SUBJECT_CAPABILITY_ID,), vram_mb=24564,
                        last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=3)),
                  _View("mid", (SUBJECT_12GB_CAPABILITY_ID,), vram_mb=12282)]
    assert (await subject_route(pool, OWNER))[0] == SUBJECT_12GB_CAPABILITY_ID


async def test_only_offline_capable_machines_are_refused_as_offline() -> None:
    pool = _Pool([_offline(_View("big", (SUBJECT_CAPABILITY_ID,), vram_mb=24564))])
    with pytest.raises(MachineCannotRun) as refused:
        await subject_route(pool, OWNER, labels={"big": "Desk PC"})
    assert refused.value.message == (
        "None of your machines can make subject images. Desk PC: it is offline; start its worker.")


async def test_a_removed_16gb_machine_does_not_decide_the_route() -> None:
    pool = _Pool([_View("removed", (SUBJECT_CAPABILITY_ID,), vram_mb=24564),
                  _View("mid", (SUBJECT_12GB_CAPABILITY_ID,), vram_mb=12282)])

    async def revoked(worker_id: str) -> bool:
        return worker_id == "removed"

    assert (await subject_route(pool, OWNER))[0] == SUBJECT_CAPABILITY_ID
    assert (await subject_route(pool, OWNER, revoked=revoked))[0] == SUBJECT_12GB_CAPABILITY_ID


async def test_a_retry_polls_the_job_already_in_the_pool_without_rerouting() -> None:
    # Submitted while the machine served the 12 GB route; it has since gone offline.
    pool = _Completing([_offline(_View("mid", (SUBJECT_12GB_CAPABILITY_ID,), vram_mb=12282))])
    generator = PoolSubjectGenerator(pool, MemoryAssetStore(), poll_interval_seconds=0)
    pool.submissions.append(SimpleNamespace(job_id=_pool_job_id("idem")))
    with pytest.raises(TerminalSubjectProviderError, match="stop"):
        await generator.generate_subject(_request())
    assert len(pool.submissions) == 1

"""Inactivity expiry: unreleased work nobody opens for the retention period is
deleted by the hourly sweep; released or busy work is kept; existing work gets a
full period when first seen; only the scheduler may run the sweep."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import magic_video_editor.app as app_module
from magic_video_editor.app import ServiceRole, create_app
from magic_video_editor.contracts import JobOperation, JobSubmission
from magic_video_editor.memory import InMemoryRuntime, MemoryAssetStore, MemoryJobStore
from magic_video_editor.movies.models import Release, Shot, Take
from magic_video_editor.movies.service import MovieService
from magic_video_editor.movies.store import MemoryMovieStore
from magic_video_editor.retention import (
    MOVIE,
    PROJECT,
    ItemExpiring,
    MemoryActivityStore,
    Retention,
    RetentionPolicy,
    RetentionSweeper,
)
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService
from magic_video_editor.task_dispatch import NullTaskDispatcher

OWNER = "owner-1"
START = datetime(2026, 1, 1, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, days: float) -> None:
        self.now += timedelta(days=days)


def _retention(clock: _Clock) -> Retention:
    return Retention(RetentionPolicy(days=90, warning_days=14), MemoryActivityStore(), clock=clock)


def test_the_policy_reads_its_env_and_zero_turns_it_off() -> None:
    assert RetentionPolicy.from_env({}) == RetentionPolicy(90, 14)
    assert RetentionPolicy.from_env({"MAGIC_VIDEO_EDITOR_RETENTION_DAYS": "0"}).enabled is False
    with pytest.raises(RuntimeError):
        RetentionPolicy.from_env({"MAGIC_VIDEO_EDITOR_RETENTION_DAYS": "10",
                                  "MAGIC_VIDEO_EDITOR_RETENTION_WARNING_DAYS": "20"})


async def test_the_sweep_gives_new_items_a_full_period_then_expires_idle_ones() -> None:
    clock = _Clock()
    retention = _retention(clock)
    items = {"a": OWNER, "b": OWNER}
    deleted: list[str] = []

    async def lister():
        return list(items.items())

    async def expire(item_id: str) -> bool:
        deleted.append(item_id)
        items.pop(item_id)
        return True

    sweeper = RetentionSweeper(retention, listers={PROJECT: lister}, expirers={PROJECT: expire})

    await sweeper.sweep()  # first sight: enrolled, nothing deleted
    clock.advance(89)
    await retention.touch(PROJECT, "b", OWNER)  # b is opened
    clock.advance(2)
    result = await sweeper.sweep()

    assert deleted == ["a"]
    assert result.expired == {PROJECT: ["a"]}
    assert await retention.activity.tracked(PROJECT) == {"b": START + timedelta(days=89)}


async def test_kept_items_wait_a_full_period_and_vanished_items_are_forgotten() -> None:
    clock = _Clock()
    retention = _retention(clock)
    items = {"released": OWNER, "gone": OWNER}
    calls: list[str] = []

    async def lister():
        return list(items.items())

    async def expire(item_id: str) -> bool:
        calls.append(item_id)
        return False

    sweeper = RetentionSweeper(retention, listers={PROJECT: lister}, expirers={PROJECT: expire})
    await sweeper.sweep()
    items.pop("gone")
    clock.advance(91)

    first = await sweeper.sweep()
    clock.advance(1)
    await sweeper.sweep()

    assert first.kept == 1
    assert calls == ["released"]  # not re-checked an hour, or a day, later
    assert set(await retention.activity.tracked(PROJECT)) == {"released"}


async def test_a_failed_delete_is_counted_and_the_sweep_goes_on() -> None:
    clock = _Clock()
    retention = _retention(clock)
    items = {"bad": OWNER, "good": OWNER}

    async def lister():
        return list(items.items())

    async def expire(item_id: str) -> bool:
        if item_id == "bad":
            raise RuntimeError("storage down")
        return True

    sweeper = RetentionSweeper(retention, listers={PROJECT: lister}, expirers={PROJECT: expire})
    await sweeper.sweep()
    clock.advance(91)

    result = await sweeper.sweep()

    assert (result.failed, result.expired) == (1, {PROJECT: ["good"]})


class _Studio:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.retention = _retention(self.clock)
        self.assets = MemoryAssetStore()
        self.jobs = MemoryJobStore()
        self.store = MemoryStudioStore()
        self.service = StudioService(store=self.store, assets=self.assets, jobs=self.jobs,
                                     dispatcher=NullTaskDispatcher(), retention=self.retention)
        self.runtime = InMemoryRuntime(self.jobs, self.assets, studio_service=self.service)
        self.client = TestClient(create_app(self.runtime, service_role=ServiceRole.API),
                                 headers={"X-Studio-Actor-Id": OWNER})

    def create(self, name: str) -> str:
        return self.client.post("/studio/projects", json={"name": name}).json()["project_id"]


def test_the_project_list_warns_before_unopened_work_expires_and_opening_clears_it() -> None:
    studio = _Studio()
    project_id = studio.create("Ember")
    studio.clock.advance(75)
    [summary] = studio.client.get("/studio/projects").json()
    assert summary["expires_at"] is None  # not yet inside the 14-day warning

    studio.clock.advance(5)
    [summary] = studio.client.get("/studio/projects").json()
    assert datetime.fromisoformat(summary["expires_at"]) == START + timedelta(days=90)

    assert studio.client.get(f"/studio/projects/{project_id}").status_code == 200
    [summary] = studio.client.get("/studio/projects").json()
    assert summary["expires_at"] is None


def test_an_idle_project_is_deleted_and_its_stale_work_cancelled() -> None:
    studio = _Studio()
    idle, kept = studio.create("Idle"), studio.create("Kept")
    job_id = "00000000-0000-0000-0000-0000000000b1"
    asyncio.run(studio.jobs.submit(JobSubmission(
        job_id=job_id, idempotency_key="k", operation=JobOperation.PACKAGE,
        source_key=f"sources/studio/{idle}/a.png", output_key=f"outputs/studio/{idle}/a.mp4",
        requested_outputs={}, project_id=idle, manifest_operation_id="op",
    )))
    studio.assets.assets[f"outputs/studio/{idle}/take.mp4"] = b"x"

    assert asyncio.run(studio.service.expire_project(idle)) is True
    assert [key for key in studio.assets.assets if idle in key] == []
    assert asyncio.run(studio.jobs.list_for_project(idle)) == ()
    assert [p["project_id"] for p in studio.client.get("/studio/projects").json()] == [kept]


def test_a_released_project_is_kept() -> None:
    from types import SimpleNamespace

    studio = _Studio()
    project_id = studio.create("Shipped")

    async def releases(_project_id):
        return (SimpleNamespace(),)

    studio.store.list_releases = releases  # type: ignore[method-assign]

    assert asyncio.run(studio.service.expire_project(project_id)) is False


class _Pool:
    async def submit(self, request):
        return None

    async def cancel(self, job_id: str) -> bool:
        return True


def test_a_released_movie_is_kept_and_idle_ones_are_deleted_even_with_stale_takes() -> None:
    clock = _Clock()
    service = MovieService(MemoryMovieStore(), MemoryAssetStore(), _Pool(), retention=_retention(clock))

    async def run():
        idle = await service.create(OWNER, "Idle")
        released = await service.create(OWNER, "Released")
        released.releases = [Release(release_id="00000000-0000-0000-0000-000000000001",
                                     name="v1", asset_key="outputs/movies/x/r.mp4", cut_digest="d", source_revision=1)]
        await service.save(released, released.revision)
        busy = await service.create(OWNER, "Busy")
        busy.shots = [Shot(shot_id="a", name="A", prompt="p", takes=[Take(
            take_id="t", request_id="r", job_id="j", prompt="p", asset_key="k", duration_seconds=2,
            status="running")])]
        await service.save(busy, busy.revision)
        return [await service.expire(m.movie_id) for m in (idle, released, busy)], idle.movie_id

    outcome, idle_id = asyncio.run(run())

    assert outcome == [True, False, True]
    assert asyncio.run(service.store.get(idle_id)) is None
    clock.advance(80)
    listed = asyncio.run(service.list_with_expiry(OWNER, "movie"))
    assert {m.name: expires is None for m, expires in listed} == {"Released": True}


def test_only_the_scheduler_can_run_the_sweep(monkeypatch) -> None:
    studio = _Studio()
    sweeper = RetentionSweeper(studio.retention, listers={}, expirers={})
    route = "/internal/retention/sweep"

    assert studio.client.post(route).status_code == 404  # not configured
    studio.runtime.retention_sweeper = sweeper
    studio.runtime.retention_sweep_caller = ("aud", "scheduler@project.iam.gserviceaccount.com")

    async def verified(authorization, audience):
        assert audience == "aud"
        return {"Bearer good": "scheduler@project.iam.gserviceaccount.com",
                "Bearer other": "someone@example.com"}.get(authorization)

    monkeypatch.setattr(app_module, "_verified_google_caller", verified)

    assert studio.client.post(route).status_code == 403
    assert studio.client.post(route, headers={"Authorization": "Bearer other"}).status_code == 403
    ran = studio.client.post(route, headers={"Authorization": "Bearer good"})
    assert ran.status_code == 200
    assert ran.json()["failed"] == 0


def test_the_movie_kind_constant_matches_the_activity_table() -> None:
    assert (PROJECT, MOVIE) == ("project", "movie")


async def test_an_item_opened_during_the_sweep_is_not_deleted() -> None:
    clock = _Clock()
    retention = _retention(clock)
    items = {"a": OWNER, "b": OWNER}
    deleted: list[str] = []

    async def lister():
        return list(items.items())

    async def expire(item_id: str) -> bool:
        deleted.append(item_id)
        # While "a" is being deleted, its owner opens "b".
        clock.advance(0.01)
        await retention.activity.touch(PROJECT, "b", OWNER, clock())
        return True

    sweeper = RetentionSweeper(retention, listers={PROJECT: lister}, expirers={PROJECT: expire})
    await sweeper.sweep()
    clock.advance(91)

    await sweeper.sweep()

    assert deleted == ["a"]


async def test_an_owner_cannot_open_an_item_while_the_sweep_deletes_it() -> None:
    clock = _Clock()
    retention = _retention(clock)
    items = {"a": OWNER, "b": OWNER}
    refused: list[str] = []

    async def lister():
        return list(items.items())

    async def expire(item_id: str) -> bool:
        # The owner opens the item while it is being deleted: the open is
        # refused rather than succeeding on work that is about to vanish.
        clock.advance(0.01)
        try:
            await retention.touch(PROJECT, item_id, OWNER)
        except ItemExpiring:
            refused.append(item_id)
        if item_id == "b":
            raise RuntimeError("cannot delete")
        del items[item_id]
        return True

    sweeper = RetentionSweeper(retention, listers={PROJECT: lister}, expirers={PROJECT: expire})
    await sweeper.sweep()
    clock.advance(91)

    result = await sweeper.sweep()

    assert refused == ["a", "b"]
    assert result.expired == {PROJECT: ["a"]} and result.failed == 1
    # The deletion of "b" failed, so its owner can open it again.
    await retention.touch(PROJECT, "b", OWNER)
    assert (await retention.activity.last_active(PROJECT, ["b"]))["b"] == clock()


async def test_a_duplicate_sweep_never_deletes_the_same_item_twice() -> None:
    clock = _Clock()
    retention = _retention(clock)
    items = {"a": OWNER}
    deleted: list[str] = []

    async def lister():
        return list(items.items())

    async def expire(item_id: str) -> bool:
        deleted.append(item_id)
        if len(deleted) == 1:
            # A duplicate scheduler delivery runs while the first is deleting "a".
            await sweeper.sweep()
        items.pop(item_id, None)
        return True

    sweeper = RetentionSweeper(retention, listers={PROJECT: lister}, expirers={PROJECT: expire})
    await sweeper.sweep()
    clock.advance(91)

    await sweeper.sweep()

    assert deleted == ["a"]


async def test_a_claim_left_by_a_dead_sweep_is_retaken_once_its_lease_lapses() -> None:
    clock = _Clock()
    activity = MemoryActivityStore()
    await activity.enrol(PROJECT, [("a", OWNER)], clock() - timedelta(days=100))
    cutoff = clock() - timedelta(days=90)

    assert await activity.claim(PROJECT, "a", cutoff, clock())
    assert not await activity.claim(PROJECT, "a", cutoff, clock() + timedelta(minutes=29))
    assert await activity.claim(PROJECT, "a", cutoff, clock() + timedelta(minutes=31))


def test_opening_a_project_being_deleted_for_inactivity_is_gone() -> None:
    studio = _Studio()
    project_id = studio.create("Ember")
    studio.clock.advance(91)
    cutoff = studio.clock() - timedelta(days=90)
    assert asyncio.run(studio.retention.activity.claim(PROJECT, project_id, cutoff, studio.clock()))

    opened = studio.client.get(f"/studio/projects/{project_id}")

    assert opened.status_code == 410, opened.text
    assert opened.json()["detail"]["code"] == "item_expiring"


async def test_items_that_keep_failing_do_not_starve_the_ones_behind_them() -> None:
    clock = _Clock()
    retention = _retention(clock)
    items = {"bad-1": OWNER, "bad-2": OWNER}
    await retention.activity.enrol(PROJECT, list(items.items()), clock() - timedelta(days=200))
    items["good"] = OWNER
    await retention.activity.enrol(PROJECT, [("good", OWNER)], clock() - timedelta(days=100))
    attempts: list[str] = []

    async def lister():
        return list(items.items())

    async def expire(item_id: str) -> bool:
        attempts.append(item_id)
        if item_id.startswith("bad"):
            raise RuntimeError("cannot delete")
        return True

    sweeper = RetentionSweeper(retention, listers={PROJECT: lister}, expirers={PROJECT: expire})

    # One attempt per sweep: each failure is pushed back a day, so the good item
    # is reached on the third sweep instead of never.
    results = [await sweeper.sweep(limit=1) for _ in range(3)]

    assert attempts == ["bad-1", "bad-2", "good"]
    assert results[-1].expired == {PROJECT: ["good"]}
    clock.advance(1.01)
    await sweeper.sweep(limit=1)
    assert attempts[-1] == "bad-1"  # retried a day later


def test_deleting_a_project_or_movie_drops_its_activity_row() -> None:
    studio = _Studio()
    project_id = studio.create("Ember")
    movies = MovieService(MemoryMovieStore(), MemoryAssetStore(), _Pool(), retention=studio.retention)

    async def run():
        movie = await movies.create(OWNER, "Heist")
        assert set(await studio.retention.activity.tracked(MOVIE)) == {movie.movie_id}
        await movies.purge(movie)

    asyncio.run(run())
    assert studio.client.request("DELETE", f"/studio/projects/{project_id}", json={"confirm_name": "Ember"}).status_code == 204

    assert asyncio.run(studio.retention.activity.tracked(PROJECT)) == {}
    assert asyncio.run(studio.retention.activity.tracked(MOVIE)) == {}


def test_an_open_that_cannot_be_recorded_fails_the_request() -> None:
    studio = _Studio()
    project_id = studio.create("Ember")
    studio.clock.advance(1)

    async def broken(*args):
        raise RuntimeError("database down")

    studio.retention.activity.touch = broken  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        studio.client.get(f"/studio/projects/{project_id}")


async def test_opens_are_recorded_while_expiry_is_off_so_turning_it_on_is_safe() -> None:
    clock = _Clock()
    activity = MemoryActivityStore()
    off = Retention(RetentionPolicy(days=0), activity, clock=clock)
    await activity.touch(PROJECT, "p", OWNER, clock() - timedelta(days=200))

    await off.touch(PROJECT, "p", OWNER)

    assert await activity.tracked(PROJECT) == {"p": clock()}


def test_creating_a_project_succeeds_even_if_its_activity_cannot_be_recorded() -> None:
    studio = _Studio()

    async def broken(*args):
        raise RuntimeError("database down")

    studio.retention.activity.touch = broken  # type: ignore[method-assign]

    created = studio.client.post("/studio/projects", json={"name": "Ember"})

    assert created.status_code == 200, created.text


def test_an_abandoned_project_that_cannot_be_read_still_expires() -> None:
    studio = _Studio()
    project_id = studio.create("Broken")
    stored = asyncio.run(studio.store.get(project_id))
    corrupt = stored.manifest.model_dump(mode="json")
    corrupt["motions"] = [None]
    asyncio.run(studio.store.append_raw(project_id, corrupt))
    studio.assets.assets[f"outputs/studio/{project_id}/take.mp4"] = b"x"

    assert asyncio.run(studio.service.expire_project(project_id)) is True
    assert asyncio.run(studio.store.current_revision_number(project_id)) is None
    assert [key for key in studio.assets.assets if project_id in key] == []


def test_a_delete_still_succeeds_if_its_activity_row_cannot_be_dropped() -> None:
    studio = _Studio()
    project_id = studio.create("Ember")

    async def broken(*args):
        raise RuntimeError("database down")

    studio.retention.activity.forget = broken  # type: ignore[method-assign]

    deleted = studio.client.request("DELETE", f"/studio/projects/{project_id}", json={"confirm_name": "Ember"})

    assert deleted.status_code == 204


def test_an_unreadable_project_is_warned_about_and_gives_its_storage_back() -> None:
    from magic_video_editor.studio.limits import MemoryStudioOwnerUsageStore

    studio = _Studio()
    usage = MemoryStudioOwnerUsageStore()
    studio.service._owner_usage = usage
    project_id = studio.create("Broken")
    stored = asyncio.run(studio.store.get(project_id))
    subject_key = f"sources/studio/{project_id}/subject.png"
    studio.assets.assets[subject_key] = b"x" * 10
    asyncio.run(usage.add_bytes(OWNER, 10))
    readable = stored.manifest.model_dump(mode="json")
    corrupt = dict(readable, motions=[None])

    import magic_video_editor.studio.service as service_module

    original = service_module._accounted_keys
    service_module._accounted_keys = lambda manifest: [subject_key]
    try:
        asyncio.run(studio.store.append_raw(project_id, corrupt))
        studio.clock.advance(80)
        [summary] = studio.client.get("/studio/projects").json()
        assert summary["unreadable"] is True
        assert summary["expires_at"] is not None

        assert asyncio.run(studio.service.expire_project(project_id)) is True
    finally:
        service_module._accounted_keys = original

    assert asyncio.run(usage.stored_bytes(OWNER)) == 0


async def test_releasing_bytes_after_account_deletion_does_not_recreate_usage() -> None:
    from magic_video_editor.studio.limits import MemoryStudioOwnerUsageStore

    usage = MemoryStudioOwnerUsageStore()
    await usage.add_bytes(OWNER, 100)
    await usage.forget_owner(OWNER)  # account deleted while an expiry purge runs

    assert await usage.add_bytes(OWNER, -100) == 0
    assert OWNER not in usage._stored


async def test_the_sweep_runs_extension_purges_and_reports_each() -> None:
    retention = _retention(_Clock())
    calls: list[str] = []

    async def purge() -> int:
        calls.append("purged")
        return 3

    sweeper = RetentionSweeper(retention, listers={}, expirers={}, purges={"access_requests_purged": purge})
    result = await sweeper.sweep()

    assert calls == ["purged"]
    assert result.purged == {"access_requests_purged": 3}

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.movies.service import MovieError, MovieService
from magic_video_editor.movies.store import MemoryMovieStore


class Pool:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    async def submit(self, request):
        self.calls.append(request)
        self.jobs.setdefault(
            str(request.job_id),
            SimpleNamespace(status="queued", output_key=request.output_key),
        )

    async def get(self, job_id):
        return self.jobs.get(str(job_id))


async def setup():
    pool = Pool()
    assets = MemoryAssetStore()
    service = MovieService(MemoryMovieStore(), assets, pool)
    movie = await service.create("owner", "Film")
    shots = [
        {
            "shot_id": s,
            "name": s,
            "prompt": "motion",
            "opening_prompt": "still",
            "duration_seconds": 2,
            "continuation_of": ("a" if s == "c" else None),
        }
        for s in ["a", "b", "c"]
    ]
    movie = await service.story(
        movie.movie_id,
        "owner",
        movie.revision,
        brief="Story",
        style="cinematic",
        shots=shots,
        batch_size=3,
    )
    from io import BytesIO

    from PIL import Image

    b = BytesIO()
    Image.new("RGB", (16, 16)).save(b, "PNG")
    for sid in ["a", "b"]:
        grant = await service.upload(movie.movie_id, "owner", "image/png")
        await assets.write_once(grant["asset_key"], b.getvalue(), "image/png")
        movie = await service.opening(
            movie.movie_id, "owner", grant["revision"], sid, grant["asset_key"]
        )
        movie = await service.approve(movie.movie_id, "owner", movie.revision, sid)
    return service, pool, movie


@pytest.mark.asyncio
async def test_fanout_durable_idempotent_and_waits_for_selection():
    service, pool, movie = await setup()
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a", "b", "c"], "batch1"
    )
    assert len(pool.jobs) == 2
    assert movie.shots[2].takes[0].status == "waiting"
    again = await service.generate(
        movie.movie_id, "owner", 1, ["a", "b", "c"], "batch1"
    )
    assert len(pool.jobs) == 2
    assert again.revision == movie.revision
    with pytest.raises(MovieError):
        await service.get(movie.movie_id, "other")


@pytest.mark.asyncio
async def test_concurrent_same_revision_only_one_batch_spends():
    service, pool, movie = await setup()
    results = await asyncio.gather(
        *(
            service.generate(
                movie.movie_id, "owner", movie.revision, ["a"], f"batch{x}"
            )
            for x in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, MovieError) for r in results) == 1
    assert len(pool.jobs) == 1


class Media:
    def __init__(self, assets):
        self.assets = assets

    async def duration_for_key(self, key):
        return 2.4375

    async def final_frame(self, key):
        from io import BytesIO

        from PIL import Image

        output = BytesIO()
        Image.new("RGB", (768, 448), "blue").save(output, "PNG")
        await self.assets.write_once(key + ".png", output.getvalue(), "image/png")
        return key + ".png"

    async def assemble(self, sources, key, *, soundtracks=()):
        return key


@pytest.mark.asyncio
async def test_selection_unblocks_waiting_take_and_pins_exact_frame():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a", "c"], "batch1"
    )
    a = movie.shots[0].takes[0]
    pool.jobs[a.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    movie = await service.select(
        movie.movie_id, "owner", movie.revision, "a", a.take_id
    )
    movie = await service.reconcile(movie.movie_id, "owner")
    c = movie.shots[2].takes[0]
    assert c.status == "running"
    assert c.predecessor_take_id == a.take_id
    assert c.opening_asset_key == a.asset_key + ".png"
    assert pool.calls[-1].input_keys == (c.provider_frame_key,)
    assert movie.shots[0].takes[0].duration_seconds == 2.4375
    from io import BytesIO

    from PIL import Image

    for submission in pool.calls:
        assert submission.payload["width"] == 768
        assert submission.payload["height"] == 448
        assert (
            submission.payload["width"] % 32 == submission.payload["height"] % 32 == 0
        )
        frame_key = submission.payload["images"]["first_frame"]
        assert submission.input_keys == (frame_key,)
        frame = await service.assets.read_limited(frame_key, 20 * 1024 * 1024)
        assert Image.open(BytesIO(frame)).size == (768, 448)
    # The continuation uses the exact provider-canvas last frame without another crop.
    assert await service.assets.read(c.provider_frame_key) == await service.assets.read(
        c.opening_asset_key
    )


@pytest.mark.asyncio
async def test_editing_story_cancels_waiting_intent_without_spending():
    service, pool, movie = await setup()
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["c"], "batch1"
    )
    shots = [
        {
            k: getattr(s, k)
            for k in [
                "shot_id",
                "name",
                "prompt",
                "opening_prompt",
                "duration_seconds",
                "continuation_of",
            ]
        }
        for s in movie.shots
    ]
    shots[2]["prompt"] = "changed"
    movie = await service.story(
        movie.movie_id,
        "owner",
        movie.revision,
        brief=movie.brief,
        style=movie.style,
        shots=shots,
        batch_size=3,
    )
    assert movie.shots[2].takes[0].status == "cancelled"
    assert not pool.calls


@pytest.mark.asyncio
async def test_release_pins_cut_and_preserves_successful_takes_after_retry():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a", "b"], "first"
    )
    a, b = movie.shots[0].takes[0], movie.shots[1].takes[0]
    pool.jobs[a.job_id].status = "completed"
    pool.jobs[b.job_id].status = "failed"
    movie = await service.reconcile(movie.movie_id, "owner")
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["b"], "retry"
    )
    assert movie.shots[0].takes[0].status == "succeeded"
    assert len(movie.shots[1].takes) == 2
    movie = await service.cut(
        movie.movie_id,
        "owner",
        movie.revision,
        [{"shot_id": "a", "take_id": a.take_id, "in_seconds": 0, "out_seconds": 1}],
    )
    movie = await service.assemble(movie.movie_id, "owner", movie.revision, "cut1")
    movie = await service.release(movie.movie_id, "owner", movie.revision, "Release 1")
    release = movie.releases[0]
    movie = await service.cut(
        movie.movie_id,
        "owner",
        movie.revision,
        [{"shot_id": "a", "take_id": a.take_id, "in_seconds": 0.5, "out_seconds": 1.5}],
    )
    assert movie.releases[0] == release
    assert movie.assembly is None


@pytest.mark.asyncio
async def test_cancel_waiting_take_is_durable_and_no_spend():
    service, pool, movie = await setup()
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["c"], "wait"
    )
    take = movie.shots[2].takes[0]
    movie = await service.cancel(
        movie.movie_id, "owner", movie.revision, "c", take.take_id
    )
    assert movie.shots[2].takes[0].status == "cancelled"
    assert len(pool.calls) == 0


@pytest.mark.asyncio
async def test_composition_is_durable_reviewable_and_idempotent():
    service, _pool, movie = await setup()

    class Composer:
        def __init__(self):
            self.calls = []

        async def compose_frame(self, request):
            self.calls.append(request)
            await service.assets.write_once(
                request.output_key,
                await service.assets.read(request.subject_key),
                "image/png",
            )
            return request.output_key

    composer = Composer()
    service.composition = composer
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "comp1", []
    )
    assert not movie.shots[0].opening_approved
    assert movie.shots[0].opening_asset_key == composer.calls[0].output_key
    movie = await service.compose(movie.movie_id, "owner", 1, "a", "comp1", [])
    assert len(composer.calls) == 1


@pytest.mark.asyncio
async def test_provider_prompt_limit_rejects_before_durable_spend():
    service, pool, movie = await setup()
    movie.shots[0].prompt = "x" * 2001
    movie = await service.save(movie, movie.revision)
    with pytest.raises(MovieError, match="2000"):
        await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "long")
    assert not pool.jobs


@pytest.mark.asyncio
async def test_removing_active_shot_is_rejected_to_keep_jobs_reconcilable():
    service, _pool, movie = await setup()
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a"], "active"
    )
    with pytest.raises(MovieError, match="active"):
        await service.story(
            movie.movie_id,
            "owner",
            movie.revision,
            brief="",
            style="",
            shots=[],
            batch_size=1,
        )


@pytest.mark.asyncio
async def test_reference_and_asset_scope_cannot_cross_movies():
    service, _pool, movie = await setup()
    other = await service.create("owner", "Other")
    with pytest.raises(MovieError):
        await service.authorize_asset(
            other.movie_id, "owner", movie.shots[0].opening_asset_key
        )


@pytest.mark.asyncio
async def test_reconcile_recovers_after_pool_accepts_then_transport_fails():
    service, pool, movie = await setup()
    original = pool.submit

    async def interrupted(request):
        await original(request)
        raise TimeoutError("response lost")

    pool.submit = interrupted
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a"], "lost"
    )
    assert movie.shots[0].takes[0].status == "submitting"
    pool.submit = original
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.shots[0].takes[0].status == "running"
    assert len(pool.jobs) == 1
    assert pool.calls[0].idempotency_key == pool.calls[1].idempotency_key


@pytest.mark.asyncio
async def test_total_active_budget_and_distinct_retry_seed():
    service, pool, movie = await setup()
    movie.batch_size = 1
    movie = await service.save(movie, movie.revision)
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "a")
    with pytest.raises(MovieError, match="active"):
        await service.generate(movie.movie_id, "owner", movie.revision, ["b"], "b")
    t = movie.shots[0].takes[0]
    pool.jobs[t.job_id].status = "failed"
    movie = await service.reconcile(movie.movie_id, "owner")
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a"], "retry"
    )
    assert pool.calls[0].payload["seed"] != pool.calls[-1].payload["seed"]


@pytest.mark.asyncio
async def test_unchanged_story_preserves_assembly():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "a")
    t = movie.shots[0].takes[0]
    pool.jobs[t.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    movie = await service.cut(
        movie.movie_id,
        "owner",
        movie.revision,
        [{"shot_id": "a", "take_id": t.take_id, "in_seconds": 0, "out_seconds": 1}],
    )
    movie = await service.assemble(movie.movie_id, "owner", movie.revision, "cut")
    shots = [
        {
            k: getattr(s, k)
            for k in [
                "shot_id",
                "name",
                "prompt",
                "opening_prompt",
                "duration_seconds",
                "continuation_of",
            ]
        }
        for s in movie.shots
    ]
    movie = await service.story(
        movie.movie_id,
        "owner",
        movie.revision,
        brief=movie.brief,
        style=movie.style,
        shots=shots,
        batch_size=3,
    )
    assert movie.assembly is not None


@pytest.mark.asyncio
async def test_terminal_bad_video_fails_take_preserves_retry():
    service, pool, movie = await setup()

    class BadMedia:
        async def final_frame(self, key):
            raise ValueError("Corrupt movie")

    service.media = BadMedia()
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "a")
    t = movie.shots[0].takes[0]
    pool.jobs[t.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.shots[0].takes[0].status == "failed"


@pytest.mark.asyncio
async def test_import_movie_freezes_clip_and_uses_actual_duration():
    service, pool, movie = await setup()

    class ImportedMedia(Media):
        async def duration(self, content):
            return 3.5

    service.media = ImportedMedia(service.assets)
    grant = await service.upload(movie.movie_id, "owner", "video/mp4")
    await service.assets.write_once(
        grant["asset_key"], b"fake video fixture", "video/mp4"
    )
    movie = await service.import_clip(
        movie.movie_id, "owner", grant["revision"], "a", grant["asset_key"]
    )
    take = movie.shots[0].takes[-1]
    assert take.status == "succeeded"
    assert take.duration_seconds == 3.5
    assert take.asset_key != grant["asset_key"]
    assert len(pool.jobs) == 0


@pytest.mark.asyncio
async def test_pending_composition_cannot_replace_new_uploaded_opening():
    service, _pool, movie = await setup()

    class InterruptedComposer:
        async def compose_frame(self, request):
            raise TimeoutError()

    service.composition = InterruptedComposer()
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "old", []
    )
    assert movie.compositions["old"]["status"] == "pending"
    grant = await service.upload(movie.movie_id, "owner", "image/png")
    from io import BytesIO

    from PIL import Image

    b = BytesIO()
    Image.new("RGB", (16, 16), "red").save(b, "PNG")
    await service.assets.write_once(grant["asset_key"], b.getvalue(), "image/png")
    movie = await service.opening(
        movie.movie_id, "owner", grant["revision"], "a", grant["asset_key"]
    )
    assert movie.compositions["old"]["status"] == "superseded"


@pytest.mark.asyncio
async def test_concurrent_compositions_for_same_shot_require_settling_first():
    service, _pool, movie = await setup()

    class InterruptedComposer:
        async def compose_frame(self, request):
            raise TimeoutError()

    service.composition = InterruptedComposer()
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "old", []
    )
    with pytest.raises(MovieError, match="pending"):
        await service.compose(movie.movie_id, "owner", movie.revision, "a", "new", [])


@pytest.mark.asyncio
async def test_accept_reviewed_stale_take_preserves_original_provenance_without_gpu():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "a")
    take = movie.shots[0].takes[0]
    pool.jobs[take.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    shots = [
        {
            k: getattr(s, k)
            for k in [
                "shot_id",
                "name",
                "prompt",
                "opening_prompt",
                "duration_seconds",
                "continuation_of",
            ]
        }
        for s in movie.shots
    ]
    shots[0]["prompt"] = "new intent"
    movie = await service.story(
        movie.movie_id,
        "owner",
        movie.revision,
        brief=movie.brief,
        style=movie.style,
        shots=shots,
        batch_size=3,
    )
    assert movie.shots[0].takes[0].stale
    movie = await service.accept_take(
        movie.movie_id, "owner", movie.revision, "a", take.take_id
    )
    accepted = movie.shots[0].takes[0]
    assert not accepted.stale
    assert accepted.predecessor_take_id == take.predecessor_take_id
    assert accepted.reviewed_spec_digest
    assert len(pool.jobs) == 1


@pytest.mark.asyncio
async def test_removing_shot_invalidates_old_assembled_cut():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a", "b"], "both"
    )
    for shot in movie.shots[:2]:
        pool.jobs[shot.takes[0].job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    clips = [
        {"shot_id": s.shot_id, "take_id": s.takes[0].take_id, "out_seconds": 1}
        for s in movie.shots[:2]
    ]
    movie = await service.cut(movie.movie_id, "owner", movie.revision, clips)
    movie = await service.assemble(movie.movie_id, "owner", movie.revision, "cut")
    inputs = [
        {
            k: getattr(s, k)
            for k in [
                "shot_id",
                "name",
                "prompt",
                "opening_prompt",
                "duration_seconds",
                "continuation_of",
            ]
        }
        for s in movie.shots
        if s.shot_id != "b"
    ]
    movie = await service.story(
        movie.movie_id,
        "owner",
        movie.revision,
        brief=movie.brief,
        style=movie.style,
        shots=inputs,
        batch_size=3,
    )
    assert movie.assembly is None
    with pytest.raises(MovieError):
        await service.release(movie.movie_id, "owner", movie.revision, "bad")


@pytest.mark.asyncio
async def test_release_checks_current_cut_digest():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "a")
    t = movie.shots[0].takes[0]
    pool.jobs[t.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    movie = await service.cut(
        movie.movie_id,
        "owner",
        movie.revision,
        [{"shot_id": "a", "take_id": t.take_id, "out_seconds": 1}],
    )
    movie = await service.assemble(movie.movie_id, "owner", movie.revision, "cut")
    movie.cut[0].out_seconds = 1.5
    movie = await service.save(movie, movie.revision)
    with pytest.raises(MovieError, match="current cut"):
        await service.release(movie.movie_id, "owner", movie.revision, "bad")


@pytest.mark.asyncio
async def test_corrupt_composition_fails_and_allows_explicit_new_attempt():
    service, _pool, movie = await setup()

    class BrokenComposer:
        async def compose_frame(self, request):
            await service.assets.write_once(request.output_key, b"broken", "image/png")
            return request.output_key

    service.composition = BrokenComposer()
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "bad", []
    )
    assert movie.compositions["bad"]["status"] == "failed"
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "retry", []
    )
    assert len(movie.compositions) == 2


@pytest.mark.asyncio
async def test_release_digest_survives_json_database_round_trip():
    from magic_video_editor.movies.models import Movie

    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "a")
    t = movie.shots[0].takes[0]
    pool.jobs[t.job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    movie = await service.cut(
        movie.movie_id,
        "owner",
        movie.revision,
        [{"shot_id": "a", "take_id": t.take_id, "out_seconds": 1}],
    )
    movie = await service.assemble(movie.movie_id, "owner", movie.revision, "cut")
    movie = await service.save(
        Movie.model_validate_json(movie.model_dump_json()), movie.revision
    )
    movie = await service.release(movie.movie_id, "owner", movie.revision, "Release")
    assert len(movie.releases) == 1


@pytest.mark.asyncio
async def test_generation_pins_owner_machine_affinity_across_reconciliation():
    service, pool, movie = await setup()

    async def tenant_for(owner):
        assert owner == "owner"
        return owner

    service.tenant_for = tenant_for
    movie.compute_target = "my_machines"
    movie = await service.save(movie, movie.revision)
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a"], "tenant"
    )
    assert pool.calls[0].tenant_id == "owner"
    assert pool.calls[0].requirements.min_vram_mb == 24_000
    assert movie.shots[0].takes[0].tenant_id == "owner"
    assert await service.running_jobs("owner") == 1
    assert await service.running_jobs("stranger") == 0


@pytest.mark.asyncio
async def test_owner_limit_refuses_complete_batch_before_creating_intent():
    service, pool, movie = await setup()

    async def guard(owner, requested):
        assert (owner, requested) == ("owner", 2)
        raise MovieError("Concurrent generation limit reached", 429)

    service.work_guard = guard
    with pytest.raises(MovieError, match="Concurrent"):
        await service.generate(
            movie.movie_id, "owner", movie.revision, ["a", "b"], "quota"
        )
    assert pool.calls == []
    assert (await service.get(movie.movie_id, "owner")).revision == movie.revision


@pytest.mark.asyncio
async def test_pool_opening_is_durable_wide_and_requires_approval():
    from io import BytesIO

    from PIL import Image

    service, pool, movie = await setup()
    expected_created_revision = movie.revision + 1
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "plate", []
    )
    intent = movie.compositions["plate"]
    assert intent["created_revision"] == expected_created_revision
    job_id = intent["job_id"]
    assert intent["status"] == "pending"
    submission = pool.calls[-1]
    assert submission.capability_id == "image.flux2_klein.subject.v2"
    assert submission.payload["prompt"] == "cinematic\nstill"
    assert submission.requirements.min_vram_mb == 16_000
    data = BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(data, "PNG")
    await service.assets.write_once(submission.output_key, data.getvalue(), "image/png")
    pool.jobs[job_id].status = "completed"
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.compositions["plate"]["status"] == "succeeded"
    assert movie.shots[0].opening_asset_key == intent["output_key"]
    assert movie.shots[0].opening_approved is False
    frame = await service.assets.read_limited(intent["output_key"], 20 * 1024 * 1024)
    assert Image.open(BytesIO(frame)).size == (768, 432)
    assert {call.job_id for call in pool.calls} == {job_id}


@pytest.mark.asyncio
async def test_legacy_pool_endpoints_do_not_expose_movie_jobs():
    from fastapi import HTTPException

    from magic_video_editor.app import _authorize_pool_job

    class Jobs:
        async def get(self, job_id):
            return None

    class MoviePool:
        async def get(self, job_id):
            return SimpleNamespace(output_key="outputs/movies/film/takes/take.mp4")

    runtime = SimpleNamespace(jobs=Jobs(), worker_pool=MoviePool())
    for actor in (None, "stranger", "forged-owner"):
        with pytest.raises(HTTPException) as error:
            await _authorize_pool_job(runtime, "take", actor)
        assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_superseded_pool_opening_counts_until_cancel_settlement():
    service, pool, movie = await setup()
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "plate", []
    )
    job_id = movie.compositions["plate"]["job_id"]
    service.invalidate(movie, {"a"})
    movie = await service.save(movie, movie.revision)
    assert await service.running_jobs("owner") == 1

    async def cancel(job):
        assert job == job_id
        pool.jobs[job].status = "cancelled"
        return True

    pool.cancel = cancel
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.compositions["plate"]["settled"] is True
    assert await service.running_jobs("owner") == 0


@pytest.mark.asyncio
async def test_asset_usage_retries_are_idempotent_and_owner_scoped():
    service, _, movie = await setup()
    first = await service.store.stored_bytes("owner")
    assert first > 0
    movie = await service.save(movie, movie.revision)
    await service.account_assets(movie)
    assert await service.store.stored_bytes("owner") == first
    assert await service.store.stored_bytes("other") == 0


@pytest.mark.asyncio
async def test_shared_compute_ignores_private_enrolment_and_my_machines_requires_it():
    service, pool, movie = await setup()

    async def private_machine(owner):
        return owner

    service.tenant_for = private_machine
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a"], "shared"
    )
    assert pool.calls[-1].tenant_id is None
    movie.compute_target = "my_machines"
    service.tenant_for = None
    movie = await service.save(movie, movie.revision)
    with pytest.raises(MovieError, match="Attach a machine"):
        await service.generate(
            movie.movie_id, "owner", movie.revision, ["b"], "private"
        )
    assert len(pool.calls) == 1


@pytest.mark.asyncio
async def test_completed_take_provenance_backfills_late_audit_without_claim_secrets():
    from datetime import UTC, datetime

    service, pool, movie = await setup()
    movie = await service.generate(
        movie.movie_id, "owner", movie.revision, ["a"], "proof"
    )
    take = movie.shots[0].takes[0]
    take.status = "succeeded"
    movie = await service.save(movie, movie.revision)
    events = []

    async def audit_for_job(job_id):
        assert job_id == take.job_id
        if not events:
            raise OSError("Audit temporarily unavailable")
        return events

    pool.audit_for_job = audit_for_job
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.shots[0].takes[0].completed_by_worker_id is None
    events.append(
        SimpleNamespace(
            event_type="job_completed",
            worker_id="gpu-house-2",
            created_at=datetime.now(UTC),
            claim_token="private",
        )
    )
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.shots[0].takes[0].completed_by_worker_id == "gpu-house-2"
    assert "claim_token" not in movie.model_dump_json()


@pytest.mark.asyncio
async def test_failed_pool_opening_is_not_misreported_as_invalid_image(caplog):
    service, pool, movie = await setup()
    movie = await service.compose(
        movie.movie_id, "owner", movie.revision, "a", "failed-pool", []
    )
    job_id = movie.compositions["failed-pool"]["job_id"]
    pool.jobs[job_id].status = "failed"
    pool.jobs[job_id].failure_code = "worker_execution_failed"
    pool.jobs[job_id].claim_token = "never-log-this"
    movie = await service.reconcile(movie.movie_id, "owner")
    assert movie.compositions["failed-pool"]["status"] == "failed"
    assert "GPU could not generate" in movie.compositions["failed-pool"]["error"]
    assert job_id in caplog.text
    assert "worker_execution_failed" in caplog.text
    assert "never-log-this" not in caplog.text


@pytest.mark.asyncio
async def test_legacy_take_reconciliation_preserves_submitted_payload_identity():
    from magic_video_editor.movies.models import Take

    service, pool, movie = await setup()
    legacy = Take(
        take_id="old",
        request_id="old",
        job_id="old",
        asset_key="outputs/old.mp4",
        opening_asset_key=movie.shots[0].opening_asset_key,
        prompt="motion",
        duration_seconds=2,
    )
    await service.submit(legacy)
    submission = pool.calls[-1]
    assert submission.payload["height"] == 432
    assert submission.payload["fps"] == 16
    assert submission.payload["length"] == 32
    assert submission.input_keys == (legacy.opening_asset_key,)
    assert submission.payload["images"]["first_frame"] == legacy.opening_asset_key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seconds,frames", [(2, 56), (6, 158), (8, 192), (10, 243), (12.5, 311), (15, 362)]
)
async def test_motion_uses_native_audio_clock_and_persists_retry_recipe(seconds, frames):
    service, pool, movie = await setup()
    # A persisted story with the requested motion duration.
    movie.shots[0].duration_seconds = seconds
    movie = await service.save(movie, movie.revision)
    movie = await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "native-clock")
    take = movie.shots[0].takes[0]
    first = pool.calls[-1].payload
    assert first["fps"] == 24
    assert first["length"] == frames
    assert frames % 17 == 5
    # Reconciliation replaces reported duration, but cannot change an immutable
    # already-submitted request's frame count or encoding clock on retry.
    take.duration_seconds = frames / 24
    await service.submit(take)
    assert pool.calls[-1].payload == first


async def test_admin_shared_routing_does_not_override_explicit_own_machine_choice():
    from unittest.mock import AsyncMock
    service = MovieService(MemoryMovieStore(), MemoryAssetStore(), tenant_for=AsyncMock(return_value='admin-owner'), shared_tenant_for=AsyncMock(return_value='studio:admin-cluster'))
    movie = SimpleNamespace(owner_id='admin-owner', compute_target='shared')
    assert await service.resolve_tenant(movie) == 'studio:admin-cluster'
    movie.compute_target = 'my_machines'
    assert await service.resolve_tenant(movie) == 'admin-owner'

@pytest.mark.asyncio
async def test_pending_cpu_assembly_does_not_churn_revision_or_change_tenant():
    from magic_video_editor.movies.cpu_pool import CpuPending
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(movie.movie_id, 'owner', movie.revision, ['a'], 'cpu-test')
    take = movie.shots[0].takes[0]
    pool.jobs[take.job_id].status = 'completed'
    movie = await service.reconcile(movie.movie_id, 'owner')
    movie = await service.cut(movie.movie_id, 'owner', movie.revision, [{'shot_id':'a','take_id':take.take_id,'in_seconds':0,'out_seconds':1}])
    class PendingMedia(Media):
        def scoped(self, tenant, scope):
            self.tenant = tenant
            return self
        async def assemble(self, *args, **kwargs):
            raise CpuPending()
    service.media = PendingMedia(service.assets)
    async def shared(owner): return 'studio:admin-cluster'
    service.shared_tenant_for = shared
    movie = await service.assemble(movie.movie_id, 'owner', movie.revision, 'cpu-assembly')
    assert movie.assembly.status == 'pending'
    assert movie.assembly.tenant_id == 'studio:admin-cluster'
    revision = movie.revision
    service.shared_tenant_for = None
    movie = await service.reconcile(movie.movie_id, 'owner')
    assert movie.revision == revision
    assert service.media.tenant == 'studio:admin-cluster'


@pytest.mark.asyncio
async def test_movie_build_batches_selects_continuations_and_stitches_once():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie.batch_size = 1
    movie = await service.save(movie, movie.revision)
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'film')
    assert len(pool.jobs) == 1
    # Replaying a lost response must retain the same intent and jobs.
    await service.build_movie(movie.movie_id, 'owner', 0, 'film')
    for _ in range(20):
        assert sum(j.status == 'queued' for j in pool.jobs.values()) <= 1
        for job in pool.jobs.values():
            job.status = 'completed'
        movie = await service.reconcile(movie.movie_id, 'owner')
        if movie.build.status == 'succeeded':
            break
    assert movie.build.status == 'succeeded'
    assert len(pool.jobs) == 3
    assert [c.shot_id for c in movie.cut] == ['a', 'b', 'c']
    assert movie.shots[2].takes[0].predecessor_take_id == movie.shots[0].selected_take_id
    assert movie.assembly.status == 'succeeded'
    for _ in range(3):
        await service.reconcile(movie.movie_id, 'owner')
    assert len(pool.jobs) == 3


@pytest.mark.asyncio
async def test_review_starts_from_the_cut_a_full_build_saves():
    from magic_video_editor.movies.build import selected_cut

    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'film')
    # Nothing has finished, so nothing is offered yet.
    assert selected_cut(movie) == []
    for _ in range(20):
        for job in pool.jobs.values():
            job.status = 'completed'
        movie = await service.reconcile(movie.movie_id, 'owner')
        if movie.build.status == 'succeeded':
            break
    assert movie.build.status == 'succeeded'
    assert [c.model_dump() for c in selected_cut(movie)] == [c.model_dump() for c in movie.cut]
    assert [(c.transition_seconds, c.mute) for c in movie.cut] == [(0.5, True)] * 3


@pytest.mark.asyncio
async def test_failed_take_stops_build_without_repeated_spending():
    service, pool, movie = await setup()
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'film')
    for job in pool.jobs.values():
        job.status = 'failed'
        job.error = 'GPU failed'
    for _ in range(5):
        movie = await service.reconcile(movie.movie_id, 'owner')
    assert movie.build.status == 'failed'
    assert len(pool.jobs) == 2


@pytest.mark.asyncio
async def test_build_without_a_gpu_pool_fails_instead_of_generating_forever():
    service, _, movie = await setup()
    service.pool = None
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'film')
    assert movie.build.status == 'failed'
    assert 'no GPU pool' in movie.build.error
    movie = await service.reconcile(movie.movie_id, 'owner')
    assert movie.build.status == 'failed'
    assert all(not shot.takes for shot in movie.shots)


@pytest.mark.asyncio
async def test_build_with_active_takes_fails_after_a_restart_without_a_pool():
    service, pool, movie = await setup()
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'film')
    assert len(pool.jobs) == 2
    assert movie.build.status == 'generating'
    # The service comes back without a pool: nothing can submit or poll the takes
    # the build already adopted, so it must fail rather than wait for them.
    service.pool = None
    movie = await service.reconcile(movie.movie_id, 'owner')
    assert movie.build.status == 'failed'
    assert 'no GPU pool' in movie.build.error
    assert any(t.status == 'running' for s in movie.shots for t in s.takes)


@pytest.mark.asyncio
async def test_build_without_a_pool_fails_on_a_reroll_it_would_wait_for():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'first')
    for _ in range(20):
        for job in pool.jobs.values():
            job.status = 'completed'
        movie = await service.reconcile(movie.movie_id, 'owner')
        if movie.build.status == 'succeeded':
            break
    assert movie.build.status == 'succeeded'
    # Every shot has a usable take, but a reroll is still running when the service
    # comes back without a pool. The build waits for every active take before it
    # assembles, and this one can never finish, so the build must fail, not hang.
    movie = await service.generate(movie.movie_id, 'owner', movie.revision, ['b'], 'reroll')
    assert movie.shots[1].takes[-1].status == 'running'
    service.pool = None
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'second')
    assert movie.build.status == 'failed'
    assert 'no GPU pool' in movie.build.error


@pytest.mark.asyncio
async def test_concurrent_movie_requests_do_not_duplicate_generation():
    service, pool, movie = await setup()
    await asyncio.gather(*(
        service.build_movie(movie.movie_id, 'owner', movie.revision, f'film-{i}')
        for i in range(2)
    ), return_exceptions=True)
    await asyncio.gather(*(service.reconcile(movie.movie_id, 'owner') for _ in range(3)))
    assert len(pool.jobs) == 2


@pytest.mark.asyncio
async def test_movie_rebuild_reuses_takes_and_preserves_matching_trims():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'first')
    for _ in range(20):
        for job in pool.jobs.values():
            job.status = 'completed'
        movie = await service.reconcile(movie.movie_id, 'owner')
        if movie.build.status == 'succeeded':
            break
    assert movie.build.status == 'succeeded'
    movie.cut[0].in_seconds = 0.5
    movie = await service.cut(movie.movie_id, 'owner', movie.revision, movie.cut)
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'second')
    for _ in range(5):
        movie = await service.reconcile(movie.movie_id, 'owner')
    assert movie.build.status == 'succeeded'
    assert movie.cut[0].in_seconds == 0.5
    assert len(pool.jobs) == 3


@pytest.mark.asyncio
async def test_movie_build_requires_ownership_and_a_starting_image():
    service, pool, movie = await setup()
    with pytest.raises(MovieError):
        await service.build_movie(movie.movie_id, 'other', movie.revision, 'film')
    movie.shots[0].opening_asset_key = None
    movie = await service.save(movie, movie.revision)
    with pytest.raises(MovieError, match='starting image'):
        await service.build_movie(movie.movie_id, 'owner', movie.revision, 'film')
    assert not pool.jobs


@pytest.mark.asyncio
async def test_image_generation_uses_attached_image_without_approval():
    service, pool, movie = await setup()
    image = movie.shots[0].opening_asset_key
    movie.shots[0].opening_approved = False
    movie = await service.save(movie, movie.revision)
    movie = await service.generate(movie.movie_id, 'owner', movie.revision, ['a'], 'image-to-video')
    assert len(pool.jobs) == 1
    assert movie.shots[0].takes[0].opening_asset_key == image
    assert movie.shots[0].opening_asset_key == image


@pytest.mark.asyncio
async def test_whole_movie_uses_existing_images_without_approval():
    service, pool, movie = await setup()
    for shot in movie.shots:
        shot.opening_approved = False
    movie = await service.save(movie, movie.revision)
    movie = await service.build_movie(movie.movie_id, 'owner', movie.revision, 'no-approval')
    assert len(pool.jobs) == 2
    assert movie.build.status == 'generating'


@pytest.mark.asyncio
async def test_refined_shot_automatically_becomes_selected_after_completion():
    service, pool, movie = await setup()
    service.media = Media(service.assets)
    movie = await service.generate(movie.movie_id, 'owner', movie.revision, ['a'], 'first', select_when_ready=True)
    pool.jobs[movie.shots[0].takes[0].job_id].status = 'completed'
    movie = await service.reconcile(movie.movie_id, 'owner')
    first = movie.shots[0].selected_take_id
    assert first == movie.shots[0].takes[0].take_id
    movie = await service.generate(movie.movie_id, 'owner', movie.revision, ['a'], 'refine', select_when_ready=True)
    pool.jobs[movie.shots[0].takes[-1].job_id].status = 'completed'
    movie = await service.reconcile(movie.movie_id, 'owner')
    assert movie.shots[0].selected_take_id == movie.shots[0].takes[-1].take_id
    assert movie.shots[0].selected_take_id != first
    assert movie.shots[0].takes[0].status == 'succeeded'


REFUSAL = (
    "None of your machines can make subject images. "
    "desk: subject images need a 12 GB card and this one has 8 GB."
)


@pytest.mark.asyncio
async def test_an_opening_none_of_the_owners_machines_can_run_fails_with_the_reason():
    from outbound_gpu_worker_pool.contracts import GpuTelemetry

    service, pool, movie = await setup()

    async def own_machines(owner):
        return owner

    async def capable(*, tenant_id=None):
        return (SimpleNamespace(
            worker_id="desk", tenant_id="owner", draining=False, vram_mb=12282, gpus=(),
            status=SimpleNamespace(value="online"), last_heartbeat_at=datetime.now(UTC),
            capability_ids=("image.flux2_klein.subject.12gb.v1",),
        ),)

    async def worker_views(*, tenant_id=None):
        return (SimpleNamespace(
            worker_id="desk", tenant_id="owner", draining=False, vram_mb=None,
            status=SimpleNamespace(value="online"), last_heartbeat_at=datetime.now(UTC),
            capability_ids=("movie.ffmpeg.process.v3",),
            gpus=(GpuTelemetry(index=0, name="RTX 3070", utilization_pct=0,
                               memory_used_mb=0, memory_total_mb=8192),),
        ),)

    service.tenant_for = service.shared_tenant_for = own_machines
    calls = len(pool.calls)
    # Composed while a capable machine was attached, then it was swapped out.
    pool.worker_views = capable
    movie = await service.compose(movie.movie_id, "owner", movie.revision, "a", "plate", [])
    pool.jobs.clear()
    pool.calls[calls:] = []
    pool.worker_views = worker_views
    movie = await service.reconcile(movie.movie_id, "owner")

    intent = movie.compositions["plate"]
    assert intent["status"] == "failed"
    assert intent["error"] == REFUSAL
    assert len(pool.calls) == calls

    # A new opening is refused before anything is recorded or queued.
    with pytest.raises(MovieError) as refused:
        await service.compose(movie.movie_id, "owner", movie.revision, "a", "plate-2", [])
    assert (str(refused.value), refused.value.code) == (REFUSAL, "machine_cannot_run")
    assert len(pool.calls) == calls

    # So is a take: the 8 GB card cannot run the 24 GB H3 graphs.
    with pytest.raises(MovieError, match="movie takes need a 24 GB card and this one has 8 GB"):
        await service.generate(movie.movie_id, "owner", movie.revision, ["a"], "takes")


@pytest.mark.asyncio
async def test_an_opening_already_in_the_pool_finishes_after_its_machine_goes_away():
    from io import BytesIO

    from PIL import Image

    service, pool, movie = await setup()

    async def own_machines(owner):
        return owner

    def machine(**presence):
        async def views(*, tenant_id=None):
            return (SimpleNamespace(
                worker_id="desk", tenant_id="owner", vram_mb=12282, gpus=(),
                capability_ids=("image.flux2_klein.subject.12gb.v1",),
                **{"draining": False, "status": SimpleNamespace(value="online"),
                   "last_heartbeat_at": datetime.now(UTC), **presence}),)
        return views

    service.tenant_for = service.shared_tenant_for = own_machines
    pool.worker_views = machine()
    movie = await service.compose(movie.movie_id, "owner", movie.revision, "a", "plate", [])
    submission = pool.calls[-1]
    assert submission.capability_id == "image.flux2_klein.subject.12gb.v1"

    # The machine finishes the frame, then starts draining before the next poll.
    pool.worker_views = machine(draining=True)
    data = BytesIO()
    Image.new("RGB", (1024, 1024), "blue").save(data, "PNG")
    await service.assets.write_once(submission.output_key, data.getvalue(), "image/png")
    pool.jobs[submission.job_id].status = "completed"
    calls = len(pool.calls)
    movie = await service.reconcile(movie.movie_id, "owner")

    assert movie.compositions["plate"]["status"] == "succeeded"
    assert len(pool.calls) == calls

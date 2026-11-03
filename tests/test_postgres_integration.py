import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

from magic_video_editor.contracts import (
    JobStatus,
    JobSubmission,
    VideoJobResult,
    job_activity_cursor,
)
from magic_video_editor.plugins.postgres import PostgresJobStore
from magic_video_editor.studio.postgres import PostgresStudioStore
from magic_video_editor.studio.enrolment import (
    PostgresStudioEnrolmentStore,
    PairingTokenUnusable,
    generate_pairing_token,
    generate_worker_credential,
    hash_token,
)


DATABASE_URL = os.environ.get(
    "MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL",
    "postgresql://postgres:test@127.0.0.1:15432/magic_video_editor",
)


@dataclass
class IsolatedStore:
    jobs: PostgresJobStore
    pool: asyncpg.Pool


@asynccontextmanager
async def _isolated_store() -> AsyncIterator[IsolatedStore]:
    schema = f"test_{uuid4().hex}"
    admin = await asyncpg.connect(DATABASE_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        server_settings={"search_path": schema},
    )
    job_store = PostgresJobStore(DATABASE_URL, pool=pool)
    await job_store.start()
    try:
        yield IsolatedStore(jobs=job_store, pool=pool)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.fixture
async def store() -> AsyncIterator[PostgresJobStore]:
    async with _isolated_store() as isolated:
        yield isolated.jobs


@pytest.fixture
async def isolated() -> AsyncIterator[IsolatedStore]:
    async with _isolated_store() as isolated_store:
        yield isolated_store


async def test_worker_package_authorization_preserves_single_use_pairing(isolated: IsolatedStore) -> None:
    enrolment = PostgresStudioEnrolmentStore(DATABASE_URL, pool=isolated.pool)
    await enrolment.start()
    now = datetime.now(UTC)
    token_hash = hash_token(generate_pairing_token())
    await enrolment.mint_pairing_token(
        owner_id="admin-1", token_hash=token_hash, expires_at=now + timedelta(hours=1),
    )
    for _ in range(2):
        assert await enrolment.usable_pairing_token(token_hash=token_hash, now=now)
    assert not await enrolment.usable_pairing_token(token_hash=hash_token("unknown"), now=now)
    assert not await enrolment.usable_pairing_token(token_hash=token_hash, now=now + timedelta(hours=1))
    kwargs = dict(token_hash=token_hash, worker_id=str(uuid4()), label="Admin GPU",
                  credential_hash=hash_token(generate_worker_credential()), now=now)
    worker = await enrolment.redeem_pairing_token(**kwargs)
    assert worker.owner_id == "admin-1"
    assert not await enrolment.usable_pairing_token(token_hash=token_hash, now=now)
    with pytest.raises(PairingTokenUnusable):
        await enrolment.redeem_pairing_token(**kwargs)


async def test_standalone_job_lifecycle_is_idempotent_and_fenced(
    store: PostgresJobStore,
) -> None:
    submission = JobSubmission(
        job_id="00000000-0000-0000-0000-000000000001",
        idempotency_key="request-1",
        operation="package",
        source_key="sources/input.mp4",
        output_key="outputs/job-1.mp4",
        requested_outputs={"alpha": True},
    )

    first_submission = await store.submit(submission)
    first = first_submission.record
    duplicate = await store.submit(
        JobSubmission(**{**submission.__dict__, "job_id": "00000000-0000-0000-0000-000000000002"})
    )
    claimed = await store.claim(first.job_id)

    assert first_submission.created is True
    assert duplicate.created is False
    assert duplicate.record.job_id == first.job_id
    assert claimed is not None
    assert await store.complete(first.job_id, "stale-token", VideoJobResult(output_key="bad")) is False
    assert await store.complete(
        first.job_id, claimed.claim_token, VideoJobResult(output_key="outputs/final.mp4")
    ) is True
    settled = await store.get(first.job_id)
    assert settled is not None
    assert settled.status is JobStatus.COMPLETED
    assert settled.result == VideoJobResult(output_key="outputs/final.mp4")


async def test_migrations_replay_and_leave_no_worker_pool_schema(
    isolated: IsolatedStore,
) -> None:
    # Migrations must be idempotent: a second start on the same schema is a no-op.
    await isolated.jobs.start()

    async with isolated.pool.acquire() as connection:
        tables = await connection.fetch(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = current_schema()"""
        )
        columns = await connection.fetch(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = 'video_jobs'"""
        )

    table_names = {row["table_name"] for row in tables}
    column_names = {row["column_name"] for row in columns}
    assert "video_jobs" in table_names
    assert "gpu_workers" not in table_names
    assert "worker_audit_events" not in table_names
    assert "attempt_budget" in column_names
    assert column_names.isdisjoint(
        {
            "capability_id",
            "priority",
            "execution_deadline_seconds",
            "request_digest",
            "leased_by",
        }
    )


async def test_migration_replay_burns_no_column_slots(
    isolated: IsolatedStore,
) -> None:
    # Postgres keeps a catalog slot per dropped column (limit 1600 per table). A
    # migration pair that adds then drops columns on every start eats those slots
    # until startup fails outright (2026-09-06 outage). Replay must drop nothing.
    async with isolated.pool.acquire() as connection:
        before = await connection.fetchval(
            "SELECT count(*) FROM pg_attribute "
            "WHERE attrelid = 'video_jobs'::regclass AND attisdropped"
        )
    await isolated.jobs.start()
    await isolated.jobs.start()
    async with isolated.pool.acquire() as connection:
        after = await connection.fetchval(
            "SELECT count(*) FROM pg_attribute "
            "WHERE attrelid = 'video_jobs'::regclass AND attisdropped"
        )
    assert after == before


async def test_attempt_budget_survives_the_pool_removal(
    isolated: IsolatedStore,
) -> None:
    submission = JobSubmission(
        job_id="00000000-0000-0000-0000-000000000004",
        idempotency_key="request-budget",
        operation="package",
        source_key="sources/input.mp4",
        output_key="outputs/job-budget.mp4",
        requested_outputs={},
        attempt_budget=2,
    )

    record = (await isolated.jobs.submit(submission)).record
    first = await isolated.jobs.claim(record.job_id)
    assert first is not None
    assert await isolated.jobs.release(record.job_id, first.claim_token, "retry")
    second = await isolated.jobs.claim(record.job_id)

    assert record.attempt_budget == 2
    assert first.attempt_budget == 2
    assert first.attempts == 1
    assert second is not None
    assert second.attempts == 2
    # 006's attempt_budget CHECK constraint is deliberately kept by 007.
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with isolated.pool.acquire() as connection:
            await connection.execute(
                "UPDATE video_jobs SET attempt_budget = 0 WHERE job_id = $1::uuid",
                record.job_id,
            )


async def test_cancellation_rejects_stale_worker_completion(
    store: PostgresJobStore,
) -> None:
    record = (await store.submit(JobSubmission(
        job_id="00000000-0000-0000-0000-000000000003",
        idempotency_key="request-2", operation="package",
        source_key="sources/input.mp4", output_key="outputs/job-2.mp4",
        requested_outputs={},
    ))).record
    claimed = await store.claim(record.job_id)
    assert claimed is not None

    assert await store.cancel(record.job_id) is True
    assert await store.complete(
        record.job_id, claimed.claim_token, VideoJobResult(output_key="outputs/stale.mp4")
    ) is False


async def test_list_for_project_pages_past_the_unpaged_query_cap(
    store: PostgresJobStore,
) -> None:
    """T11: list_for_project(limit=None) hard-caps at 100 rows (video_jobs is
    unbounded per project), so callers needing full history must page with
    an explicit cursor/limit instead. Postgres-only.
    """
    total_jobs = 12
    job_ids: list[str] = []
    for index in range(total_jobs):
        job_id = f"00000000-0000-0000-0000-0000000001{index:02d}"
        await store.submit(JobSubmission(
            job_id=job_id,
            idempotency_key=f"paging-request-{index}",
            operation="package",
            source_key="sources/input.mp4",
            output_key=f"outputs/job-{index}.mp4",
            requested_outputs={},
            project_id="paging-project",
        ))
        job_ids.append(job_id)

    seen: list[str] = []
    cursor = None
    for _ in range(total_jobs + 1):
        page = await store.list_for_project("paging-project", cursor=cursor, limit=5)
        if not page:
            break
        seen.extend(record.job_id for record in page)
        cursor = job_activity_cursor(page[-1])

    assert set(seen) == set(job_ids)
    assert len(seen) == total_jobs


@pytest.fixture
async def studio_schema() -> str:
    schema = f"test_{uuid4().hex}"
    admin = await asyncpg.connect(DATABASE_URL)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    yield schema
    await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await admin.close()


def _legacy_manifest(project_id: str, artifact_key: str) -> dict:
    """A manifest shaped like one written before `media_kind` existed: the
    candidate's metadata object has no `media_kind` key at all."""
    return {
        "project_id": project_id,
        "name": "legacy project",
        "owner_id": None,
        "external_subject": None,
        "subject": {
            "revisions": [],
            "current_revision": None,
            "assessment": None,
            "approval": {"state": "not_ready"},
        },
        "composition": {"revisions": [], "current": None},
        "motions": [
            {
                "specification": {
                    "motion_id": "walk",
                    "name": "Walk",
                    "kind": "preset",
                    "preset": "walk",
                    "loop_policy": "loop",
                    "required": True,
                    "references": [],
                },
                "candidates": [
                    {
                        "candidate_id": "candidate-1",
                        "artifact_key": artifact_key,
                        "sha256": "a" * 64,
                        "lifecycle": "ready",
                        "parent_revision_ids": [],
                        "metadata": {
                            "decodable": True,
                            "frame_count": 4,
                            "width": 512,
                            "height": 512,
                            "constant_geometry": True,
                            "has_alpha": True,
                            "duplicate_terminal_frame": False,
                            "packing_estimate": {
                                "profile": "horizontal-strip-v1",
                                "width": 2048,
                                "height": 512,
                            },
                        },
                        "facing": "right",
                        "source": "generated",
                    }
                ],
                "selected_candidate_id": None,
            }
        ],
        "set_approval": {"state": "not_ready"},
        "releases": [],
    }


async def test_media_kind_backfill_derives_video_and_sheet_from_artifact_key(
    studio_schema: str,
) -> None:
    """T1: manifests written before `media_kind` was required must keep
    loading. Postgres-only — set MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL to run
    it; it does not run without a reachable Postgres instance.
    """
    pool = await asyncpg.create_pool(
        DATABASE_URL, server_settings={"search_path": studio_schema}
    )
    bootstrap_store = PostgresStudioStore(DATABASE_URL, pool=pool)
    await bootstrap_store.start()

    video_project = "legacy-video-project"
    sheet_project = "legacy-sheet-project"
    now = datetime.now(UTC)
    async with pool.acquire() as connection:
        for project_id, artifact_key in (
            (video_project, "outputs/studio/legacy/motions/walk/op.mp4"),
            (sheet_project, "outputs/studio/legacy/motions/walk/op.png"),
        ):
            await connection.execute(
                """INSERT INTO studio_projects (project_id, current_revision, updated_at)
                   VALUES ($1, 1, $2)""",
                project_id, now,
            )
            await connection.execute(
                """INSERT INTO studio_manifest_revisions
                       (project_id, revision, manifest_digest, manifest, created_at)
                   VALUES ($1, 1, 'legacy-digest', $2::jsonb, $3)""",
                project_id,
                json.dumps(_legacy_manifest(project_id, artifact_key)),
                now,
            )

    # A fresh store instance re-runs start(), which applies the media_kind
    # backfill migration against the rows just inserted above.
    second_pool = await asyncpg.create_pool(
        DATABASE_URL, server_settings={"search_path": studio_schema}
    )
    store = PostgresStudioStore(DATABASE_URL, pool=second_pool)
    await store.start()

    video_stored = await store.get(video_project)
    sheet_stored = await store.get(sheet_project)

    assert video_stored is not None
    assert sheet_stored is not None
    video_candidate = video_stored.manifest.motions[0].candidates[0]
    sheet_candidate = sheet_stored.manifest.motions[0].candidates[0]
    assert video_candidate.metadata is not None
    assert video_candidate.metadata.media_kind == "video"
    assert sheet_candidate.metadata is not None
    assert sheet_candidate.metadata.media_kind == "sheet"

    await pool.close()
    await second_pool.close()


async def test_studio_enrolment_binds_machines_in_the_postgres_pool_registry(
    isolated: IsolatedStore,
) -> None:
    """Against the real registry, whose identity subjects are unique: two
    machines of one owner both enroll under that owner's tenant, and a row a
    machine wrote before server-approved enrollment keeps authenticating."""
    from outbound_gpu_worker_pool import (
        MemoryAssetStore,
        WorkerIdentityMismatch,
        WorkerRegistration,
    )
    from outbound_gpu_worker_pool.postgres import (
        PostgresAuditLog,
        PostgresWorkerRegistry,
    )
    from outbound_gpu_worker_pool.postgres import (
        PostgresJobStore as PostgresPoolJobStore,
    )
    from outbound_gpu_worker_pool.service import WorkerPoolService

    from magic_video_editor.studio.enrolment import (
        EnrolledWorkerAuthenticator,
        enroll_in_pool,
        enrolled_worker_subject,
    )

    enrolment = PostgresStudioEnrolmentStore(DATABASE_URL, pool=isolated.pool)
    await enrolment.start()
    pool_jobs = PostgresPoolJobStore(DATABASE_URL, pool=isolated.pool)
    await pool_jobs.start()  # applies the pool migrations
    registry = PostgresWorkerRegistry(DATABASE_URL, pool=isolated.pool)
    authenticator = EnrolledWorkerAuthenticator(enrolment, registry=registry)
    service = WorkerPoolService(
        pool_jobs,
        MemoryAssetStore(),
        registry,
        PostgresAuditLog(DATABASE_URL, pool=isolated.pool),
        authenticator,
        {},
    )
    now = datetime.now(UTC)

    async def redeem(owner: str) -> tuple[str, object]:
        token_hash = hash_token(generate_pairing_token())
        await enrolment.mint_pairing_token(
            owner_id=owner, token_hash=token_hash, expires_at=now + timedelta(hours=1)
        )
        credential = generate_worker_credential()
        worker = await enrolment.redeem_pairing_token(
            token_hash=token_hash,
            worker_id=str(uuid4()),
            label="GPU",
            credential_hash=hash_token(credential),
            now=now,
        )
        return credential, worker

    first_credential, first = await redeem("owner-1")
    second_credential, second = await redeem("owner-1")
    await enroll_in_pool(service, first)
    await enroll_in_pool(service, second)
    for credential, worker in ((first_credential, first), (second_credential, second)):
        identity = await service.authenticate(f"Bearer {credential}")
        assert identity.subject == enrolled_worker_subject(worker.worker_id)
        record = await service.register_heartbeat(
            identity,
            WorkerRegistration(worker_id=worker.worker_id, capabilities=(), tenant_id="owner-1"),
        )
        assert record.tenant_id == "owner-1"
    # Enrollment is idempotent, never a rebinding.
    await enroll_in_pool(service, first)
    with pytest.raises(WorkerIdentityMismatch):
        from outbound_gpu_worker_pool import WorkerEnrollment

        await service.enroll_worker(
            first.worker_id, WorkerEnrollment("studio-machine:someone-else", "owner-1")
        )

    legacy_credential, legacy = await redeem("owner-2")
    await registry.upsert(
        WorkerRegistration(worker_id=legacy.worker_id, capabilities=(), tenant_id="owner-2"),
        identity_subject="owner-2",
    )
    identity = await service.authenticate(f"Bearer {legacy_credential}")
    assert identity.subject == "owner-2"
    record = await service.register_heartbeat(
        identity,
        WorkerRegistration(worker_id=legacy.worker_id, capabilities=(), tenant_id="owner-2"),
    )
    assert record.identity_subject == "owner-2"


async def test_project_movie_and_account_deletes_leave_no_rows(isolated: IsolatedStore) -> None:
    """The deletes that back project, movie and account deletion, on the real schema."""
    from studio.test_releases import PNG_SHEET, releasable_manifest

    from magic_video_editor.memory import MemoryAssetStore
    from magic_video_editor.movies.models import Movie
    from magic_video_editor.movies.store import PostgresMovieStore
    from magic_video_editor.studio.api_keys import PostgresStudioApiKeyStore
    from magic_video_editor.studio.auth import PostgresStudioAuthStore
    from magic_video_editor.studio.limits import PostgresStudioOwnerUsageStore
    from magic_video_editor.studio.provider_accounts import PostgresProviderAccountStore
    from magic_video_editor.studio.service import StudioService


    pool = isolated.pool
    studio_store = PostgresStudioStore(DATABASE_URL, pool=pool)
    movies = PostgresMovieStore(DATABASE_URL, pool=pool)
    keys = PostgresStudioApiKeyStore(DATABASE_URL, pool=pool)
    users = PostgresStudioAuthStore(DATABASE_URL, pool=pool)
    usage = PostgresStudioOwnerUsageStore(DATABASE_URL, pool=pool)
    providers = PostgresProviderAccountStore(DATABASE_URL, pool=pool)
    enrolment = PostgresStudioEnrolmentStore(DATABASE_URL, pool=pool)
    for started in (studio_store, movies, keys, users, usage, providers, enrolment):
        await started.start()

    # A project with a release and an export, plus one of its jobs.
    service = StudioService(store=studio_store, assets=MemoryAssetStore(assets={"outputs/idle.png": PNG_SHEET}))
    created = await studio_store.create(releasable_manifest())
    release = await service.create_release(created.project_id, created.revision, "First", "user-1")
    export = await service.create_export(release.release_id, "png-json", "user-1")
    await isolated.jobs.submit(JobSubmission(
        job_id=str(uuid4()), idempotency_key="delete-me", operation="package",
        source_key="sources/input.mp4", output_key="outputs/job.mp4", requested_outputs={},
        project_id=created.project_id,
    ))

    await studio_store.delete_project(created.project_id)
    assert await isolated.jobs.delete_for_project(created.project_id) == 1
    assert await studio_store.get(created.project_id) is None
    assert await studio_store.get_release(release.release_id) is None
    assert await studio_store.get_export(export.export_id) is None
    assert await isolated.jobs.list_for_project(created.project_id) == ()

    # A movie and its storage ledger.
    movie = await movies.create(Movie(movie_id="movie-1", owner_id="user-1", name="Heist"))
    await movies.account_asset(movie.movie_id, "outputs/movies/movie-1/t.mp4", 9)
    await movies.delete(movie.movie_id)
    assert await movies.get(movie.movie_id) is None
    assert await movies.stored_bytes("user-1") == 0

    # Everything else an account holds.
    user = await users.upsert_user(email="owner@example.com")
    await keys.create(actor_id=user.actor_id, name="agent", key_hash="h" * 64)
    await usage.add_bytes(user.actor_id, 100)
    await providers.put_credential(user.actor_id, "higgsfield", b"cipher", "…1234")
    await providers.reserve_request(str(uuid4()), user.actor_id, "higgsfield", "kling-3-std")
    token_hash = hash_token(generate_pairing_token())
    await enrolment.mint_pairing_token(
        owner_id=user.actor_id, token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await enrolment.redeem_pairing_token(
        token_hash=token_hash, worker_id=str(uuid4()), label="Box",
        credential_hash=hash_token(generate_worker_credential()), now=datetime.now(UTC),
    )

    await enrolment.forget_owner(user.actor_id)
    await keys.forget_owner(user.actor_id)
    await usage.forget_owner(user.actor_id)
    await providers.forget_owner(user.actor_id)
    await users.delete_user(user.user_id)

    assert await enrolment.list_workers(user.actor_id) == ()
    assert await keys.list_for_actor(user.actor_id) == ()
    assert await usage.stored_bytes(user.actor_id) == 0
    # A purge finishing after the account is gone releases its bytes, but never
    # brings back a usage row for the deleted owner.
    assert await usage.add_bytes(user.actor_id, -40) == 0
    assert await usage.add_bytes("user-2", 50) == 50
    assert await usage.add_bytes("user-2", -80) == 0
    async with pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM studio_owner_usage WHERE owner_id = $1", user.actor_id
        ) == 0
    assert await providers.list_credentials(user.actor_id) == ()
    assert await users.get_user(user.user_id) is None
    async with pool.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM studio_provider_requests") == 0
        assert await connection.fetchval("SELECT count(*) FROM studio_pairing_tokens") == 0


async def test_retention_activity_refusals_and_owner_listings_on_the_real_schema(
    isolated: IsolatedStore,
) -> None:
    from datetime import UTC, datetime, timedelta

    from studio.test_releases import releasable_manifest

    from magic_video_editor.movies.models import Movie
    from magic_video_editor.movies.store import PostgresMovieStore
    from magic_video_editor.retention import PostgresActivityStore
    from magic_video_editor.safety import PostgresPromptRefusalStore, PromptRefusal

    pool = isolated.pool
    studio_store = PostgresStudioStore(DATABASE_URL, pool=pool)
    movies = PostgresMovieStore(DATABASE_URL, pool=pool)
    activity = PostgresActivityStore(DATABASE_URL, pool=pool)
    refusals = PostgresPromptRefusalStore(DATABASE_URL, pool=pool)
    for started in (studio_store, movies, activity, refusals):
        await started.start()
    await activity.start()  # migrations are idempotent

    created = await studio_store.create(releasable_manifest())
    await movies.create(Movie(movie_id="movie-1", owner_id="user-1", name="Heist"))
    assert await studio_store.list_project_owners() == ((created.project_id, created.manifest.owner_id),)
    assert await movies.list_owners() == [("movie-1", "user-1")]

    now = datetime.now(UTC)
    await activity.enrol("project", [("p1", "user-1"), ("p2", "user-1")], now - timedelta(days=5))
    await activity.enrol("project", [("p1", "user-1")], now)  # never moves an existing row
    await activity.touch("project", "p2", "user-1", now)
    await activity.touch("project", "p2", "user-1", now - timedelta(days=9))  # never moves back
    assert await activity.tracked("project") == {"p1": now - timedelta(days=5), "p2": now}
    assert await activity.last_active("project", ["p2", "missing"]) == {"p2": now}
    # Expiry claims only an item still idle before the cutoff; while claimed,
    # opens are refused, and releasing the claim records the given time.
    cutoff = now - timedelta(days=1)
    assert not await activity.claim("project", "p2", cutoff, now)
    assert await activity.claim("project", "p1", cutoff, now)
    assert not await activity.touch("project", "p1", "user-1", now)
    assert await activity.last_active("project", ["p1"]) == {"p1": now - timedelta(days=5)}
    assert not await activity.claim("project", "p1", cutoff, now)  # a duplicate sweep is kept out
    later = now + timedelta(minutes=31)
    assert await activity.claim("project", "p1", cutoff, later)  # a dead sweep's claim is retaken
    await activity.release("project", "p1", now - timedelta(days=3))
    assert await activity.touch("project", "p1", "user-1", now - timedelta(days=4))
    assert await activity.last_active("project", ["p1"]) == {"p1": now - timedelta(days=3)}
    await activity.forget("project", ["p1"])
    assert set(await activity.tracked("project")) == {"p2"}

    for days in (400, 1):
        await refusals.record(PromptRefusal(f"r{days}", "user-1", "motion", "sexual", "", "t",
                                            now - timedelta(days=days)))
    assert await refusals.purge_before(now - timedelta(days=365)) == 1


async def test_signing_a_user_out_everywhere_on_the_real_schema(isolated: IsolatedStore) -> None:
    from datetime import UTC, datetime, timedelta

    from magic_video_editor.studio.auth import PostgresStudioAuthStore, SignInMethod

    users = PostgresStudioAuthStore(DATABASE_URL, pool=isolated.pool)
    await users.start()
    ada = await users.upsert_user(email="ada@example.com")
    bo = await users.upsert_user(email="bo@example.com")
    later = datetime.now(UTC) + timedelta(days=30)
    for session_id, user in (("s1", ada), ("s2", ada), ("s3", bo)):
        await users.create_session(session_id=session_id, user_id=user.user_id,
                                   sign_in_method=SignInMethod.AUTH0, expires_at=later, user_agent=None)

    await users.delete_sessions_for_user(ada.user_id)

    assert await users.get_session("s1") is None and await users.get_session("s2") is None
    assert await users.get_session("s3") is not None
    assert await users.get_user(ada.user_id) is not None



async def test_keyed_artifact_usage_counts_and_releases_exactly_once(isolated: IsolatedStore) -> None:
    """Turnaround videos are counted by key, so retries never double-count or lose a count."""
    import asyncio

    from magic_video_editor.studio.limits import PostgresStudioOwnerUsageStore

    usage = PostgresStudioOwnerUsageStore(DATABASE_URL, pool=isolated.pool)
    await usage.start()
    await usage.start()  # the migrations replay
    owner = f"owner-{uuid4()}"
    key = f"outputs/studio/p/turnarounds/t/{uuid4()}.mp4"
    await usage.add_bytes(owner, 500)  # usage from other projects
    # A retry after a lost response and a concurrent settle both find the key.
    results = await asyncio.gather(*(usage.record_artifact(owner, key, 120) for _ in range(4)))
    assert sorted(results) == [False, False, False, True]
    assert await usage.record_artifact(owner, key, 120) is False
    assert await usage.stored_bytes(owner) == 620
    assert await usage.release_artifact(owner, key) == 120
    assert await usage.release_artifact(owner, key) == 0
    assert await usage.release_artifact(owner, "never-recorded.mp4") == 0
    assert await usage.stored_bytes(owner) == 500
    await usage.record_artifact(owner, key, 120)
    await usage.forget_owner(owner)
    assert await usage.stored_bytes(owner) == 0
    async with isolated.pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM studio_owner_usage_artifacts WHERE owner_id = $1", owner) == 0


async def test_keyed_usage_is_released_by_project_prefix(isolated: IsolatedStore) -> None:
    from magic_video_editor.studio.limits import PostgresStudioOwnerUsageStore

    usage = PostgresStudioOwnerUsageStore(DATABASE_URL, pool=isolated.pool)
    await usage.start()
    owner = f"owner-{uuid4()}"
    gone = "outputs/studio/project_1/turnarounds/"
    await usage.record_artifact(owner, gone + "a/1.mp4", 100)
    await usage.record_artifact(owner, gone + "b/2.mp4", 20)
    # `_` and `%` in a prefix are literal: project-X is not matched by project_1.
    await usage.record_artifact(owner, "outputs/studio/projectX1/turnarounds/c/3.mp4", 7)
    await usage.record_artifact(f"other-{uuid4()}", gone + "a/1.mp4", 50)
    assert await usage.release_artifacts(owner, gone) == 120
    assert await usage.release_artifacts(owner, gone) == 0
    assert await usage.stored_bytes(owner) == 7

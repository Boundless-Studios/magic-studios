"""Run against the repository's disposable local PostgreSQL test service."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from magic_video_editor.movies.models import Movie
from magic_video_editor.movies.store import Conflict, PostgresMovieStore


@pytest.mark.asyncio
async def test_postgres_cross_process_cas_append_only_owner_listing():
    url = os.environ.get(
        "MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL",
        "postgresql://postgres:test@127.0.0.1:15432/magic_video_editor",
    )
    try:
        admin = await asyncpg.connect(url, timeout=2)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Local PostgreSQL test service unavailable")
    schema = "movie_test_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(url, server_settings={"search_path": schema})
    try:
        a = PostgresMovieStore(url, pool=pool)
        b = PostgresMovieStore(url, pool=pool)
        await a.start()
        await b.start()
        movie = await a.create(
            Movie(movie_id=str(uuid4()), owner_id="one", name="Film")
        )
        results = await asyncio.gather(
            a.append(movie.model_copy(update={"name": "A"}), 1),
            b.append(movie.model_copy(update={"name": "B"}), 1),
            return_exceptions=True,
        )
        assert sum(isinstance(r, Conflict) for r in results) == 1
        assert [r.revision for r in await a.history(movie.movie_id)] == [1, 2]
        assert (await a.history(movie.movie_id))[0].name == "Film"
        assert await a.list("two") == []
        assert len(await b.list("one")) == 1
        await asyncio.gather(
            a.account_asset(movie.movie_id, "asset", 12),
            b.account_asset(movie.movie_id, "asset", 12),
        )
        assert await a.stored_bytes("one") == 12
        assert await b.stored_bytes("two") == 0
        assert await b.accounted_keys(movie.movie_id) == {"asset"}
        latest = await a.get(movie.movie_id)
        latest.compositions = {
            "z-older": {"status": "failed", "created_revision": 2},
            "a-newer": {"status": "pending", "created_revision": 3},
        }
        await a.append(latest, latest.revision)
        reloaded = await b.get(movie.movie_id)
        # JSONB key order is not creation order; persisted revisions are.
        ordered = sorted(
            reloaded.compositions,
            key=lambda key: reloaded.compositions[key]["created_revision"],
        )
        assert ordered == ["z-older", "a-newer"]
        await a.stop()
        assert (await b.get(movie.movie_id)).revision == 3
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.asyncio
async def test_start_failure_closes_owned_connection_pool(monkeypatch):
    class BrokenPool:
        closed = False

        def acquire(self):
            raise RuntimeError("schema unavailable")

        async def close(self):
            self.closed = True

    pool = BrokenPool()

    async def create_pool(*args, **kwargs):
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    store = PostgresMovieStore("unused")
    with pytest.raises(RuntimeError):
        await store.start()
    assert pool.closed

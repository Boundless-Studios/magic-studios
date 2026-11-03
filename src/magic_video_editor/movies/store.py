"""Append-only snapshots; compare-and-swap is the only mutation boundary."""

from __future__ import annotations

import asyncio
from importlib.resources import files
from typing import Any, Protocol

from magic_video_editor.movies.models import Movie


class MovieStore(Protocol):
    async def accounted_keys(self, movie_id: str) -> set[str]: ...
    async def account_asset(self, movie_id: str, key: str, size: int) -> None: ...
    async def stored_bytes(self, owner: str) -> int: ...
    async def create(self, movie: Movie) -> Movie: ...
    async def get(self, id: str) -> Movie | None: ...
    async def append(self, movie: Movie, expected: int) -> Movie: ...
    async def list(self, owner: str) -> list[Movie]: ...

    async def list_owners(self) -> list[tuple[str, str]]:
        """Every movie id with its owner, for the retention sweep."""
        ...
    async def history(self, id: str) -> list[Movie]: ...
    async def delete(self, id: str) -> None:
        """Permanently remove the movie, every snapshot and its storage ledger."""
        ...


class Conflict(Exception):
    def __init__(self, revision: int) -> None:
        self.revision = revision


class MemoryMovieStore:
    def __init__(self) -> None:
        self.rows = {}
        self.asset_usage: dict[str, tuple[str, int]] = {}
        self.lock = asyncio.Lock()

    async def accounted_keys(self, movie_id: str) -> set[str]:
        return {key for key, (mid, _) in self.asset_usage.items() if mid == movie_id}

    async def account_asset(self, movie_id: str, key: str, size: int) -> None:
        self.asset_usage.setdefault(key, (movie_id, size))

    async def stored_bytes(self, owner: str) -> int:
        return sum(
            size
            for mid, size in self.asset_usage.values()
            if self.rows[mid][-1].owner_id == owner
        )

    async def create(self, movie: Movie) -> Movie:
        async with self.lock:
            if movie.movie_id in self.rows:
                raise Conflict(1)
            self.rows[movie.movie_id] = [movie.model_copy(deep=True)]
        return movie.model_copy(deep=True)

    async def get(self, id: str) -> Movie | None:
        rows = self.rows.get(id)
        return rows[-1].model_copy(deep=True) if rows else None

    async def append(self, movie: Movie, expected: int) -> Movie:
        async with self.lock:
            rows = self.rows.get(movie.movie_id, [])
            if not rows or rows[-1].revision != expected:
                raise Conflict(rows[-1].revision if rows else 0)
            movie = movie.model_copy(deep=True, update={"revision": expected + 1})
            rows.append(movie)
            return movie.model_copy(deep=True)

    async def list(self, owner: str) -> list[Movie]:
        return [
            r[-1].model_copy(deep=True)
            for r in self.rows.values()
            if r[-1].owner_id == owner
        ]

    async def list_owners(self) -> list[tuple[str, str]]:
        return [(id, rows[-1].owner_id) for id, rows in self.rows.items() if rows]

    async def history(self, id: str) -> list[Movie]:
        return [r.model_copy(deep=True) for r in self.rows.get(id, [])]

    async def delete(self, id: str) -> None:
        async with self.lock:
            for key in [key for key, (mid, _) in self.asset_usage.items() if mid == id]:
                del self.asset_usage[key]
            self.rows.pop(id, None)


class PostgresMovieStore:
    def __init__(self, database_url: str, *, pool: Any = None) -> None:
        self.url = database_url
        self.pool = pool
        self.owns_pool = False

    async def start(self) -> None:
        import asyncpg

        if self.pool is None:
            self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=4)
            self.owns_pool = True
        try:
            async with self.pool.acquire() as c, c.transaction():
                await c.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('movie_schema_v1'))"
                )
                await c.execute(
                    files("magic_video_editor.migrations")
                    .joinpath("017_movies.sql")
                    .read_text()
                )
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self.pool and self.owns_pool:
            await self.pool.close()
            self.pool = None

    async def accounted_keys(self, movie_id: str) -> set[str]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT asset_key FROM movie_asset_usage WHERE movie_id=$1", movie_id
            )
        return {r["asset_key"] for r in rows}

    async def account_asset(self, movie_id: str, key: str, size: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO movie_asset_usage(asset_key,movie_id,size_bytes) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                key,
                movie_id,
                size,
            )

    async def delete(self, id: str) -> None:
        # Children first: these foreign keys do not cascade.
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("DELETE FROM movie_asset_usage WHERE movie_id=$1", id)
            await c.execute("DELETE FROM movie_revisions WHERE movie_id=$1", id)
            await c.execute("DELETE FROM movie_projects WHERE movie_id=$1", id)

    async def list_owners(self) -> list[tuple[str, str]]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT movie_id, owner_id FROM movie_projects")
        return [(r["movie_id"], r["owner_id"]) for r in rows]

    async def stored_bytes(self, owner: str) -> int:
        async with self.pool.acquire() as c:
            return int(
                await c.fetchval(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM movie_asset_usage JOIN movie_projects USING(movie_id) WHERE owner_id=$1",
                    owner,
                )
            )

    async def create(self, movie: Movie) -> Movie:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                "INSERT INTO movie_projects(movie_id,owner_id,current_revision) VALUES($1,$2,1)",
                movie.movie_id,
                movie.owner_id,
            )
            await c.execute(
                "INSERT INTO movie_revisions(movie_id,revision,snapshot) VALUES($1,1,$2::jsonb)",
                movie.movie_id,
                movie.model_dump_json(),
            )
        return movie

    async def get(self, id: str) -> Movie | None:
        async with self.pool.acquire() as c:
            row = await c.fetchval(
                "SELECT snapshot FROM movie_revisions r JOIN movie_projects p USING(movie_id) WHERE movie_id=$1 AND revision=current_revision",
                id,
            )
        return Movie.model_validate_json(row) if row else None

    async def append(self, movie: Movie, expected: int) -> Movie:
        async with self.pool.acquire() as c, c.transaction():
            revision = await c.fetchval(
                "SELECT current_revision FROM movie_projects WHERE movie_id=$1 FOR UPDATE",
                movie.movie_id,
            )
            if revision != expected:
                raise Conflict(revision or 0)
            movie = movie.model_copy(update={"revision": expected + 1})
            await c.execute(
                "INSERT INTO movie_revisions(movie_id,revision,snapshot) VALUES($1,$2,$3::jsonb)",
                movie.movie_id,
                movie.revision,
                movie.model_dump_json(),
            )
            await c.execute(
                "UPDATE movie_projects SET current_revision=$2 WHERE movie_id=$1",
                movie.movie_id,
                movie.revision,
            )
        return movie

    async def list(self, owner: str) -> list[Movie]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT snapshot FROM movie_projects p JOIN movie_revisions r USING(movie_id) WHERE owner_id=$1 AND revision=current_revision ORDER BY p.created_at DESC",
                owner,
            )
        return [Movie.model_validate_json(r["snapshot"]) for r in rows]

    async def history(self, id: str) -> list[Movie]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT snapshot FROM movie_revisions WHERE movie_id=$1 ORDER BY revision",
                id,
            )
        return [Movie.model_validate_json(r["snapshot"]) for r in rows]

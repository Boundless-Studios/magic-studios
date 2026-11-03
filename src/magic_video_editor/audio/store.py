"""Append-only audio set snapshots; compare-and-swap is the only mutation boundary."""

from __future__ import annotations

import asyncio
from importlib.resources import files
from typing import Any, Protocol

from magic_video_editor.audio.models import AudioSet


class AudioStore(Protocol):
    async def accounted_keys(self, set_id: str) -> set[str]: ...
    async def account_asset(self, set_id: str, key: str, size: int) -> None: ...
    async def stored_bytes(self, owner: str) -> int: ...
    async def create(self, audio_set: AudioSet) -> AudioSet: ...
    async def get(self, id: str) -> AudioSet | None: ...
    async def append(self, audio_set: AudioSet, expected: int) -> AudioSet: ...
    async def list(self, owner: str) -> list[AudioSet]: ...
    async def delete(self, id: str) -> None:
        """Permanently remove the set, every snapshot and its storage ledger."""
        ...


class Conflict(Exception):
    def __init__(self, revision: int) -> None:
        self.revision = revision


class MemoryAudioStore:
    def __init__(self) -> None:
        self.rows: dict[str, list[AudioSet]] = {}
        self.asset_usage: dict[str, tuple[str, int]] = {}
        self.lock = asyncio.Lock()

    async def accounted_keys(self, set_id: str) -> set[str]:
        return {key for key, (sid, _) in self.asset_usage.items() if sid == set_id}

    async def account_asset(self, set_id: str, key: str, size: int) -> None:
        self.asset_usage.setdefault(key, (set_id, size))

    async def stored_bytes(self, owner: str) -> int:
        return sum(
            size
            for sid, size in self.asset_usage.values()
            if sid in self.rows and self.rows[sid][-1].owner_id == owner
        )

    async def create(self, audio_set: AudioSet) -> AudioSet:
        async with self.lock:
            if audio_set.set_id in self.rows:
                raise Conflict(1)
            self.rows[audio_set.set_id] = [audio_set.model_copy(deep=True)]
        return audio_set.model_copy(deep=True)

    async def get(self, id: str) -> AudioSet | None:
        rows = self.rows.get(id)
        return rows[-1].model_copy(deep=True) if rows else None

    async def append(self, audio_set: AudioSet, expected: int) -> AudioSet:
        async with self.lock:
            rows = self.rows.get(audio_set.set_id, [])
            if not rows or rows[-1].revision != expected:
                raise Conflict(rows[-1].revision if rows else 0)
            audio_set = audio_set.model_copy(deep=True, update={"revision": expected + 1})
            rows.append(audio_set)
            return audio_set.model_copy(deep=True)

    async def list(self, owner: str) -> list[AudioSet]:
        return [
            r[-1].model_copy(deep=True) for r in self.rows.values() if r[-1].owner_id == owner
        ]

    async def delete(self, id: str) -> None:
        async with self.lock:
            for key in [key for key, (sid, _) in self.asset_usage.items() if sid == id]:
                del self.asset_usage[key]
            self.rows.pop(id, None)


class PostgresAudioStore:
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
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('audio_schema_v1'))")
                await c.execute(
                    files("magic_video_editor.migrations").joinpath("024_audio_sets.sql").read_text()
                )
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self.pool and self.owns_pool:
            await self.pool.close()
            self.pool = None

    async def accounted_keys(self, set_id: str) -> set[str]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT asset_key FROM audio_asset_usage WHERE set_id=$1", set_id)
        return {r["asset_key"] for r in rows}

    async def account_asset(self, set_id: str, key: str, size: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO audio_asset_usage(asset_key,set_id,size_bytes) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                key,
                set_id,
                size,
            )

    async def delete(self, id: str) -> None:
        # Children first: these foreign keys do not cascade.
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("DELETE FROM audio_asset_usage WHERE set_id=$1", id)
            await c.execute("DELETE FROM audio_set_revisions WHERE set_id=$1", id)
            await c.execute("DELETE FROM audio_sets WHERE set_id=$1", id)

    async def stored_bytes(self, owner: str) -> int:
        async with self.pool.acquire() as c:
            return int(
                await c.fetchval(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM audio_asset_usage JOIN audio_sets USING(set_id) WHERE owner_id=$1",
                    owner,
                )
            )

    async def create(self, audio_set: AudioSet) -> AudioSet:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                "INSERT INTO audio_sets(set_id,owner_id,current_revision) VALUES($1,$2,1)",
                audio_set.set_id,
                audio_set.owner_id,
            )
            await c.execute(
                "INSERT INTO audio_set_revisions(set_id,revision,snapshot) VALUES($1,1,$2::jsonb)",
                audio_set.set_id,
                audio_set.model_dump_json(),
            )
        return audio_set

    async def get(self, id: str) -> AudioSet | None:
        async with self.pool.acquire() as c:
            row = await c.fetchval(
                "SELECT snapshot FROM audio_set_revisions r JOIN audio_sets s USING(set_id) WHERE set_id=$1 AND revision=current_revision",
                id,
            )
        return AudioSet.model_validate_json(row) if row else None

    async def append(self, audio_set: AudioSet, expected: int) -> AudioSet:
        async with self.pool.acquire() as c, c.transaction():
            revision = await c.fetchval(
                "SELECT current_revision FROM audio_sets WHERE set_id=$1 FOR UPDATE",
                audio_set.set_id,
            )
            if revision != expected:
                raise Conflict(revision or 0)
            audio_set = audio_set.model_copy(update={"revision": expected + 1})
            await c.execute(
                "INSERT INTO audio_set_revisions(set_id,revision,snapshot) VALUES($1,$2,$3::jsonb)",
                audio_set.set_id,
                audio_set.revision,
                audio_set.model_dump_json(),
            )
            await c.execute(
                "UPDATE audio_sets SET current_revision=$2 WHERE set_id=$1",
                audio_set.set_id,
                audio_set.revision,
            )
        return audio_set

    async def list(self, owner: str) -> list[AudioSet]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT snapshot FROM audio_sets s JOIN audio_set_revisions r USING(set_id) WHERE owner_id=$1 AND revision=current_revision ORDER BY s.created_at DESC",
                owner,
            )
        return [AudioSet.model_validate_json(r["snapshot"]) for r in rows]

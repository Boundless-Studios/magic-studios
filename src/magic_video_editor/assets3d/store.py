"""Append-only 3D asset snapshots; compare-and-swap is the only mutation boundary."""

from __future__ import annotations

import asyncio
from importlib.resources import files
from typing import Any, Protocol

from magic_video_editor.assets3d.models import Asset3D


class Assets3DStore(Protocol):
    async def accounted_keys(self, asset_id: str) -> set[str]: ...
    async def account_asset(self, asset_id: str, key: str, size: int) -> None: ...
    async def settle_asset(self, asset_id: str, key: str, size: int) -> None:
        """Set a key's counted size, replacing a reservation with the real size."""
        ...

    async def forget_asset(self, asset_id: str, key: str) -> None:
        """Drop a reservation for a file that was never stored."""
        ...

    async def append_reserving(
        self, asset: Asset3D, expected: int, owner: str, key: str, size: int, allowance: int | None
    ) -> Asset3D:
        """`append`, and count `size` bytes for `key`, in one transaction: both or
        neither. The reservation must keep the owner's 3D total within `allowance`
        (None: no limit); the check and the write are one step per owner across
        every API instance, so concurrent grants cannot all pass against the same
        total. Raises `Conflict` on a stale revision and `OverAllowance` when it
        would not fit; neither leaves anything behind."""
        ...

    async def mark_deleting(self, id: str) -> None:
        """Record, as a new revision regardless of the current one, that the asset
        is being deleted. Writers that store files check it after writing."""
        ...

    async def stored_bytes(self, owner: str) -> int: ...
    async def create(self, asset: Asset3D) -> Asset3D: ...
    async def get(self, id: str) -> Asset3D | None: ...
    async def append(self, asset: Asset3D, expected: int) -> Asset3D: ...
    async def list(self, owner: str) -> list[Asset3D]: ...
    async def delete(self, id: str) -> None:
        """Permanently remove the asset, every snapshot and its storage ledger."""
        ...


class Conflict(Exception):
    def __init__(self, revision: int) -> None:
        self.revision = revision


class OverAllowance(Exception):
    """A reservation would take the owner past what they may store."""


class MemoryAssets3DStore:
    def __init__(self) -> None:
        self.rows: dict[str, list[Asset3D]] = {}
        self.asset_usage: dict[str, tuple[str, int]] = {}
        self.lock = asyncio.Lock()

    async def accounted_keys(self, asset_id: str) -> set[str]:
        return {key for key, (aid, _) in self.asset_usage.items() if aid == asset_id}

    async def account_asset(self, asset_id: str, key: str, size: int) -> None:
        self.asset_usage.setdefault(key, (asset_id, size))

    async def settle_asset(self, asset_id: str, key: str, size: int) -> None:
        self.asset_usage[key] = (asset_id, size)

    async def forget_asset(self, asset_id: str, key: str) -> None:
        if self.asset_usage.get(key, (None, 0))[0] == asset_id:
            del self.asset_usage[key]

    async def append_reserving(
        self, asset: Asset3D, expected: int, owner: str, key: str, size: int, allowance: int | None
    ) -> Asset3D:
        async with self.lock:
            rows = self.rows.get(asset.asset_id, [])
            if not rows or rows[-1].revision != expected:
                raise Conflict(rows[-1].revision if rows else 0)
            used = sum(
                s for k, (aid, s) in self.asset_usage.items()
                if k != key and aid in self.rows and self.rows[aid][-1].owner_id == owner
            )
            if allowance is not None and used + size > allowance:
                raise OverAllowance()
            asset = asset.model_copy(deep=True, update={"revision": expected + 1})
            rows.append(asset)
            self.asset_usage[key] = (asset.asset_id, size)
            return asset.model_copy(deep=True)

    async def mark_deleting(self, id: str) -> None:
        async with self.lock:
            rows = self.rows.get(id)
            if rows and not rows[-1].deleting:
                rows.append(rows[-1].model_copy(
                    deep=True, update={"deleting": True, "revision": rows[-1].revision + 1}
                ))

    async def stored_bytes(self, owner: str) -> int:
        return sum(
            size
            for aid, size in self.asset_usage.values()
            if aid in self.rows and self.rows[aid][-1].owner_id == owner
        )

    async def create(self, asset: Asset3D) -> Asset3D:
        async with self.lock:
            if asset.asset_id in self.rows:
                raise Conflict(1)
            self.rows[asset.asset_id] = [asset.model_copy(deep=True)]
        return asset.model_copy(deep=True)

    async def get(self, id: str) -> Asset3D | None:
        rows = self.rows.get(id)
        return rows[-1].model_copy(deep=True) if rows else None

    async def append(self, asset: Asset3D, expected: int) -> Asset3D:
        async with self.lock:
            rows = self.rows.get(asset.asset_id, [])
            if not rows or rows[-1].revision != expected:
                raise Conflict(rows[-1].revision if rows else 0)
            asset = asset.model_copy(deep=True, update={"revision": expected + 1})
            rows.append(asset)
            return asset.model_copy(deep=True)

    async def list(self, owner: str) -> list[Asset3D]:
        return [
            r[-1].model_copy(deep=True) for r in self.rows.values() if r[-1].owner_id == owner
        ]

    async def delete(self, id: str) -> None:
        async with self.lock:
            for key in [key for key, (aid, _) in self.asset_usage.items() if aid == id]:
                del self.asset_usage[key]
            self.rows.pop(id, None)


class PostgresAssets3DStore:
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
                await c.execute("SELECT pg_advisory_xact_lock(hashtext('assets_3d_schema_v1'))")
                await c.execute(
                    files("magic_video_editor.migrations").joinpath("025_assets_3d.sql").read_text()
                )
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        if self.pool and self.owns_pool:
            await self.pool.close()
            self.pool = None

    async def accounted_keys(self, asset_id: str) -> set[str]:
        async with self.pool.acquire() as c:
            rows = await c.fetch("SELECT asset_key FROM asset_3d_usage WHERE asset_id=$1", asset_id)
        return {r["asset_key"] for r in rows}

    async def account_asset(self, asset_id: str, key: str, size: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO asset_3d_usage(asset_key,asset_id,size_bytes) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                key,
                asset_id,
                size,
            )

    async def settle_asset(self, asset_id: str, key: str, size: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "INSERT INTO asset_3d_usage(asset_key,asset_id,size_bytes) VALUES($1,$2,$3) "
                "ON CONFLICT (asset_key) DO UPDATE SET size_bytes=EXCLUDED.size_bytes "
                "WHERE asset_3d_usage.asset_id=EXCLUDED.asset_id",
                key,
                asset_id,
                size,
            )

    async def forget_asset(self, asset_id: str, key: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "DELETE FROM asset_3d_usage WHERE asset_key=$1 AND asset_id=$2", key, asset_id
            )

    async def append_reserving(
        self, asset: Asset3D, expected: int, owner: str, key: str, size: int, allowance: int | None
    ) -> Asset3D:
        async with self.pool.acquire() as c, c.transaction():
            # One owner's reservations queue behind each other on every instance;
            # other owners and other sections are not held up. The owner lock is
            # always taken before the asset's row lock, never after.
            await c.execute(
                "SELECT pg_advisory_xact_lock(hashtext('assets_3d_owner_storage'), hashtext($1))", owner
            )
            revision = await c.fetchval(
                "SELECT current_revision FROM assets_3d WHERE asset_id=$1 FOR UPDATE",
                asset.asset_id,
            )
            if revision != expected:
                raise Conflict(revision or 0)
            if allowance is not None:
                used = await c.fetchval(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM asset_3d_usage JOIN assets_3d USING(asset_id) "
                    "WHERE owner_id=$1 AND asset_key<>$2",
                    owner,
                    key,
                )
                if used + size > allowance:
                    raise OverAllowance()
            asset = asset.model_copy(update={"revision": expected + 1})
            await c.execute(
                "INSERT INTO asset_3d_revisions(asset_id,revision,snapshot) VALUES($1,$2,$3::jsonb)",
                asset.asset_id,
                asset.revision,
                asset.model_dump_json(),
            )
            await c.execute(
                "UPDATE assets_3d SET current_revision=$2 WHERE asset_id=$1",
                asset.asset_id,
                asset.revision,
            )
            await c.execute(
                "INSERT INTO asset_3d_usage(asset_key,asset_id,size_bytes) VALUES($1,$2,$3) "
                "ON CONFLICT (asset_key) DO UPDATE SET size_bytes=EXCLUDED.size_bytes "
                "WHERE asset_3d_usage.asset_id=EXCLUDED.asset_id",
                key,
                asset.asset_id,
                size,
            )
        return asset

    async def mark_deleting(self, id: str) -> None:
        async with self.pool.acquire() as c, c.transaction():
            revision = await c.fetchval(
                "SELECT current_revision FROM assets_3d WHERE asset_id=$1 FOR UPDATE", id
            )
            if revision is None:
                return
            snapshot = await c.fetchval(
                "SELECT snapshot FROM asset_3d_revisions WHERE asset_id=$1 AND revision=$2", id, revision
            )
            asset = Asset3D.model_validate_json(snapshot)
            if asset.deleting:
                return
            asset = asset.model_copy(update={"deleting": True, "revision": revision + 1})
            await c.execute(
                "INSERT INTO asset_3d_revisions(asset_id,revision,snapshot) VALUES($1,$2,$3::jsonb)",
                id,
                asset.revision,
                asset.model_dump_json(),
            )
            await c.execute("UPDATE assets_3d SET current_revision=$2 WHERE asset_id=$1", id, asset.revision)

    async def delete(self, id: str) -> None:
        # Children first: these foreign keys do not cascade.
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("DELETE FROM asset_3d_usage WHERE asset_id=$1", id)
            await c.execute("DELETE FROM asset_3d_revisions WHERE asset_id=$1", id)
            await c.execute("DELETE FROM assets_3d WHERE asset_id=$1", id)

    async def stored_bytes(self, owner: str) -> int:
        async with self.pool.acquire() as c:
            return int(
                await c.fetchval(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM asset_3d_usage JOIN assets_3d USING(asset_id) WHERE owner_id=$1",
                    owner,
                )
            )

    async def create(self, asset: Asset3D) -> Asset3D:
        async with self.pool.acquire() as c, c.transaction():
            await c.execute(
                "INSERT INTO assets_3d(asset_id,owner_id,current_revision) VALUES($1,$2,1)",
                asset.asset_id,
                asset.owner_id,
            )
            await c.execute(
                "INSERT INTO asset_3d_revisions(asset_id,revision,snapshot) VALUES($1,1,$2::jsonb)",
                asset.asset_id,
                asset.model_dump_json(),
            )
        return asset

    async def get(self, id: str) -> Asset3D | None:
        async with self.pool.acquire() as c:
            row = await c.fetchval(
                "SELECT snapshot FROM asset_3d_revisions r JOIN assets_3d s USING(asset_id) WHERE asset_id=$1 AND revision=current_revision",
                id,
            )
        return Asset3D.model_validate_json(row) if row else None

    async def append(self, asset: Asset3D, expected: int) -> Asset3D:
        async with self.pool.acquire() as c, c.transaction():
            revision = await c.fetchval(
                "SELECT current_revision FROM assets_3d WHERE asset_id=$1 FOR UPDATE",
                asset.asset_id,
            )
            if revision != expected:
                raise Conflict(revision or 0)
            asset = asset.model_copy(update={"revision": expected + 1})
            await c.execute(
                "INSERT INTO asset_3d_revisions(asset_id,revision,snapshot) VALUES($1,$2,$3::jsonb)",
                asset.asset_id,
                asset.revision,
                asset.model_dump_json(),
            )
            await c.execute(
                "UPDATE assets_3d SET current_revision=$2 WHERE asset_id=$1",
                asset.asset_id,
                asset.revision,
            )
        return asset

    async def list(self, owner: str) -> list[Asset3D]:
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT snapshot FROM assets_3d s JOIN asset_3d_revisions r USING(asset_id) WHERE owner_id=$1 AND revision=current_revision ORDER BY s.created_at DESC",
                owner,
            )
        return [Asset3D.model_validate_json(r["snapshot"]) for r in rows]

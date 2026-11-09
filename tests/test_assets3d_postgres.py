"""Run against the repository's disposable local PostgreSQL test service."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from magic_video_editor.assets3d.models import Asset3D, Asset3DBounds, Asset3DCandidate, GlbMeasurement
from magic_video_editor.assets3d.store import Conflict, OverAllowance, PostgresAssets3DStore


@pytest.mark.asyncio
async def test_postgres_assets3d_store_cas_listing_accounting_and_delete():
    url = os.environ.get(
        "MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL",
        "postgresql://postgres:test@127.0.0.1:15432/magic_video_editor",
    )
    try:
        admin = await asyncpg.connect(url, timeout=2)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Local PostgreSQL test service unavailable")
    schema = "assets3d_test_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(url, server_settings={"search_path": schema})
    try:
        a = PostgresAssets3DStore(url, pool=pool)
        b = PostgresAssets3DStore(url, pool=pool)
        await a.start()
        await b.start()
        asset = await a.create(Asset3D(asset_id=str(uuid4()), owner_id="one", name="Crate"))
        results = await asyncio.gather(
            a.append(asset.model_copy(update={"name": "A"}), 1),
            b.append(asset.model_copy(update={"name": "B"}), 1),
            return_exceptions=True,
        )
        assert sum(isinstance(r, Conflict) for r in results) == 1
        assert (await b.get(asset.asset_id)).revision == 2
        # A full snapshot (candidate, measurements, selection) reads back unchanged.
        current = await b.get(asset.asset_id)
        current.candidates.append(Asset3DCandidate(
            candidate_id="m1", stage="mesh", origin="upload", asset_key="k", sha256="0" * 64,
            byte_length=4, created_at="2026-11-02T00:00:00+00:00",
            measured=GlbMeasurement(
                meshes=1, primitives=1, triangles=2, other_primitives=0, vertices=4, nodes=1,
                materials=0, textures=0, images=0, skins=0, joints=0, animations=0,
                bounds=Asset3DBounds(min=[0, 0, 0], max=[1, 1, 1]),
            ),
        ))
        current.selections = {"mesh": "m1"}
        saved = await a.append(current, 2)
        assert await b.get(asset.asset_id) == saved
        assert await a.list("two") == []
        assert len(await b.list("one")) == 1
        await asyncio.gather(
            a.account_asset(asset.asset_id, "asset", 12),
            b.account_asset(asset.asset_id, "asset", 12),
        )
        assert await a.stored_bytes("one") == 12
        assert await b.accounted_keys(asset.asset_id) == {"asset"}
        # A grant is reserved at its limit, settled to the real size, or forgotten.
        await a.settle_asset(asset.asset_id, "grant", 1000)
        assert await b.stored_bytes("one") == 1012
        await b.settle_asset(asset.asset_id, "grant", 30)
        assert await a.stored_bytes("one") == 42
        await a.settle_asset(asset.asset_id, "never", 500)
        await b.forget_asset(asset.asset_id, "never")
        assert await a.stored_bytes("one") == 42
        assert await b.accounted_keys(asset.asset_id) == {"asset", "grant"}
        # Concurrent grants from two stores (two API instances) never pass the
        # allowance together: exactly five of sixteen 200-byte grants fit, and a
        # refused or conflicting grant leaves neither revision nor reservation.
        others = [await a.create(Asset3D(asset_id=str(uuid4()), owner_id="one", name=f"O{i}"))
                  for i in range(16)]
        results = await asyncio.gather(*(
            (a if i % 2 else b).append_reserving(o, 1, "one", f"grant-{i}", 200, 1042)
            for i, o in enumerate(others)
        ), return_exceptions=True)
        assert sum(isinstance(r, Asset3D) for r in results) == 5
        assert sum(isinstance(r, OverAllowance) for r in results) == 11
        assert await a.stored_bytes("one") == 42 + 5 * 200
        refused = [o for o, r in zip(others, results) if isinstance(r, OverAllowance)]
        assert [(await b.get(o.asset_id)).revision for o in refused] == [1] * 11
        try:
            await a.append_reserving(others[0], 1, "one", "stale", 1, None)
            raise AssertionError("a stale revision must conflict")
        except Conflict:
            pass
        assert "stale" not in await a.accounted_keys(others[0].asset_id)
        for o in others:
            await a.delete(o.asset_id)
        # The deletion mark is a new revision, whatever the current one is.
        before = (await a.get(asset.asset_id)).revision
        await b.mark_deleting(asset.asset_id)
        marked = await a.get(asset.asset_id)
        assert marked.deleting and marked.revision == before + 1
        await a.mark_deleting(asset.asset_id)
        assert (await b.get(asset.asset_id)).revision == before + 1
        await a.delete(asset.asset_id)
        assert await b.get(asset.asset_id) is None
        assert await a.stored_bytes("one") == 0
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()

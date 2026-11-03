"""Run against the repository's disposable local PostgreSQL test service."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest

from magic_video_editor.audio.models import AudioSet
from magic_video_editor.audio.store import Conflict, PostgresAudioStore


@pytest.mark.asyncio
async def test_postgres_audio_store_cas_listing_accounting_and_delete():
    url = os.environ.get(
        "MAGIC_VIDEO_EDITOR_TEST_DATABASE_URL",
        "postgresql://postgres:test@127.0.0.1:15432/magic_video_editor",
    )
    try:
        admin = await asyncpg.connect(url, timeout=2)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("Local PostgreSQL test service unavailable")
    schema = "audio_test_" + uuid4().hex
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(url, server_settings={"search_path": schema})
    try:
        a = PostgresAudioStore(url, pool=pool)
        b = PostgresAudioStore(url, pool=pool)
        await a.start()
        await b.start()
        audio_set = await a.create(AudioSet(set_id=str(uuid4()), owner_id="one", name="Glass"))
        results = await asyncio.gather(
            a.append(audio_set.model_copy(update={"name": "A"}), 1),
            b.append(audio_set.model_copy(update={"name": "B"}), 1),
            return_exceptions=True,
        )
        assert sum(isinstance(r, Conflict) for r in results) == 1
        assert (await b.get(audio_set.set_id)).revision == 2
        assert await a.list("two") == []
        assert len(await b.list("one")) == 1
        await asyncio.gather(
            a.account_asset(audio_set.set_id, "asset", 12),
            b.account_asset(audio_set.set_id, "asset", 12),
        )
        assert await a.stored_bytes("one") == 12
        assert await b.accounted_keys(audio_set.set_id) == {"asset"}
        await a.delete(audio_set.set_id)
        assert await b.get(audio_set.set_id) is None
        assert await a.stored_bytes("one") == 0
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()

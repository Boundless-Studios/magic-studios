"""A signed URL is a bearer capability, so its lifetime is a security control.

These lock the two named constants and prove every mint -- GCS and the in-memory
plugin alike -- goes through them rather than an inline `timedelta`.
"""

from datetime import timedelta

import pytest

from magic_video_editor.memory import MemoryAssetStore
from magic_video_editor.plugins.gcs import (
    SIGNED_READ_URL_TTL,
    SIGNED_UPLOAD_URL_TTL,
    GcsAssetStore,
)


class _Blob:
    def __init__(self, content: bytes | None = None) -> None:
        self.content = content
        self.size = len(content) if content is not None else None
        self.generation: int | None = 7
        self.content_type: str | None = None
        self.signed_url_options: dict[str, object] | None = None

    def reload(self) -> None:
        return None

    def generate_signed_url(self, **kwargs: object) -> str:
        self.signed_url_options = kwargs
        return "https://signed.invalid/url"


class _Bucket:
    def __init__(self, blobs: dict[str, _Blob]) -> None:
        self.blobs = blobs

    def blob(self, key: str) -> _Blob:
        return self.blobs.setdefault(key, _Blob())


class _Client:
    def __init__(self, buckets: dict[str, _Bucket]) -> None:
        self.buckets = buckets

    def bucket(self, name: str) -> _Bucket:
        return self.buckets[name]


def _store(bucket: _Bucket) -> GcsAssetStore:
    return GcsAssetStore(
        client=_Client({"magic-output": bucket}),
        output_bucket="magic-output",
        signing_service_account_email="signer@example.invalid",
        signing_access_token="token",
    )


def test_upload_urls_expire_in_fifteen_minutes() -> None:
    assert SIGNED_UPLOAD_URL_TTL == timedelta(minutes=15)


async def test_gcs_source_upload_url_uses_the_upload_lifetime() -> None:
    blob = _Blob()
    bucket = _Bucket({"sources/studio/p1/subjects/a.png": blob})

    await _store(bucket).create_upload_url(
        "sources/studio/p1/subjects/a.png", "image/png"
    )

    assert blob.signed_url_options is not None
    assert blob.signed_url_options["expiration"] == SIGNED_UPLOAD_URL_TTL
    assert blob.signed_url_options["method"] == "PUT"


async def test_gcs_output_upload_url_uses_the_upload_lifetime() -> None:
    blob = _Blob()
    bucket = _Bucket({"outputs/pool/job.png": blob})

    await _store(bucket).create_output_upload_url("outputs/pool/job.png", "image/png")

    assert blob.signed_url_options is not None
    assert blob.signed_url_options["expiration"] == SIGNED_UPLOAD_URL_TTL


async def test_memory_asset_store_mirrors_the_same_lifetimes() -> None:
    assets = MemoryAssetStore()
    assets.assets["outputs/studio/p1/subjects/a.png"] = b"bytes"

    await assets.create_upload_url("sources/studio/p1/subjects/a.png", "image/png")
    await assets.create_output_upload_url("outputs/pool/job.png", "image/png")

    assert assets.minted_url_ttls == [
        ("sources/studio/p1/subjects/a.png", SIGNED_UPLOAD_URL_TTL),
        ("outputs/pool/job.png", SIGNED_UPLOAD_URL_TTL),
    ]


async def test_worker_input_grant_is_a_five_minute_get() -> None:
    bucket = _Bucket({})
    key = "outputs/movies/flight/takes/take.first-768x448.png"

    await _store(bucket).create_read_url(key)

    options = bucket.blobs[key].signed_url_options
    assert SIGNED_READ_URL_TTL == timedelta(minutes=5)
    assert options["method"] == "GET"
    assert options["expiration"] == SIGNED_READ_URL_TTL
    assert "response_disposition" not in options


async def test_worker_input_grant_refuses_keys_outside_job_inputs() -> None:
    with pytest.raises(ValueError, match="pool job input"):
        await _store(_Bucket({})).create_read_url("exports/studio/project/export.zip")


async def test_memory_store_grants_only_existing_inputs() -> None:
    store = MemoryAssetStore()
    store.assets["sources/pool/frame.png"] = b"png"

    assert await store.create_read_url("sources/pool/frame.png") == "memory://read/sources/pool/frame.png"
    assert store.minted_url_ttls[-1] == ("sources/pool/frame.png", SIGNED_READ_URL_TTL)

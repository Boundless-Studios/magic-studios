from datetime import timedelta

import pytest
from google.api_core.exceptions import NotFound

from magic_video_editor.contracts import (
    AssetNotFound,
    AssetStorageUnavailable,
    AssetTooLarge,
)
from magic_video_editor.plugins.gcs import GcsAssetStore
from magic_video_editor.studio.memory import MemoryStudioStore
from magic_video_editor.studio.service import StudioService


class _Blob:
    def __init__(
        self,
        content: bytes | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.content = content
        self.error = error
        self.size = len(content) if content is not None else None
        self.generation: int | None = 7
        self.generation_match: int | None = None
        self.content_type: str | None = None
        self.signed_url_options: dict[str, object] | None = None
        self.download_call_count = 0
        self.last_download_kwargs: dict[str, object] | None = None
        self.reload_call_count = 0

    def reload(self) -> None:
        self.reload_call_count += 1
        if self.error is not None:
            raise self.error

    def download_as_bytes(self, **kwargs: object) -> bytes:
        self.download_call_count += 1
        self.last_download_kwargs = kwargs
        if self.error is not None:
            raise self.error
        if self.content is None:
            raise FileNotFoundError
        start = int(kwargs.get("start", 0) or 0)
        end = kwargs.get("end")
        if end is None:
            return self.content[start:]
        return self.content[start : int(end) + 1]

    def upload_from_string(
        self,
        content: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None:
        self.generation_match = if_generation_match
        self.content_type = content_type
        if self.content is not None:
            raise _PreconditionFailed
        self.content = content

    def generate_signed_url(self, **kwargs: object) -> str:
        self.signed_url_options = kwargs
        assert kwargs["version"] == "v4"
        assert kwargs["method"] in {"GET", "PUT"}
        return "https://upload.invalid/signed"


class _Bucket:
    def __init__(self, blobs: dict[str, _Blob]) -> None:
        self.blobs = blobs

    def blob(self, key: str) -> _Blob:
        return self.blobs.setdefault(key, _Blob())

    def exists(self) -> bool:
        return True


class _Client:
    def __init__(self, buckets: dict[str, _Bucket]) -> None:
        self.buckets = buckets

    def bucket(self, name: str) -> _Bucket:
        return self.buckets[name]

    def list_blobs(self, bucket_name: str, *, max_results: int):
        assert max_results == 1
        return iter(self.buckets[bucket_name].blobs.values())


class _PreconditionFailed(Exception):
    pass


class _UnavailableClient:
    def list_blobs(self, bucket_name: str, *, max_results: int):
        raise PermissionError(f"cannot list {bucket_name}")


def test_construction_does_not_resolve_ambient_credentials(monkeypatch) -> None:
    def reject_client_construction():
        raise AssertionError("credentials resolved before startup")

    monkeypatch.setattr(
        "magic_video_editor.plugins.gcs.storage.Client",
        reject_client_construction,
    )

    GcsAssetStore(output_bucket="magic-output")


async def test_reads_only_from_the_editor_bucket() -> None:
    output = _Bucket({"inputs/source.mp4": _Blob(b"source")})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    content = await store.read("inputs/source.mp4")

    assert content == b"source"


async def test_missing_gcs_objects_map_to_asset_not_found() -> None:
    output = _Bucket({"inputs/source.mp4": _Blob(error=NotFound("missing"))})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    with pytest.raises(AssetNotFound):
        await store.read("inputs/source.mp4")


async def test_bounded_read_rejects_oversized_gcs_blob_from_metadata_without_download() -> None:
    blob = _Blob(b"x" * 32)
    output = _Bucket({"inputs/source.png": blob})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    with pytest.raises(AssetTooLarge):
        await store.read_limited("inputs/source.png", max_bytes=16)

    assert blob.reload_call_count == 1
    assert blob.download_call_count == 0


async def test_bounded_read_normalizes_non_runtime_provider_failure() -> None:
    output = _Bucket({"inputs/source.png": _Blob(error=PermissionError("denied"))})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}), output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    with pytest.raises(AssetStorageUnavailable) as error:
        await store.read_limited("inputs/source.png", max_bytes=16)

    assert isinstance(error.value.__cause__, PermissionError)


async def test_bounded_read_downloads_at_most_max_bytes_plus_one_with_generation_pin() -> None:
    blob = _Blob(b"1234567890")
    blob.size = None
    output = _Bucket({"inputs/source.png": blob})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    with pytest.raises(AssetTooLarge):
        await store.read_limited("inputs/source.png", max_bytes=5)

    assert blob.download_call_count == 1
    assert blob.last_download_kwargs == {
        "start": 0,
        "end": 5,
        "if_generation_match": 7,
    }


async def test_start_validates_object_list_access_without_bucket_metadata() -> None:
    store = GcsAssetStore(
        client=_Client({"magic-output": _Bucket({})}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    await store.start()


async def test_start_normalizes_output_bucket_access_failures() -> None:
    store = GcsAssetStore(
        client=_UnavailableClient(),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    with pytest.raises(
        RuntimeError,
        match="output bucket is unavailable: magic-output",
    ) as error:
        await store.start()

    assert isinstance(error.value.__cause__, PermissionError)


async def test_stop_leaves_an_injected_client_usable() -> None:
    output = _Bucket({"inputs/source.mp4": _Blob(b"source")})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )
    await store.start()

    assert await store.stop() is None
    assert await store.read("inputs/source.mp4") == b"source"


async def test_stop_closes_the_client_start_created(monkeypatch) -> None:
    created: list[_ClosableClient] = []

    class _Credentials:
        token = "ambient-token"

        def refresh(self, request: object) -> None:
            pass

    class _ClosableClient(_Client):
        def __init__(self, *, credentials: object) -> None:
            super().__init__({"magic-output": _Bucket({})})
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "magic_video_editor.plugins.gcs.google.auth.default",
        lambda scopes: (_Credentials(), "project"),
    )
    monkeypatch.setattr(
        "magic_video_editor.plugins.gcs.storage.Client", _ClosableClient
    )
    store = GcsAssetStore(output_bucket="magic-output")
    await store.start()

    await store.stop()
    await store.stop()

    assert [client.closed for client in created] == [True]
    await store.start()
    assert [client.closed for client in created] == [True, False]


async def test_publishes_output_once_with_a_generation_precondition() -> None:
    output = _Bucket({})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    first = await store.write_once("outputs/job.mp4", b"video", "video/mp4")
    second = await store.write_once("outputs/job.mp4", b"video", "video/mp4")

    assert first is True
    assert second is False
    assert output.blobs["outputs/job.mp4"].generation_match == 0
    assert output.blobs["outputs/job.mp4"].content_type == "video/mp4"
    assert output.blobs["outputs/job.mp4"].content == b"video"


async def test_creates_a_bounded_signed_upload_for_the_editor_bucket() -> None:
    output = _Bucket({})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
        signing_service_account_email="runtime@editor.invalid",
        signing_access_token="token",
    )

    url = await store.create_upload_url("sources/job.mp4", "video/mp4")

    assert url == "https://upload.invalid/signed"
    assert "sources/job.mp4" in output.blobs
    options = output.blobs["sources/job.mp4"].signed_url_options
    assert options is not None
    assert options["service_account_email"] == "runtime@editor.invalid"
    assert options["access_token"] == "token"
    assert options["query_parameters"] == {"ifGenerationMatch": "0"}
    # No ceiling: the grant keeps the shape existing clients send.
    assert options["headers"] is None

    await store.create_upload_url("sources/capped.png", "image/png", max_bytes=20_000_000)
    capped = output.blobs["sources/capped.png"].signed_url_options
    assert capped["headers"] == {"x-goog-content-length-range": "0,20000000"}


async def test_creates_a_create_once_signed_upload_for_a_pool_output() -> None:
    output = _Bucket({})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
        signing_service_account_email="runtime@editor.invalid",
        signing_access_token="token",
    )

    url = await store.create_output_upload_url("outputs/pool/echo.txt", "text/plain")

    assert url == "https://upload.invalid/signed"
    options = output.blobs["outputs/pool/echo.txt"].signed_url_options
    assert options is not None
    assert options["method"] == "PUT"
    assert options["content_type"] == "text/plain"
    assert options["expiration"] == timedelta(minutes=15)
    assert options["query_parameters"] == {"ifGenerationMatch": "0"}


async def test_signed_output_uploads_are_limited_to_the_output_namespace() -> None:
    store = GcsAssetStore(
        client=_Client({"magic-output": _Bucket({})}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
        signing_service_account_email="runtime@editor.invalid",
        signing_access_token="token",
    )

    with pytest.raises(ValueError, match="outputs/"):
        await store.create_output_upload_url("sources/pool/echo.bin", "text/plain")


async def test_studio_subject_upload_keys_are_gcs_source_compatible() -> None:
    output = _Bucket({})
    assets = GcsAssetStore(
        client=_Client({"magic-output": output}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
        signing_service_account_email="runtime@editor.invalid",
        signing_access_token="token",
    )
    service = StudioService(store=MemoryStudioStore(), assets=assets)
    project = await service.create_project("Ember Knight", "owner-1")

    target = await service.create_subject_upload(
        project.project_id,
        "image/png",
        actor_id="owner-1",
    )

    assert target.key.startswith(f"sources/studio/{project.project_id}/subjects/")
    assert target.upload_url == "https://upload.invalid/signed"


async def test_signed_uploads_are_limited_to_the_source_namespace() -> None:
    store = GcsAssetStore(
        client=_Client({"magic-output": _Bucket({})}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
        signing_service_account_email="runtime@editor.invalid",
        signing_access_token="token",
    )

    with pytest.raises(ValueError, match="sources/"):
        await store.create_upload_url("outputs/job.mp4", "video/mp4")


async def test_reads_an_inclusive_byte_range_for_streaming() -> None:
    output = _Bucket({"outputs/studio/project-1/motions/idle/a.mp4": _Blob(b"0123456789")})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}), output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    assert await store.read_range("outputs/studio/project-1/motions/idle/a.mp4", 2, 5) == b"2345"
    with pytest.raises(AssetNotFound):
        await store.read_range("outputs/studio/project-1/missing.mp4", 0, 1)
    with pytest.raises(ValueError, match="normalized relative path"):
        await store.read_range("outputs/movies/movie-1/../other/take.mp4", 0, 1)


@pytest.mark.parametrize(
    "key",
    ["gs://other-project-media/outputs/job.mp4", "../job.mp4", "/outputs/job.mp4"],
)
async def test_rejects_nonlocal_or_unnormalized_keys(key: str) -> None:
    store = GcsAssetStore(
        client=_Client({"magic-output": _Bucket({})}),
        output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
    )

    with pytest.raises(ValueError, match="normalized relative path"):
        await store.write_once(key, b"video", "video/mp4")


async def test_movie_source_upload_is_immutable_and_output_upload_is_rejected():
    output = _Bucket({})
    store = GcsAssetStore(
        client=_Client({"magic-output": output}), output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
        signing_service_account_email="runtime@editor.invalid",
        signing_access_token="token",
    )
    key = "uploads/movies/movie-1/reference"
    await store.create_upload_url(key, "image/png")
    options = output.blobs[key].signed_url_options
    assert options["method"] == "PUT"
    assert options["query_parameters"] == {"ifGenerationMatch": "0"}
    with pytest.raises(ValueError):
        await store.create_upload_url("outputs/movies/movie-1/take.mp4", "video/mp4")


async def test_movie_service_generated_upload_opening_and_cut_keys_can_be_read():
    from io import BytesIO

    from PIL import Image

    from magic_video_editor.movies.models import Clip, Shot, Take
    from magic_video_editor.movies.service import MovieService
    from magic_video_editor.movies.store import MemoryMovieStore

    output = _Bucket({})
    assets = GcsAssetStore(
        client=_Client({"magic-output": output}), output_bucket="magic-output",
        precondition_failed=_PreconditionFailed,
        signing_service_account_email="runtime@editor.invalid",
        signing_access_token="token",
    )
    class Media:
        async def assemble(self, sources, output_key, *, soundtracks):
            await assets.write_once(output_key, b"rendered MP4", "video/mp4")
    service = MovieService(MemoryMovieStore(), assets, media=Media())
    movie = await service.create("owner", "Film")
    grant = await service.upload(movie.movie_id, "owner", "image/png")
    assert grant["upload_url"] == "https://upload.invalid/signed"
    image = BytesIO()
    Image.new("RGB", (16, 16)).save(image, "PNG")
    output.blobs[grant["asset_key"]].content = image.getvalue()
    output.blobs[grant["asset_key"]].size = len(image.getvalue())
    movie = await service.get(movie.movie_id, "owner")
    movie.shots = [Shot(shot_id="one", name="One", prompt="Motion")]
    movie = await service.save(movie, movie.revision)
    movie = await service.opening(movie.movie_id, "owner", movie.revision, "one", grant["asset_key"])
    # Reads are owner-authorized and streamed; nothing mints a read URL.
    await service.authorize_asset(movie.movie_id, "owner", movie.shots[0].opening_asset_key)
    opening = output.blobs[movie.shots[0].opening_asset_key].content
    assert await assets.read_range(movie.shots[0].opening_asset_key, 0, 3) == opening[:4]
    movie.shots[0].takes = [Take(take_id="take", request_id="request", job_id="job", asset_key="outputs/movies/" + movie.movie_id + "/takes/clip.mp4", prompt="Motion", duration_seconds=2, status="succeeded")]
    movie.cut = [Clip(shot_id="one", take_id="take", in_seconds=0, out_seconds=2)]
    movie = await service.save(movie, movie.revision)
    movie = await service.assemble(movie.movie_id, "owner", movie.revision, "cut")
    assert movie.assembly.status == "succeeded"
    await service.authorize_asset(movie.movie_id, "owner", movie.assembly.asset_key)
    assert await assets.read_range(movie.assembly.asset_key, 0, 7) == b"rendered"
    movie = await service.release(movie.movie_id, "owner", movie.revision, "Final")
    release = movie.releases[-1]
    assert await service.release_file(movie.movie_id, "owner", release.release_id) == (
        release.asset_key, f"movie-{release.release_id}.mp4"
    )


async def test_deleting_a_project_prefix_removes_every_stored_version() -> None:
    from types import SimpleNamespace

    class VersionedBucket:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, int | None]] = []

        def delete_blob(self, blob_name: str, *, generation: int | None = None) -> None:
            self.deleted.append((blob_name, generation))

    class VersionedClient:
        def __init__(self) -> None:
            self.versioned = VersionedBucket()
            self.listed: dict[str, object] = {}

        def bucket(self, name: str) -> VersionedBucket:
            return self.versioned

        def list_blobs(self, bucket_name: str, **kwargs: object):
            self.listed = kwargs
            return [
                SimpleNamespace(name="outputs/studio/p1/a.png", generation=1),
                SimpleNamespace(name="outputs/studio/p1/a.png", generation=2),
                SimpleNamespace(name="outputs/studio/p1/b.mp4", generation=5),
            ]

    client = VersionedClient()
    store = GcsAssetStore(client=client, output_bucket="magic-output")

    assert await store.delete_prefix("outputs/studio/p1/") == 3
    assert client.listed == {"prefix": "outputs/studio/p1/", "versions": True}
    assert client.versioned.deleted == [
        ("outputs/studio/p1/a.png", 1),
        ("outputs/studio/p1/a.png", 2),
        ("outputs/studio/p1/b.mp4", 5),
    ]


@pytest.mark.parametrize("prefix", [
    "", "outputs/", "outputs/studio/", "outputs/studio", "outputs/studio/p1",
    "outputs/studio/../", "outputs/pool/p1/", "sources/studio/p1/x/",
])
async def test_deletion_is_confined_to_one_project_or_movie(prefix: str) -> None:
    store = GcsAssetStore(client=_Client({"magic-output": _Bucket({})}), output_bucket="magic-output")

    with pytest.raises(ValueError, match="refusing to delete"):
        await store.delete_prefix(prefix)

import asyncio
from collections.abc import Iterable
from datetime import timedelta
from pathlib import PurePosixPath
from typing import Protocol

import google.auth
from google.api_core.exceptions import PreconditionFailed
from google.auth.transport.requests import Request
from google.cloud import storage
from google.cloud.exceptions import NotFound

from magic_video_editor.contracts import (
    AssetDescriptor,
    AssetNotFound,
    AssetStorageUnavailable,
    AssetTooLarge,
    require_deletable_prefix,
    upload_size_headers,
)

# A signed upload URL is a bearer capability, so it lives only as long as a large
# artifact legitimately takes to PUT. Browsers never get a signed read: the API
# streams reads to their owner. The one signed read is a GPU worker's input grant,
# minted by the pool coordinator for a job it has just leased to that worker.
SIGNED_UPLOAD_URL_TTL = timedelta(minutes=15)
SIGNED_READ_URL_TTL = timedelta(minutes=5)
# Where pool job inputs live: studio subjects and references, pool API sources,
# and movie uploads, frames and plates.
POOL_INPUT_PREFIXES = (
    "sources/studio/",
    "outputs/studio/",
    "sources/pool/",
    "outputs/pool/",
    "uploads/movies/",
    "outputs/movies/",
)


class _Blob(Protocol):
    size: int | None
    generation: int | None
    content_type: str | None

    def reload(self) -> None: ...

    def download_as_bytes(self, **kwargs: object) -> bytes: ...

    def upload_from_string(
        self,
        content: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None: ...

    def generate_signed_url(self, **kwargs: object) -> str: ...


class _Bucket(Protocol):
    def blob(self, key: str) -> _Blob: ...

    def delete_blob(self, blob_name: str, *, generation: int | None = None) -> None: ...


class _StorageClient(Protocol):
    def bucket(self, name: str) -> _Bucket: ...

    def list_blobs(self, bucket_name: str, **kwargs: object) -> Iterable[_Blob]: ...


def _validate_object_key(key: str) -> str:
    normalized = str(PurePosixPath(key))
    if (
        not key
        or key.startswith(("/", "gs://"))
        or normalized != key
        or ".." in PurePosixPath(key).parts
    ):
        raise ValueError("GCS object key must be a normalized relative path")
    return key


class GcsAssetStore:
    def __init__(
        self,
        *,
        output_bucket: str,
        client: _StorageClient | None = None,
        precondition_failed: type[Exception] = PreconditionFailed,
        signing_service_account_email: str | None = None,
        signing_access_token: str | None = None,
    ) -> None:
        self._output_bucket = output_bucket
        self._client = client
        self._owns_client = client is None
        self._precondition_failed = precondition_failed
        self._signing_service_account_email = signing_service_account_email
        self._signing_access_token = signing_access_token
        self._ambient_credentials = None

    async def start(self) -> None:
        if self._client is None:
            credentials, _project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            await asyncio.to_thread(credentials.refresh, Request())
            self._ambient_credentials = credentials
            self._signing_access_token = credentials.token
            self._client = storage.Client(credentials=credentials)
        client = self._client
        try:
            await asyncio.to_thread(
                lambda: next(
                    iter(client.list_blobs(self._output_bucket, max_results=1)),
                    None,
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"output bucket is unavailable: {self._output_bucket}"
            ) from exc

    async def stop(self) -> None:
        # Close only a client this store created; an injected client is the caller's.
        if not self._owns_client or self._client is None:
            return
        client = self._client
        self._client = None
        self._ambient_credentials = None
        await asyncio.to_thread(client.close)

    async def read(self, key: str) -> bytes:
        object_key = _validate_object_key(key)
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            return await asyncio.to_thread(blob.download_as_bytes)
        except (FileNotFoundError, NotFound) as exc:
            raise AssetNotFound(object_key) from exc
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc

    async def read_limited(self, key: str, max_bytes: int) -> bytes:
        object_key = _validate_object_key(key)
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            await asyncio.to_thread(blob.reload)
            if blob.size is not None and int(blob.size) > max_bytes:
                raise AssetTooLarge(object_key, max_bytes, int(blob.size))
            download_kwargs: dict[str, object] = {"start": 0, "end": max_bytes}
            if blob.generation is not None:
                download_kwargs["if_generation_match"] = blob.generation
            content = await asyncio.to_thread(blob.download_as_bytes, **download_kwargs)
        except (FileNotFoundError, NotFound) as exc:
            raise AssetNotFound(object_key) from exc
        except AssetTooLarge:
            raise
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc
        if len(content) > max_bytes:
            raise AssetTooLarge(object_key, max_bytes, len(content))
        return content

    async def read_range(self, key: str, start: int, end: int) -> bytes:
        object_key = _validate_object_key(key)
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            return await asyncio.to_thread(blob.download_as_bytes, start=start, end=end)
        except (FileNotFoundError, NotFound) as exc:
            raise AssetNotFound(object_key) from exc
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc

    async def delete_prefix(self, prefix: str) -> int:
        require_deletable_prefix(prefix)
        client = self._require_client()
        bucket = client.bucket(self._output_bucket)

        def delete_all() -> int:
            # The bucket keeps noncurrent versions, so delete each generation;
            # deleting only the live object would leave the bytes behind.
            deleted = 0
            for blob in client.list_blobs(self._output_bucket, prefix=prefix, versions=True):
                try:
                    bucket.delete_blob(blob.name, generation=blob.generation)
                except NotFound:
                    continue
                deleted += 1
            return deleted

        try:
            return await asyncio.to_thread(delete_all)
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc

    async def create_upload_url(
        self, key: str, content_type: str, *, max_bytes: int | None = None
    ) -> str:
        object_key = _validate_object_key(key)
        if not object_key.startswith(("sources/", "uploads/movies/", "uploads/audio/")):
            raise ValueError("signed upload key must be in sources/")
        try:
            if self._ambient_credentials is not None:
                await asyncio.to_thread(self._ambient_credentials.refresh, Request())
                self._signing_access_token = self._ambient_credentials.token
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc
        if not self._signing_service_account_email or not self._signing_access_token:
            raise RuntimeError("IAM signing credentials are not configured")
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            return await asyncio.to_thread(
                blob.generate_signed_url,
                version="v4",
                expiration=SIGNED_UPLOAD_URL_TTL,
                method="PUT",
                content_type=content_type,
                service_account_email=self._signing_service_account_email,
                access_token=self._signing_access_token,
                query_parameters={"ifGenerationMatch": "0"},
                # Signed, so the client must send it and storage enforces it.
                headers=upload_size_headers(max_bytes) or None,
            )
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc

    async def create_read_url(self, key: str) -> str:
        """Short-lived GET grant for one pool job input (worker pool lease only)."""
        object_key = _validate_object_key(key)
        if not object_key.startswith(POOL_INPUT_PREFIXES):
            raise ValueError("signed read key must be a pool job input")
        try:
            if self._ambient_credentials is not None:
                await asyncio.to_thread(self._ambient_credentials.refresh, Request())
                self._signing_access_token = self._ambient_credentials.token
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc
        if not self._signing_service_account_email or not self._signing_access_token:
            raise RuntimeError("IAM signing credentials are not configured")
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            return await asyncio.to_thread(
                blob.generate_signed_url,
                version="v4",
                expiration=SIGNED_READ_URL_TTL,
                method="GET",
                service_account_email=self._signing_service_account_email,
                access_token=self._signing_access_token,
            )
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc

    async def create_output_upload_url(self, key: str, content_type: str) -> str:
        object_key = _validate_object_key(key)
        if not object_key.startswith("outputs/"):
            raise ValueError("signed output upload key must be in outputs/")
        try:
            if self._ambient_credentials is not None:
                await asyncio.to_thread(self._ambient_credentials.refresh, Request())
                self._signing_access_token = self._ambient_credentials.token
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc
        if not self._signing_service_account_email or not self._signing_access_token:
            raise RuntimeError("IAM signing credentials are not configured")
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            return await asyncio.to_thread(
                blob.generate_signed_url,
                version="v4",
                expiration=SIGNED_UPLOAD_URL_TTL,
                method="PUT",
                content_type=content_type,
                service_account_email=self._signing_service_account_email,
                access_token=self._signing_access_token,
                query_parameters={"ifGenerationMatch": "0"},
            )
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc

    async def describe(self, key: str) -> AssetDescriptor | None:
        object_key = _validate_object_key(key)
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            await asyncio.to_thread(blob.reload)
        except (FileNotFoundError, NotFound):
            return None
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc
        return AssetDescriptor(
            key=object_key,
            size=int(blob.size or 0),
            content_type=blob.content_type,
        )

    async def write_once(
        self, key: str, content: bytes, content_type: str
    ) -> bool:
        object_key = _validate_object_key(key)
        blob = self._require_client().bucket(self._output_bucket).blob(object_key)
        try:
            await asyncio.to_thread(
                blob.upload_from_string,
                content,
                content_type=content_type,
                if_generation_match=0,
            )
        except self._precondition_failed:
            return False
        except Exception as exc:
            raise AssetStorageUnavailable("asset storage unavailable") from exc
        return True

    def _require_client(self) -> _StorageClient:
        if self._client is None:
            raise RuntimeError("GCS asset store has not started")
        return self._client

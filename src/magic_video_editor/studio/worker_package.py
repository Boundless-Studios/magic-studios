"""Deliver the image's pinned worker wheel only to a valid pairing-code holder,
or to an enrolled machine updating itself with its own live credential."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from magic_video_editor.studio.enrolment import (
    StudioEnrolmentStore,
    hash_token,
    looks_like_pairing_token,
    looks_like_worker_credential,
)

PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
WHEEL_NAME = re.compile(r"outbound_gpu_worker_pool-[A-Za-z0-9_.+]+-py3-none-any\.whl")


class PackageDownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(max_length=100)


class PackageUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: str = Field(max_length=200)


def _package_directory():
    configured = os.getenv("MAGIC_VIDEO_EDITOR_WORKER_PACKAGE_DIR")
    return Path(configured) if configured else files("magic_video_editor").joinpath("worker_packages")


def _read_manifest() -> dict[str, str]:
    manifest = json.loads(_package_directory().joinpath("manifest.json").read_text())
    filename, digest = manifest["filename"], manifest["sha256"]
    if not isinstance(filename, str) or not WHEEL_NAME.fullmatch(filename):
        raise ValueError("invalid worker wheel name")
    # Builds made before the commit was recorded still identify by digest.
    return {"filename": filename, "sha256": str(digest), "commit": str(manifest.get("commit") or "")}


def _read_package() -> tuple[dict[str, str], bytes]:
    manifest = _read_manifest()
    data = _package_directory().joinpath(manifest["filename"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
        raise ValueError("worker package checksum mismatch")
    return manifest, data


def _package_response(manifest: dict[str, str], data: bytes) -> Response:
    return Response(data, media_type="application/octet-stream", headers={
        **PRIVATE_HEADERS,
        "Content-Disposition": 'attachment; filename="' + manifest["filename"] + '"',
        "X-Worker-Package-Filename": manifest["filename"],
        "X-Worker-Package-SHA256": manifest["sha256"],
        "X-Worker-Package-Commit": manifest["commit"],
    })


def create_worker_package_router(store: StudioEnrolmentStore | None) -> APIRouter:
    router = APIRouter()

    @router.post("/workers/package")
    async def download_package(request: PackageDownloadRequest) -> Response:
        if store is None:
            raise HTTPException(503, "enrolment is not configured", headers=PRIVATE_HEADERS)
        if not looks_like_pairing_token(request.token) or not await store.usable_pairing_token(
            token_hash=hash_token(request.token), now=datetime.now(UTC)
        ):
            raise HTTPException(400, "pairing token is not usable", headers=PRIVATE_HEADERS)
        try:
            manifest, data = await run_in_threadpool(_read_package)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(
                503, "worker package is unavailable; the deployment must include a built worker wheel",
                headers=PRIVATE_HEADERS,
            ) from exc
        return _package_response(manifest, data)

    async def require_enrolled(credential: str) -> None:
        """A live, unrevoked machine credential; the same refusal for every failure."""
        if store is None:
            raise HTTPException(503, "enrolment is not configured", headers=PRIVATE_HEADERS)
        worker = (await store.worker_by_credential(hash_token(credential))
                  if looks_like_worker_credential(credential) else None)
        if worker is None or worker.revoked:
            raise HTTPException(400, "credential is not usable", headers=PRIVATE_HEADERS)

    @router.post("/workers/package/manifest")
    async def package_manifest(request: PackageUpdateRequest) -> Response:
        """What an update would install, so an up-to-date machine downloads nothing."""
        await require_enrolled(request.credential)
        try:
            manifest = await run_in_threadpool(_read_manifest)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(503, "worker package is unavailable", headers=PRIVATE_HEADERS) from exc
        return Response(json.dumps(manifest), media_type="application/json", headers=PRIVATE_HEADERS)

    @router.post("/workers/package/update")
    async def update_package(request: PackageUpdateRequest) -> Response:
        """The pinned wheel for an enrolled machine's credential-preserving update."""
        await require_enrolled(request.credential)
        try:
            manifest, data = await run_in_threadpool(_read_package)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(503, "worker package is unavailable", headers=PRIVATE_HEADERS) from exc
        return _package_response(manifest, data)

    return router

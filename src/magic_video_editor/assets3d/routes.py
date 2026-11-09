"""Owner-authenticated 3D asset API. Asset3DError uses the normal FastAPI detail field."""

from collections.abc import Awaitable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field

from magic_video_editor.asset_delivery import asset_response
from magic_video_editor.assets3d.models import Kind, Model, Role, Stage
from magic_video_editor.assets3d.service import Asset3DError, Assets3DService
from magic_video_editor.contracts import AssetNotFound, AssetStorageUnavailable, AssetTooLarge
from magic_video_editor.studio.api_keys import parse_bearer_token
from magic_video_editor.studio.routes import studio_actor_id


class Create(Model):
    name: str = Field(min_length=1, max_length=200)
    kind: Kind = "prop"


class Revision(Model):
    expected_revision: int = Field(ge=1)


class Upload(Model):
    content_type: str
    # The file's size, when known: the grant then reserves and allows only that.
    byte_length: int | None = Field(default=None, ge=1)


class AddSource(Revision):
    role: Role
    asset_key: str = Field(min_length=1)
    file_name: str = Field(default="", max_length=200)


class AddMesh(Revision):
    asset_key: str = Field(min_length=1)
    file_name: str = Field(default="", max_length=200)


class ReviewBody(Revision):
    outcome: Literal["keep", "reject"]
    note: str = Field(default="", max_length=500)
    # The file the verdict is about; refused if it is not the candidate's file.
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Selections(Revision):
    # A stage's candidate id, or null to clear that stage's selection.
    selections: dict[Stage, str | None] = Field(min_length=1)


class Publish(Revision):
    name: str = Field(min_length=1, max_length=200)


class StageRequest(Model):
    # Repeating a request_id returns the same run; it never starts a second one.
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    parent_id: str | None = Field(default=None, max_length=80)
    settings: dict[str, int | str] = Field(default_factory=dict, max_length=8)


class ConfirmDeletion(Model):
    confirm_name: str = Field(min_length=1, max_length=500)


async def call[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except Asset3DError as e:
        if e.current_revision is not None:
            raise HTTPException(
                e.status, {"message": str(e), "current_revision": e.current_revision}
            ) from e
        if e.code is not None:
            raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from e
        raise HTTPException(e.status, str(e)) from e
    except AssetNotFound as e:
        raise HTTPException(404, "3D asset file not found") from e
    except AssetTooLarge as e:
        raise HTTPException(422, f"The file is larger than {e.max_bytes // (1024 * 1024)} MB") from e
    except AssetStorageUnavailable as e:
        raise HTTPException(503, "3D asset storage is temporarily unavailable") from e


def actor_kind(request: Request) -> Literal["session", "api_key"]:
    # `studio_actor_id` already resolved the caller; a bearer token means an API key.
    return "api_key" if parse_bearer_token(request.headers.get("authorization")) else "session"


def create_assets3d_router(service: Assets3DService) -> APIRouter:
    router = APIRouter(prefix="/studio/3d", tags=["3d"])

    @router.get("/capabilities")
    async def capabilities(owner: str = Depends(studio_actor_id)):
        return await service.capabilities(owner)

    @router.post("")
    async def create(body: Create, owner: str = Depends(studio_actor_id)):
        return await call(service.create(owner, body.name, body.kind))

    @router.get("")
    async def listing(owner: str = Depends(studio_actor_id)):
        return await service.list(owner)

    @router.get("/{id}")
    async def get(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.get(id, owner))

    @router.delete("/{id}", status_code=204)
    async def delete(id: str, body: ConfirmDeletion, owner: str = Depends(studio_actor_id)):
        """Permanently delete the 3D asset with every source, candidate, release and file."""
        await call(service.delete_asset(id, owner, body.confirm_name))
        return Response(status_code=204)

    @router.post("/{id}/uploads")
    async def upload(id: str, body: Upload, owner: str = Depends(studio_actor_id)):
        return await call(service.upload(id, owner, body.content_type, body.byte_length))

    @router.post("/{id}/sources")
    async def add_source(id: str, body: AddSource, request: Request, owner: str = Depends(studio_actor_id)):
        return await call(
            service.add_source(
                id, owner, body.expected_revision, body.role, body.asset_key, body.file_name,
                actor_kind(request),
            )
        )

    @router.post("/{id}/meshes")
    async def add_mesh(id: str, body: AddMesh, request: Request, owner: str = Depends(studio_actor_id)):
        return await call(
            service.add_mesh(
                id, owner, body.expected_revision, body.asset_key, body.file_name, actor_kind(request)
            )
        )

    @router.post("/{id}/candidates/{candidate_id}/review")
    async def review(
        id: str, candidate_id: str, body: ReviewBody, request: Request,
        owner: str = Depends(studio_actor_id),
    ):
        return await call(
            service.review(
                id, owner, body.expected_revision, candidate_id, body.outcome, body.note,
                actor_kind(request), body.sha256,
            )
        )

    @router.put("/{id}/selections")
    async def selections(id: str, body: Selections, owner: str = Depends(studio_actor_id)):
        return await call(service.select(id, owner, body.expected_revision, body.selections))

    @router.post("/{id}/stages/{stage}")
    async def run_stage(
        id: str, stage: Literal["mesh", "clean"], body: StageRequest,
        owner: str = Depends(studio_actor_id),
    ):
        return await call(
            service.submit_stage(id, owner, stage, body.request_id, body.parent_id, dict(body.settings))
        )

    @router.post("/{id}/reconcile")
    async def reconcile(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.reconcile(id, owner))

    @router.get("/{id}/readiness")
    async def readiness(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.get_readiness(id, owner))

    @router.post("/{id}/release")
    async def release(id: str, body: Publish, owner: str = Depends(studio_actor_id)):
        return await call(service.release(id, owner, body.expected_revision, body.name))

    # Files stream through the API after the owner check on every request.
    @router.get("/{id}/releases/{release_id}/download")
    async def release_download(
        id: str, release_id: str, request: Request, owner: str = Depends(studio_actor_id)
    ):
        key, filename = await call(service.release_file(id, owner, release_id))
        return await call(asset_response(service.assets, key, request, filename=filename))

    @router.get("/{id}/assets/{key:path}")
    async def asset(id: str, key: str, request: Request, owner: str = Depends(studio_actor_id)):
        await call(service.authorize_asset(id, owner, key))
        return await call(asset_response(service.assets, key, request))

    return router

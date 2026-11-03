"""Owner-authenticated Audio API. AudioError uses the normal FastAPI detail field."""

from collections.abc import Awaitable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import Field

from magic_video_editor.asset_delivery import asset_response
from magic_video_editor.audio.models import CueInput, LicencePolicy, Model
from magic_video_editor.audio.service import AudioError, AudioService
from magic_video_editor.contracts import AssetNotFound, AssetStorageUnavailable, AssetTooLarge
from magic_video_editor.studio.api_keys import parse_bearer_token
from magic_video_editor.studio.routes import studio_actor_id


class Create(Model):
    name: str = Field(min_length=1, max_length=200)


class Revision(Model):
    expected_revision: int = Field(ge=1)


class Update(Revision):
    name: str = Field(min_length=1, max_length=200)
    brief: str = Field(default="", max_length=4000)
    licence_policy: LicencePolicy = "cc0"
    cues: list[CueInput] = Field(max_length=64)


class Import(Revision):
    source: Literal["freesound"] = "freesound"
    source_id: str = Field(min_length=1, max_length=12, pattern=r"^[0-9]+$")


class Upload(Model):
    content_type: str


class AddUpload(Revision):
    asset_key: str = Field(min_length=1)
    filename: str = Field(default="", max_length=200)


class Original(Revision):
    asset_key: str = Field(min_length=1)


class ReviewBody(Revision):
    outcome: Literal["keep", "reject"]
    note: str = Field(default="", max_length=500)
    reason: str = Field(default="", max_length=80)
    # The file the verdict is about; refused if the candidate's file has changed.
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class Selection(Revision):
    candidate_ids: list[str] = Field(max_length=16)


class Publish(Revision):
    name: str = Field(min_length=1, max_length=200)


class SendToMovie(Model):
    movie_id: str = Field(min_length=1)
    movie_revision: int = Field(ge=1)


class ConfirmDeletion(Model):
    confirm_name: str = Field(min_length=1, max_length=500)


async def call[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except AudioError as e:
        if e.current_revision is not None:
            raise HTTPException(
                e.status, {"message": str(e), "current_revision": e.current_revision}
            ) from e
        if e.code is not None:
            raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from e
        raise HTTPException(e.status, str(e)) from e
    except AssetNotFound as e:
        raise HTTPException(404, "Audio asset not found") from e
    except AssetTooLarge as e:
        raise HTTPException(422, "The audio file is larger than 50 MB") from e
    except AssetStorageUnavailable as e:
        raise HTTPException(503, "Audio storage is temporarily unavailable") from e


def actor_kind(request: Request) -> Literal["session", "api_key"]:
    # `studio_actor_id` already resolved the caller; a bearer token means an API key.
    return "api_key" if parse_bearer_token(request.headers.get("authorization")) else "session"


def create_audio_router(service: AudioService) -> APIRouter:
    router = APIRouter(prefix="/studio/audio", tags=["audio"])

    @router.get("/capabilities")
    async def capabilities(owner: str = Depends(studio_actor_id)):
        return await service.capabilities(owner)

    @router.post("")
    async def create(body: Create, owner: str = Depends(studio_actor_id)):
        return await call(service.create(owner, body.name))

    @router.get("")
    async def listing(owner: str = Depends(studio_actor_id)):
        return await service.list(owner)

    @router.get("/{id}")
    async def get(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.get(id, owner))

    @router.put("/{id}")
    async def update(id: str, body: Update, owner: str = Depends(studio_actor_id)):
        return await call(
            service.update(
                id, owner, body.expected_revision, name=body.name, brief=body.brief,
                licence_policy=body.licence_policy, cues=body.cues,
            )
        )

    @router.delete("/{id}", status_code=204)
    async def delete(id: str, body: ConfirmDeletion, owner: str = Depends(studio_actor_id)):
        """Permanently delete the audio set with every candidate, release and file."""
        await call(service.delete_set(id, owner, body.confirm_name))
        return Response(status_code=204)

    @router.get("/{id}/free-sounds")
    async def free_sounds(
        id: str,
        q: str = Query(min_length=1, max_length=200),
        min_seconds: float = Query(default=0, ge=0, le=600),
        max_seconds: float = Query(default=120, gt=0, le=600),
        page: int = Query(default=1, ge=1, le=10),
        owner: str = Depends(studio_actor_id),
    ):
        return await call(service.search(id, owner, q, min_seconds, max_seconds, page))

    @router.post("/{id}/cues/{cue_id}/import")
    async def import_sound(id: str, cue_id: str, body: Import, owner: str = Depends(studio_actor_id)):
        return await call(
            service.import_free_sound(id, owner, body.expected_revision, cue_id, body.source_id)
        )

    @router.post("/{id}/uploads")
    async def upload(id: str, body: Upload, owner: str = Depends(studio_actor_id)):
        return await call(service.upload(id, owner, body.content_type))

    @router.post("/{id}/cues/{cue_id}/upload")
    async def add_upload(id: str, cue_id: str, body: AddUpload, owner: str = Depends(studio_actor_id)):
        return await call(
            service.add_upload(id, owner, body.expected_revision, cue_id, body.asset_key, body.filename)
        )

    @router.post("/{id}/candidates/{candidate_id}/original")
    async def original(
        id: str, candidate_id: str, body: Original, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.replace_original(id, owner, body.expected_revision, candidate_id, body.asset_key)
        )

    @router.post("/{id}/candidates/{candidate_id}/review")
    async def review(
        id: str, candidate_id: str, body: ReviewBody, request: Request,
        owner: str = Depends(studio_actor_id),
    ):
        return await call(
            service.review(
                id, owner, body.expected_revision, candidate_id, body.outcome,
                body.note, body.reason, actor_kind(request), body.sha256,
            )
        )

    @router.put("/{id}/cues/{cue_id}/selection")
    async def selection(id: str, cue_id: str, body: Selection, owner: str = Depends(studio_actor_id)):
        return await call(
            service.select(id, owner, body.expected_revision, cue_id, body.candidate_ids)
        )

    @router.get("/{id}/readiness")
    async def readiness(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.get_readiness(id, owner))

    @router.post("/{id}/release")
    async def release(id: str, body: Publish, owner: str = Depends(studio_actor_id)):
        return await call(service.release(id, owner, body.expected_revision, body.name))

    @router.post("/{id}/candidates/{candidate_id}/send-to-movie")
    async def send_to_movie(
        id: str, candidate_id: str, body: SendToMovie, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.send_to_movie(id, owner, candidate_id, body.movie_id, body.movie_revision)
        )

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

"""Owner-authenticated scene-flight API: frame-pinned camera legs between images."""

import base64
import binascii
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field

from magic_video_editor.asset_delivery import asset_response
from magic_video_editor.flights.service import (
    IMAGE_TYPES,
    LegInput,
    SceneFlightService,
    recipe,
)
from magic_video_editor.movies.models import Model
from magic_video_editor.movies.routes import ConfirmDeletion, call
from magic_video_editor.studio.routes import studio_actor_id


class Create(Model):
    name: str = Field(min_length=1, max_length=200)


class Revision(Model):
    expected_revision: int = Field(ge=1)


class ImageUpload(Model):
    content_type: str = "image/png"
    # 20 MiB decoded.
    content_base64: str = Field(min_length=1, max_length=27_962_028)


class Legs(Revision):
    legs: list[LegInput] = Field(max_length=24)


class Generate(Revision):
    request_id: str = Field(min_length=1, max_length=120)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)


class Select(Revision):
    take_id: str


class Chain(Revision):
    request_id: str = Field(min_length=1, max_length=120)


def create_scene_flight_router(service: SceneFlightService) -> APIRouter:
    router = APIRouter(prefix="/studio/scene-flights", tags=["scene-flights"])

    @router.get("/capabilities")
    async def capabilities(owner: str = Depends(studio_actor_id)):
        return {
            "generation_available": service.generation_available,
            "image_types": sorted(IMAGE_TYPES),
            **recipe(),
        }

    @router.post("")
    async def create(body: Create, owner: str = Depends(studio_actor_id)):
        return await call(service.create(owner, body.name))

    @router.get("")
    async def listing(owner: str = Depends(studio_actor_id)):
        return await service.list(owner)

    @router.get("/{id}")
    async def get(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.get(id, owner))

    @router.delete("/{id}", status_code=204)
    async def delete(id: str, body: ConfirmDeletion, owner: str = Depends(studio_actor_id)):
        """Permanently delete the scene flight with every leg, take and image."""
        await call(service.flight(id, owner))
        await call(service.movies.delete_movie(id, owner, body.confirm_name))
        return Response(status_code=204)

    @router.post("/{id}/images")
    async def image(id: str, body: ImageUpload, owner: str = Depends(studio_actor_id)):
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, "invalid base64 image") from exc
        return await call(service.add_image(id, owner, content, body.content_type))

    @router.put("/{id}/legs")
    async def legs(id: str, body: Legs, owner: str = Depends(studio_actor_id)):
        return await call(service.set_legs(id, owner, body.expected_revision, body.legs))

    @router.post("/{id}/legs/{leg_id}/takes")
    async def generate(id: str, leg_id: str, body: Generate, owner: str = Depends(studio_actor_id)):
        return await call(
            service.generate(id, owner, body.expected_revision, leg_id, body.request_id, body.seed)
        )

    @router.post("/{id}/legs/{leg_id}/select")
    async def select(id: str, leg_id: str, body: Select, owner: str = Depends(studio_actor_id)):
        return await call(service.select(id, owner, body.expected_revision, leg_id, body.take_id))

    @router.post("/{id}/legs/{leg_id}/takes/{take_id}/cancel")
    async def cancel(
        id: str, leg_id: str, take_id: str, body: Revision, owner: str = Depends(studio_actor_id)
    ):
        return await call(service.cancel(id, owner, body.expected_revision, leg_id, take_id))

    @router.get("/{id}/legs/{leg_id}/takes/{take_id}/video")
    async def take_video(
        id: str, leg_id: str, take_id: str, request: Request, download: bool = False,
        owner: str = Depends(studio_actor_id),
    ):
        key = await call(service.take_video_key(id, owner, leg_id, take_id))
        filename = f"flight-{UUID(id)}-{leg_id}-{take_id[:8]}.mp4" if download else None
        return await call(asset_response(service.movies.assets, key, request, filename=filename))

    @router.post("/{id}/chain")
    async def chain(id: str, body: Chain, owner: str = Depends(studio_actor_id)):
        return await call(service.chain(id, owner, body.expected_revision, body.request_id))

    @router.get("/{id}/chain/video")
    async def chain_video(
        id: str, request: Request, download: bool = False, owner: str = Depends(studio_actor_id)
    ):
        key = await call(service.chain_video_key(id, owner))
        filename = f"flight-{UUID(id)}-chain.mp4" if download else None
        return await call(asset_response(service.movies.assets, key, request, filename=filename))

    @router.get("/{id}/assets/{key:path}")
    async def asset(id: str, key: str, request: Request, owner: str = Depends(studio_actor_id)):
        """Images (origin, destination, frozen pins, last frames), streamed to the owner."""
        await call(service.flight(id, owner))
        await call(service.movies.authorize_asset(id, owner, key))
        return await call(asset_response(service.movies.assets, key, request))

    return router

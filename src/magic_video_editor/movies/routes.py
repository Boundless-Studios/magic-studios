"""Owner-authenticated Movies API. MovieError uses the normal FastAPI detail field."""

from collections.abc import Awaitable
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field

from magic_video_editor.asset_delivery import asset_response
from magic_video_editor.contracts import (
    AssetNotFound,
    AssetStorageUnavailable,
    AssetTooLarge,
)
from magic_video_editor.movies import dialogue
from magic_video_editor.movies.build import selected_cut
from magic_video_editor.movies.models import CharacterInput, Clip, Model, ShotInput
from magic_video_editor.movies.planner import PlanError
from magic_video_editor.movies.service import MovieError, MovieService
from magic_video_editor.movies.soundtracks import AudioTrack, save_soundtracks
from magic_video_editor.studio.accounts import AccountPolicy, director_unavailable
from magic_video_editor.studio.routes import studio_actor_id


class Create(Model):
    name: str = Field(min_length=1, max_length=200)


class Revision(Model):
    expected_revision: int = Field(ge=1)


class Story(Revision):
    brief: str = Field(default="", max_length=16000)
    style: str = Field(default="", max_length=2000)
    shots: list[ShotInput] = Field(max_length=60)
    batch_size: int = Field(default=3, ge=1, le=6)
    compute_target: Literal["shared", "my_machines"] = "shared"


class Generate(Revision):
    select_when_ready: bool = False
    shot_ids: list[str] = Field(min_length=1, max_length=6)
    request_id: str = Field(min_length=1, max_length=120)


class Select(Revision):
    take_id: str


class Opening(Revision):
    asset_key: str


class VoiceReference(Revision):
    asset_key: str = Field(min_length=1)


class Upload(Model):
    content_type: str


class Compose(Revision):
    request_id: str = Field(min_length=1, max_length=120)
    reference_asset_keys: list[str] = Field(default_factory=list, max_length=4)


class Cut(Revision):
    clips: list[Clip] = Field(min_length=1, max_length=120)


class Assemble(Revision):
    request_id: str = Field(min_length=1, max_length=120)


class ConfirmDeletion(Model):
    confirm_name: str = Field(min_length=1, max_length=500)


class Publish(Revision):
    name: str = Field(min_length=1, max_length=200)


class Plan(Revision):
    brief: str = Field(min_length=1, max_length=16000)
    style: str = Field(default="", max_length=2000)
    shot_count: int = Field(default=4, ge=1, le=40)
    target_seconds: int | None = Field(default=None, ge=10, le=600)


class Soundtracks(Revision):
    tracks: list[AudioTrack] = Field(max_length=8)


class Characters(Revision):
    characters: list[CharacterInput] = Field(max_length=8)


async def call[T](awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except MovieError as e:
        if e.current_revision is not None:
            raise HTTPException(
                e.status, {"message": str(e), "current_revision": e.current_revision}
            ) from e
        if e.code is not None:
            raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from e
        raise HTTPException(e.status, str(e)) from e
    except AssetNotFound as e:
        raise HTTPException(404, "Movie asset not found") from e
    except AssetTooLarge as e:
        raise HTTPException(422, "Movie asset exceeds the supported size") from e
    except AssetStorageUnavailable as e:
        raise HTTPException(
            503, "Movie asset storage is temporarily unavailable"
        ) from e


def create_movie_router(service: MovieService) -> APIRouter:
    async def movies_only(request: Request, owner: str = Depends(studio_actor_id)) -> None:
        # A scene flight is stored as a movie, and the service methods are shared
        # with /studio/scene-flights; so the movie routes refuse one here, as not found.
        id = request.path_params.get("id")
        if id is None:
            return
        movie = await service.store.get(id)
        if movie is not None and movie.owner_id == owner and movie.kind != "movie":
            raise HTTPException(404, "Movie not found")

    router = APIRouter(
        prefix="/studio/movies", tags=["movies"], dependencies=[Depends(movies_only)]
    )
    policy: AccountPolicy = getattr(service, "account_policy", None) or AccountPolicy()

    @router.get("/capabilities")
    async def capabilities(owner: str = Depends(studio_actor_id)):
        public = await policy.is_public(owner)
        # Takes and auditions are MiniMax H3; planning uses house services. A
        # public account runs only on its own machine and never on the shared
        # HTTP composition provider, so offer only what it can actually start.
        h3 = await policy.h3_allowed(owner)
        tenant_for = getattr(service, "tenant_for", None)
        own_machine = (not public) or (tenant_for is not None and await tenant_for(owner) is not None)
        return {
            "account_tier": (await policy.tier(owner)).value,
            "generation_available": service.pool is not None and h3 and own_machine,
            "composition_available": (
                service.composition is not None or service.pool is not None
            ) if not public else (
                service.composition is None and service.pool is not None and own_machine
            ),
            "planner_available": not public
            and (
                getattr(service, "planner", None) is not None
                or getattr(service, "director", None) is not None
            ),
            "director_available": not public
            and getattr(service, "director", None) is not None,
            "min_duration_seconds": 1.5,
            "duration_step_seconds": 0.5,
            "max_duration_seconds": dialogue.MAX_SHOT_SECONDS,
            "max_batch_size": service.max_batch_size,
            # Dialogue needs a voice reference per speaker, and voice upload and
            # auditions process audio on the service, which public accounts never use.
            "dialogue_available": service.pool is not None and h3 and own_machine and not public,
            "max_dialogue_seconds": dialogue.MAX_FRAMES / dialogue.FPS,
            "voice_min_seconds": dialogue.VOICE_MIN_SECONDS,
            "voice_max_seconds": dialogue.VOICE_MAX_SECONDS,
            "max_speakers_per_shot": dialogue.MAX_SPEAKERS,
        }

    @router.post("")
    async def create(body: Create, owner: str = Depends(studio_actor_id)):
        return await call(service.create(owner, body.name))

    @router.get("")
    async def listing(owner: str = Depends(studio_actor_id)):
        return [
            {**movie.model_dump(mode="json"), "expires_at": expires_at}
            for movie, expires_at in await service.list_with_expiry(owner, "movie")
        ]

    @router.get("/{id}")
    async def get(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.get(id, owner))

    @router.delete("/{id}", status_code=204)
    async def delete(id: str, body: ConfirmDeletion, owner: str = Depends(studio_actor_id)):
        """Permanently delete the movie with every take, cut, release and file."""
        await call(service.delete_movie(id, owner, body.confirm_name))
        return Response(status_code=204)

    @router.get("/{id}/selected-cut")
    async def selected(id: str, owner: str = Depends(studio_actor_id)):
        # Read-only: the cut Generate movie saves, built from the selected takes.
        return selected_cut(await call(service.get(id, owner)))

    @router.get("/{id}/history")
    async def history(id: str, owner: str = Depends(studio_actor_id)):
        await call(service.get(id, owner))
        return await service.store.history(id)

    @router.put("/{id}/story")
    async def story(id: str, body: Story, owner: str = Depends(studio_actor_id)):
        return await call(
            service.story(
                id,
                owner,
                body.expected_revision,
                brief=body.brief,
                style=body.style,
                shots=body.shots,
                batch_size=body.batch_size,
                compute_target=body.compute_target,
            )
        )

    @router.put("/{id}/characters")
    async def characters(
        id: str, body: Characters, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.characters(id, owner, body.expected_revision, body.characters)
        )

    @router.put("/{id}/characters/{cid}/voice")
    async def attach_voice(
        id: str, cid: str, body: VoiceReference, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.attach_voice(id, owner, body.expected_revision, cid, body.asset_key)
        )

    @router.post("/{id}/characters/{cid}/portrait")
    async def portrait(
        id: str, cid: str, body: Assemble, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.portrait(id, owner, body.expected_revision, cid, body.request_id)
        )

    @router.post("/{id}/characters/{cid}/audition")
    async def audition(
        id: str, cid: str, body: Assemble, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.audition(id, owner, body.expected_revision, cid, body.request_id)
        )

    @router.post("/{id}/storyboard")
    async def storyboard(id: str, body: Assemble, owner: str = Depends(studio_actor_id)):
        return await call(
            service.storyboard(id, owner, body.expected_revision, body.request_id)
        )

    @router.post("/{id}/plan")
    async def plan(id: str, body: Plan, owner: str = Depends(studio_actor_id)):
        await call(service.current(id, owner, body.expected_revision))
        if await policy.is_public(owner):
            # The director and planner run on house keys and endpoints.
            refusal = director_unavailable()
            raise HTTPException(403, {"code": refusal.code, "message": refusal.message})
        director = getattr(service, "director", None)
        planner = getattr(service, "planner", None)
        if director is None and planner is None:
            raise HTTPException(
                503,
                "Shot planning is not configured. You can write the shots manually.",
            )
        # The brief goes to an outside model, so it is screened first.
        await service.prompt_screening.require_allowed(owner, "plan", [body.brief, body.style])
        try:
            if director is not None:
                breakdown = await director.plan(
                    brief=body.brief,
                    style=body.style,
                    shot_count=body.shot_count,
                    target_seconds=body.target_seconds,
                )
                return {
                    "style": breakdown.style,
                    "characters": breakdown.characters,
                    "shots": breakdown.shots,
                }
            return {
                "shots": await planner.plan(
                    brief=body.brief, style=body.style, shot_count=body.shot_count
                )
            }
        except PlanError as e:
            raise HTTPException(502, str(e)) from e

    @router.post("/{id}/build")
    async def build(id: str, body: Assemble, owner: str = Depends(studio_actor_id)):
        return await call(service.build_movie(id, owner, body.expected_revision, body.request_id))

    @router.post("/{id}/generate")
    async def generate(id: str, body: Generate, owner: str = Depends(studio_actor_id)):
        return await call(
            service.generate(
                id, owner, body.expected_revision, body.shot_ids, body.request_id,
                select_when_ready=body.select_when_ready
            )
        )

    @router.post("/{id}/reconcile")
    async def reconcile(id: str, owner: str = Depends(studio_actor_id)):
        return await call(service.reconcile(id, owner))

    @router.post("/{id}/uploads")
    async def upload(id: str, body: Upload, owner: str = Depends(studio_actor_id)):
        return await call(service.upload(id, owner, body.content_type))

    @router.post("/{id}/shots/{sid}/opening")
    async def opening(
        id: str, sid: str, body: Opening, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.opening(id, owner, body.expected_revision, sid, body.asset_key)
        )

    @router.post("/{id}/shots/{sid}/import")
    async def import_clip(
        id: str, sid: str, body: Opening, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.import_clip(id, owner, body.expected_revision, sid, body.asset_key)
        )

    @router.post("/{id}/shots/{sid}/approve")
    async def approve(
        id: str, sid: str, body: Revision, owner: str = Depends(studio_actor_id)
    ):
        return await call(service.approve(id, owner, body.expected_revision, sid))

    @router.post("/{id}/shots/{sid}/compose")
    async def compose(
        id: str, sid: str, body: Compose, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.compose(
                id,
                owner,
                body.expected_revision,
                sid,
                body.request_id,
                body.reference_asset_keys,
            )
        )

    @router.post("/{id}/shots/{sid}/takes/{tid}/accept-continuity")
    async def accept_take(
        id: str,
        sid: str,
        tid: str,
        body: Revision,
        owner: str = Depends(studio_actor_id),
    ):
        return await call(
            service.accept_take(id, owner, body.expected_revision, sid, tid)
        )

    @router.post("/{id}/shots/{sid}/select")
    async def select(
        id: str, sid: str, body: Select, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            service.select(id, owner, body.expected_revision, sid, body.take_id)
        )

    @router.post("/{id}/shots/{sid}/takes/{tid}/cancel")
    async def cancel(
        id: str,
        sid: str,
        tid: str,
        body: Revision,
        owner: str = Depends(studio_actor_id),
    ):
        return await call(service.cancel(id, owner, body.expected_revision, sid, tid))

    @router.put("/{id}/cut")
    async def cut(id: str, body: Cut, owner: str = Depends(studio_actor_id)):
        return await call(service.cut(id, owner, body.expected_revision, body.clips))

    @router.put("/{id}/soundtracks")
    async def soundtracks(
        id: str, body: Soundtracks, owner: str = Depends(studio_actor_id)
    ):
        return await call(
            save_soundtracks(service, id, owner, body.expected_revision, body.tracks)
        )

    @router.post("/{id}/assemble")
    async def assemble(id: str, body: Assemble, owner: str = Depends(studio_actor_id)):
        return await call(
            service.assemble(id, owner, body.expected_revision, body.request_id)
        )

    @router.post("/{id}/release")
    async def release(id: str, body: Publish, owner: str = Depends(studio_actor_id)):
        return await call(service.release(id, owner, body.expected_revision, body.name))

    # Files stream through the API after the owner check on every request, so a
    # link works only for the person it was given to. `proxy` is accepted for
    # older clients; every response is same-origin now.
    @router.get("/{id}/releases/{release_id}/download")
    async def release_download(
        id: str, release_id: str, request: Request, proxy: bool = False,
        owner: str = Depends(studio_actor_id),
    ):
        key, filename = await call(service.release_file(id, owner, release_id))
        return await call(asset_response(service.assets, key, request, filename=filename))

    @router.get("/{id}/assets/{key:path}")
    async def asset(
        id: str, key: str, request: Request, proxy: bool = False,
        owner: str = Depends(studio_actor_id),
    ):
        await call(service.authorize_asset(id, owner, key))
        return await call(asset_response(service.assets, key, request))

    return router

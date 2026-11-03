"""Scene flights: frame-pinned camera legs between location images.

A scene flight is stored as a movie of kind ``scene_flight``. Each leg is a
shot whose opening image is the origin and whose closing image is the
destination; each generation is a take. Persistence, owner limits, durable
submission, reconciliation, worker provenance, delivery and assembly are the
movie pipeline's own. This module only owns the leg contract and its view.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from typing import Any

from PIL import Image, ImageOps
from pydantic import Field, model_validator

from magic_video_editor.flights import media, prompt
from magic_video_editor.movies.models import Clip, Model, Movie, Shot, Take
from magic_video_editor.movies.service import (
    MovieError,
    MovieService,
    revision_conflict,
)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
MAX_LEGS = 24


class LegInput(Model):
    leg_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    origin_image_key: str = Field(min_length=1, max_length=400)
    destination_image_key: str = Field(min_length=1, max_length=400)
    # The camera direction. Omit it to compose Gaia's leg wording from names.
    prompt: str = Field(default="", max_length=1600)
    origin_name: str = Field(default="", max_length=200)
    destination_name: str = Field(default="", max_length=200)
    origin_description: str = Field(default="", max_length=600)
    destination_description: str = Field(default="", max_length=600)
    negative_prompt: str = Field(default="", max_length=400)
    name: str = Field(default="", max_length=200)
    duration_seconds: float = Field(
        default=media.DEFAULT_SECONDS,
        ge=media.MIN_SECONDS,
        le=media.MAX_SECONDS,
        multiple_of=0.5,
    )

    @model_validator(mode="after")
    def _direction(self):
        if not self.prompt.strip() and not (
            self.origin_name.strip() and self.destination_name.strip()
        ):
            raise ValueError("Give a prompt, or both place names to compose one")
        return self

    def direction(self) -> str:
        if self.prompt.strip():
            return " ".join(self.prompt.split())
        return prompt.default_leg_prompt(
            self.origin_name,
            self.destination_name,
            self.origin_description,
            self.destination_description,
        )

    def label(self, index: int) -> str:
        if self.name.strip():
            return self.name.strip()
        if self.origin_name.strip() and self.destination_name.strip():
            return f"{self.origin_name.strip()} to {self.destination_name.strip()}"
        return f"Leg {index + 1}"


def recipe() -> dict[str, Any]:
    return {
        "capability_id": media.CAPABILITY_ID,
        "width": media.WIDTH,
        "height": media.HEIGHT,
        "fps": media.FPS,
        "steps": media.STEPS,
        "default_duration_seconds": media.DEFAULT_SECONDS,
        "min_duration_seconds": media.MIN_SECONDS,
        "max_duration_seconds": media.MAX_SECONDS,
        "duration_step_seconds": 0.5,
        "max_legs": MAX_LEGS,
    }


def _take_view(t: Take) -> dict[str, Any]:
    return {
        "take_id": t.take_id,
        "request_id": t.request_id,
        "status": t.status,
        "stale": t.stale,
        "error": t.error,
        "video_key": t.asset_key if t.status == "succeeded" else None,
        "last_frame_key": t.final_frame_key,
        "origin_frame_key": t.provider_frame_key,
        "destination_frame_key": t.closing_frame_key,
        "prompt": t.prompt,
        "seed": t.seed,
        "width": t.provider_width,
        "height": t.provider_height,
        "fps": t.provider_fps,
        "length_frames": t.length_frames,
        "steps": t.steps,
        "capability_id": t.capability_id,
        "executor_preset": t.executor_preset,
        # Requested length until the clip exists, then its measured length.
        "duration_seconds": t.duration_seconds,
        "first_frame_diff": t.first_frame_diff,
        "last_frame_diff": t.last_frame_diff,
        "job_id": t.job_id,
        "completed_by_worker_id": t.completed_by_worker_id,
        "completed_at": t.completed_at,
        "output_sha256": t.output_sha256,
        "output_byte_length": t.output_byte_length,
    }


def _leg_view(index: int, s: Shot) -> dict[str, Any]:
    return {
        "leg_id": s.shot_id,
        "index": index,
        "name": s.name,
        "origin_image_key": s.opening_asset_key,
        "destination_image_key": s.closing_asset_key,
        "prompt": s.prompt,
        "negative_prompt": s.negative_prompt,
        "duration_seconds": s.duration_seconds,
        "selected_take_id": s.selected_take_id,
        "takes": [_take_view(t) for t in s.takes],
    }


class SceneFlightService:
    def __init__(self, movies: MovieService) -> None:
        self.movies = movies

    @property
    def generation_available(self) -> bool:
        return self.movies.pool is not None and self.movies.scene_flights_available

    def view(self, movie: Movie) -> dict[str, Any]:
        chain = None
        if movie.assembly is not None:
            chain = {
                "request_id": movie.assembly.request_id,
                "status": movie.assembly.status,
                "video_key": movie.assembly.asset_key
                if movie.assembly.status == "succeeded"
                else None,
                "error": movie.assembly.error,
                "joins": [j.model_dump() for j in movie.assembly.joins],
            }
        return {
            "flight_id": movie.movie_id,
            "name": movie.name,
            "revision": movie.revision,
            "generation_available": self.generation_available,
            "recipe": recipe(),
            "images": list(movie.upload_keys),
            "legs": [_leg_view(i, s) for i, s in enumerate(movie.shots)],
            "chain": chain,
        }

    async def flight(self, id: str, owner: str) -> Movie:
        movie = await self.movies.get(id, owner)
        if movie.kind != "scene_flight":
            raise MovieError("Scene flight not found", 404)
        return movie

    async def create(self, owner: str, name: str) -> dict[str, Any]:
        return self.view(await self.movies.create(owner, name, kind="scene_flight"))

    async def list(self, owner: str) -> list[dict[str, Any]]:
        return [
            {
                "flight_id": m.movie_id,
                "name": m.name,
                "revision": m.revision,
                "leg_count": len(m.shots),
                "active_takes": sum(
                    t.status in {"waiting", "submitting", "running"}
                    for s in m.shots
                    for t in s.takes
                ),
                "expires_at": expires_at,
            }
            for m, expires_at in await self.movies.list_with_expiry(owner, "scene_flight")
        ]

    async def get(self, id: str, owner: str) -> dict[str, Any]:
        await self.flight(id, owner)
        return self.view(await self.movies.reconcile(id, owner))

    async def add_image(
        self, id: str, owner: str, content: bytes, content_type: str
    ) -> dict[str, Any]:
        """Normalize an origin/destination image to an immutable PNG and grant
        it to this flight. Identical images share one key."""
        await self.flight(id, owner)
        if content_type not in IMAGE_TYPES:
            raise MovieError("Use a PNG, JPEG or WebP image")
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise MovieError("Images must be between 1 byte and 20 MiB")
        if self.movies.work_guard is not None:
            await self.movies.work_guard(owner, 0)
        try:
            with Image.open(BytesIO(content)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise MovieError("Image exceeds 25 megapixels")
                normalized = ImageOps.exif_transpose(image).convert("RGB")
                width, height = normalized.size
                output = BytesIO()
                normalized.save(output, "PNG")
        except MovieError:
            raise
        except Exception as e:
            raise MovieError("Invalid image") from e
        data = output.getvalue()
        digest = sha256(data).hexdigest()
        key = f"outputs/movies/{id}/openings/{digest}.png"
        if await self.movies.assets.describe(key) is None:
            await self.movies.assets.write_once(key, data, "image/png")
        for _ in range(8):
            movie = await self.flight(id, owner)
            if key in movie.upload_keys:
                break
            movie.upload_keys.append(key)
            try:
                movie = await self.movies.save(movie, movie.revision)
                break
            except MovieError as e:
                if e.status != 409:
                    raise
        else:
            raise MovieError("Scene flight is busy; retry", 409)
        return {
            "image_key": key,
            "width": width,
            "height": height,
            "sha256": digest,
            "revision": movie.revision,
        }

    async def set_legs(
        self, id: str, owner: str, expected: int, legs: list[LegInput]
    ) -> dict[str, Any]:
        movie = await self.flight(id, owner)
        if movie.revision != expected:
            raise revision_conflict(movie.revision)
        ids = [leg.leg_id for leg in legs]
        if len(ids) != len(set(ids)) or len(ids) > MAX_LEGS:
            raise MovieError(f"Use at most {MAX_LEGS} legs with unique IDs")
        images = set(movie.upload_keys)
        for leg in legs:
            for key in (leg.origin_image_key, leg.destination_image_key):
                if key not in images or not key.startswith(f"outputs/movies/{id}/openings/"):
                    raise MovieError("Add each leg image to this flight first", 403)
            if leg.origin_image_key == leg.destination_image_key:
                raise MovieError(f"{leg.label(0)}: origin and destination must differ")
        for existing in movie.shots:
            if existing.shot_id not in ids and any(
                t.status in {"waiting", "submitting", "running"} for t in existing.takes
            ):
                raise MovieError("Cancel active takes before removing a leg", 409)
        old = {s.shot_id: s for s in movie.shots}
        changed: set[str] = set()
        updated = []
        for index, leg in enumerate(legs):
            spec = {
                "shot_id": leg.leg_id,
                "name": leg.label(index),
                "prompt": leg.direction(),
                "duration_seconds": leg.duration_seconds,
                "opening_asset_key": leg.origin_image_key,
                "closing_asset_key": leg.destination_image_key,
                "negative_prompt": " ".join(leg.negative_prompt.split()),
            }
            if len(prompt.build_prompt(spec["prompt"], spec["negative_prompt"])) > prompt.MAX_PROMPT_CHARS:
                raise MovieError(f"{spec['name']}: shorten the leg prompt; H3 takes at most 2000 characters")
            shot = old.get(leg.leg_id)
            if shot is None:
                updated.append(Shot(**spec))
                continue
            inputs = ("prompt", "duration_seconds", "opening_asset_key", "closing_asset_key", "negative_prompt")
            if any(getattr(shot, k) != spec[k] for k in inputs):
                changed.add(leg.leg_id)
            updated.append(Shot(**{**shot.model_dump(), **spec}))
        movie.shots = updated
        # A changed leg's earlier takes no longer show its inputs: mark them
        # stale (cancelling any still running), like an edited movie shot.
        self.movies.invalidate(movie, changed)
        for s in movie.shots:
            if s.shot_id in changed:
                s.selected_take_id = None
        movie.cut = [c for c in movie.cut if c.shot_id in ids]
        if changed or len(ids) != len(old):
            movie.assembly = None
        return self.view(await self.movies.save(movie, expected))

    async def generate(
        self, id: str, owner: str, expected: int, leg_id: str, request_id: str, seed: int | None = None
    ) -> dict[str, Any]:
        """Start one take of one leg. Another request ID is a re-roll; the
        newest successful take is selected when it completes."""
        await self.flight(id, owner)
        movie = await self.movies.generate(
            id, owner, expected, [leg_id], request_id,
            select_when_ready=True,
            seeds=None if seed is None else {leg_id: seed},
        )
        return self.view(movie)

    async def select(self, id: str, owner: str, expected: int, leg_id: str, take_id: str) -> dict[str, Any]:
        await self.flight(id, owner)
        return self.view(await self.movies.select(id, owner, expected, leg_id, take_id))

    async def cancel(self, id: str, owner: str, expected: int, leg_id: str, take_id: str) -> dict[str, Any]:
        await self.flight(id, owner)
        return self.view(await self.movies.cancel(id, owner, expected, leg_id, take_id))

    async def chain(self, id: str, owner: str, expected: int, request_id: str) -> dict[str, Any]:
        """Join every leg's selected take, in leg order, with hard cuts. The
        legs must share their boundary images so each join is pinned to the
        same picture on both sides."""
        movie = await self.flight(id, owner)
        if movie.assembly and movie.assembly.request_id == request_id:
            return self.view(await self.movies.reconcile(id, owner))
        if movie.revision != expected:
            raise revision_conflict(movie.revision)
        if not movie.shots:
            raise MovieError("Add a leg before chaining")
        for previous, following in zip(movie.shots, movie.shots[1:]):
            if previous.closing_asset_key != following.opening_asset_key:
                raise MovieError(
                    f"{following.name} must start on the image {previous.name} lands on"
                )
        clips = []
        for s in movie.shots:
            take = next((t for t in s.takes if t.take_id == s.selected_take_id), None)
            if take is None or take.status != "succeeded" or take.stale:
                raise MovieError(f"{s.name}: select a successful take first")
            clips.append(
                Clip(shot_id=s.shot_id, take_id=take.take_id, in_seconds=0, out_seconds=take.duration_seconds)
            )
        movie = await self.movies.cut(id, owner, expected, clips)
        movie = await self.movies.assemble(id, owner, movie.revision, request_id)
        return self.view(movie)

    async def take_video_key(self, id: str, owner: str, leg_id: str, take_id: str) -> str:
        movie = await self.flight(id, owner)
        take = self.movies.take(movie, leg_id, take_id)
        if take.status != "succeeded":
            raise MovieError("This take has no video yet", 404)
        return take.asset_key

    async def chain_video_key(self, id: str, owner: str) -> str:
        movie = await self.flight(id, owner)
        if movie.assembly is None or movie.assembly.status != "succeeded":
            raise MovieError("Chain the flight first", 404)
        return movie.assembly.asset_key

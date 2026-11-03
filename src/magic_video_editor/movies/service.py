"""Movie production with durable, dependency-aware GPU submission intents.

Requests first append their complete intent under CAS. Reconciliation may run on
any process: resubmission uses the same pool job and idempotency key, and completed
external work is folded into a fresh snapshot with CAS retries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from itertools import pairwise
from typing import Any, Protocol
from uuid import UUID, uuid4

from outbound_gpu_worker_pool.contracts import JobRequirements, JobSubmission
from PIL import Image, ImageOps, UnidentifiedImageError

from magic_video_editor.contracts import AssetStore, CompositionGenerator, upload_size_headers
from magic_video_editor.cpu_worker import ASSEMBLY_RENDER, JOIN_LIMIT
from magic_video_editor.flights import media as flight_media
from magic_video_editor.machine_features import (
    MOVIE_TAKES,
    SUBJECTS,
    Feature,
    MachineCannotRun,
    require_route,
    revoked_by,
    subject_route,
)
from magic_video_editor.flights import prompt as flight_prompt
from magic_video_editor.movies import dialogue, motion
from magic_video_editor.movies.media import MovieMedia
from magic_video_editor.movies.cpu_pool import CpuPending
from magic_video_editor.movies.models import (
    Assembly,
    Character,
    CharacterInput,
    Clip,
    Join,
    Movie,
    Release,
    Shot,
    ShotInput,
    Take,
)
from magic_video_editor.movies.planner import HttpMoviePlanner
from magic_video_editor.movies.soundtracks import MAX_AUDIO_BYTES
from magic_video_editor.movies.store import Conflict, MovieStore
from magic_video_editor.plugins.composition_http import TerminalCompositionProviderError
from magic_video_editor.retention import MOVIE, Retention
from magic_video_editor.safety import PromptRefused, PromptScreening, PromptScreenUnavailable
from magic_video_editor.studio.accounts import (
    AccountPolicy,
    AccountPolicyRefused,
    MACHINE_CANNOT_RUN,
    own_compute_required,
)

logger = logging.getLogger(__name__)

# What each kind of movie upload is read with: openings, portraits and frames;
# imported clips; voice clips.
UPLOAD_LIMITS = {"image": 20 * 1024 * 1024, "video": 256 * 1024 * 1024, "audio": MAX_AUDIO_BYTES}


MOTION_SILENCE = motion.SILENCE


class MoviePool(Protocol):
    async def submit(self, request: JobSubmission) -> Any: ...
    async def get(self, id: str) -> Any: ...
    async def cancel(self, id: str) -> bool: ...


class MovieError(Exception):
    def __init__(
        self,
        message: str,
        status: int = 422,
        *,
        current_revision: int | None = None,
        code: str | None = None,
    ) -> None:
        self.status = status
        # Set only for stale-revision conflicts, so clients can retry without a re-read.
        self.current_revision = current_revision
        # A machine-readable reason, for refusals a client may act on.
        self.code = code
        super().__init__(message)

    @classmethod
    def refused(cls, refusal: AccountPolicyRefused) -> MovieError:
        return cls(refusal.message, 403, code=refusal.code)


def shot_texts(shot: ShotInput) -> list[str]:
    """Everything a person wrote for a shot that can reach a generation prompt."""
    return [
        shot.prompt, shot.opening_prompt, shot.soundscape, shot.camera, shot.still,
        *(beat.action for beat in shot.beats),
        *(" ".join(filter(None, (line.text, line.delivery, line.reaction))) for line in shot.dialogue),
    ]


def revision_conflict(current: int) -> MovieError:
    return MovieError(
        f"Revision conflict; current revision is {current}", 409,
        current_revision=current,
    )


def digest(value: Any) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MovieService:
    def __init__(
        self,
        store: MovieStore,
        assets: AssetStore,
        pool: MoviePool | None = None,
        composition: CompositionGenerator | None = None,
        media: MovieMedia | None = None,
        max_batch_size: int = 6,
        planner: HttpMoviePlanner | None = None,
        tenant_for: Callable[[str], Awaitable[str | None]] | None = None,
        shared_tenant_for: Callable[[str], Awaitable[str | None]] | None = None,
        house_tenants: frozenset[str] = frozenset(),
        work_guard: Callable[[str, int], Awaitable[None]] | None = None,
        director: Any | None = None,
        account_policy: AccountPolicy | None = None,
        scene_flights_available: bool = False,
        prompt_screening: PromptScreening | None = None,
        retention: Retention | None = None,
    ) -> None:
        self.store = store
        # Inactivity expiry for unreleased movies and flights; None keeps them.
        self.retention = retention
        self.prompt_screening = prompt_screening if prompt_screening is not None else PromptScreening(None)
        # Scene-flight legs need an executor whose H3 template binds a last
        # frame; the composition root decides from the actual templates.
        self.scene_flights_available = scene_flights_available
        self.assets = assets
        self.pool = pool
        self.composition = composition
        self.media = media
        self.max_batch_size = max_batch_size
        self.planner = planner
        self.tenant_for = tenant_for
        self.shared_tenant_for = shared_tenant_for
        # Shared operator tenants that route like house work (`studio.operators`).
        self.house_tenants = house_tenants
        self.work_guard = work_guard
        self.director = director
        self.account_policy = account_policy if account_policy is not None else AccountPolicy()
        # Studio enrolment, so a removed machine never decides where new work goes.
        self.enrolment: Any = None

    async def resolve_tenant(
        self, movie: Movie, *, h3: bool = False, shared_only: bool = False
    ) -> str | None:
        """Where this movie's work runs. A public account runs only on its own
        machines, whatever the movie's compute target says, and runs MiniMax H3
        only when the operator has enabled it for public accounts; `shared_only`
        marks a provider with no per-owner machines."""
        policy = self.account_policy
        if await policy.is_public(movie.owner_id):
            try:
                if h3:
                    await policy.require_h3(movie.owner_id)
                tenant = await self.tenant_for(movie.owner_id) if self.tenant_for else None
                if shared_only or tenant is None:
                    raise own_compute_required()
            except AccountPolicyRefused as refusal:
                raise MovieError.refused(refusal) from refusal
            return tenant
        if movie.compute_target == "shared":
            return await self.shared_tenant_for(movie.owner_id) if self.shared_tenant_for else None
        tenant = await self.tenant_for(movie.owner_id) if self.tenant_for else None
        if tenant is None:
            raise MovieError("Attach a machine before choosing My machines")
        return tenant

    async def require_machines(self, tenant_id: str | None, feature: Feature) -> None:
        """Refuse work none of the owner's machines can run, with each machine's
        reason, before anything is queued for a machine that will never lease it."""
        try:
            await require_route(self.pool, tenant_id, feature,
                                house_tenants=self.house_tenants,
                                revoked=revoked_by(self.enrolment))
        except MachineCannotRun as refusal:
            raise MovieError(refusal.message, code=MACHINE_CANNOT_RUN) from refusal

    async def running_jobs(self, owner: str) -> int:
        return sum(
            sum(
                t.status in {"waiting", "submitting", "running"}
                for shot in movie.shots
                for t in shot.takes
            )
            + sum(
                (
                    intent["status"] == "pending"
                    or (
                        intent["status"] == "superseded"
                        and not intent.get("settled", False)
                    )
                )
                for intent in movie.compositions.values()
            )
            for movie in await self.store.list(owner)
        )

    async def create(self, owner: str, name: str, kind: str = "movie") -> Movie:
        movie = await self.store.create(
            Movie(movie_id=str(uuid4()), owner_id=owner, name=name, kind=kind)
        )
        if self.retention is not None:
            # Best effort, as for projects: the movie exists already, and an
            # untracked one gets a full period when the sweep first sees it.
            try:
                await self.retention.touch(MOVIE, movie.movie_id, owner)
            except Exception:
                logger.warning("retention.touch_failed_on_create", exc_info=True)
        return movie

    async def get(self, id: str, owner: str) -> Movie:
        movie = await self.store.get(id)
        if movie is None or movie.owner_id != owner:
            raise MovieError("Movie not found", 404)
        # Every owner operation passes here: this is "opened" for expiry.
        if self.retention is not None:
            await self.retention.touch(MOVIE, id, owner)
        return movie

    async def list_with_expiry(self, owner: str, kind: str) -> list[tuple[Movie, datetime | None]]:
        """The owner's movies (or flights) with when inactivity expiry deletes
        each unless it is opened; released work never expires."""
        movies = [m for m in await self.store.list(owner) if m.kind == kind]
        expiries = (
            await self.retention.expiring(MOVIE, [m.movie_id for m in movies])
            if self.retention is not None else {}
        )
        return [(m, None if m.releases else expiries.get(m.movie_id)) for m in movies]

    async def expire(self, id: str) -> bool:
        """Delete a movie or flight for inactivity; False keeps it (released).
        Statuses only advance when the owner reconciles, so a take still marked
        running after this long is stale; the purge cancels its job anyway."""
        movie = await self.store.get(id)
        if movie is None:
            return True
        if movie.releases:
            return False
        logger.info("retention.expired", extra={"kind": MOVIE, "item_id": id})
        await self.purge(movie)
        return True

    async def save(self, movie: Movie, expected: int) -> Movie:
        try:
            saved = await self.store.append(movie, expected)
            await self.account_assets(saved)
            return saved
        except Conflict as e:
            raise revision_conflict(e.revision) from e

    async def account_assets(self, movie: Movie) -> None:
        """Idempotent ownership ledger; retries never double-count stored media."""

        def keys(value):
            if isinstance(value, str) and value.startswith(
                (
                    f"uploads/movies/{movie.movie_id}/",
                    f"outputs/movies/{movie.movie_id}/",
                )
            ):
                return {value}
            if isinstance(value, dict):
                return set().union(*(keys(v) for v in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(v) for v in value))
            return set()

        try:
            pending = keys(movie.model_dump()) - await self.store.accounted_keys(
                movie.movie_id
            )
            for intent in movie.compositions.values():
                if intent.get("provider") == "pool":
                    pending.add(intent["output_key"].removesuffix(".png") + ".raw.png")
            pending -= await self.store.accounted_keys(movie.movie_id)
            for key in pending:
                descriptor = await self.assets.describe(key)
                if descriptor is not None:
                    await self.store.account_asset(movie.movie_id, key, descriptor.size)
        except Exception:
            logger.exception("Movie asset accounting will retry on next saved revision")

    async def current(self, id: str, owner: str, expected: int) -> Movie:
        movie = await self.get(id, owner)
        if movie.revision != expected:
            raise revision_conflict(movie.revision)
        return movie

    def shot(self, movie: Movie, sid: str) -> Shot:
        shot = next((s for s in movie.shots if s.shot_id == sid), None)
        if not shot:
            raise MovieError("Shot not found", 404)
        return shot

    def take(self, movie: Movie, sid: str, tid: str) -> Take:
        take = next((t for t in self.shot(movie, sid).takes if t.take_id == tid), None)
        if not take:
            raise MovieError("Take not found", 404)
        return take

    def invalidate(
        self,
        movie: Movie,
        roots: set[str],
        keep_waiting: bool = False,
        keep_compositions: bool = False,
    ) -> None:
        affected = set(roots)
        if not affected:
            return
        for _ in movie.shots:
            affected.update(
                s.shot_id for s in movie.shots if s.continuation_of in affected
            )
        for s in movie.shots:
            if s.shot_id in affected:
                for t in s.takes:
                    if (
                        keep_waiting
                        and t.status == "waiting"
                        and not t.predecessor_take_id
                    ):
                        continue
                    t.stale = True
                    if t.status == "waiting":
                        t.status = "cancelled"
                    elif t.status in {"submitting", "running"}:
                        t.cancel_requested = True
        for intent in movie.compositions.values():
            if (
                not keep_compositions
                and intent["shot_id"] in affected
                and intent["status"] == "pending"
            ):
                intent["status"] = "superseded"
        movie.assembly = None

    async def story(
        self,
        id: str,
        owner: str,
        expected: int,
        *,
        brief: str,
        style: str,
        shots: list[ShotInput | dict[str, Any]],
        batch_size: int,
        compute_target: str = "shared",
    ) -> Movie:
        movie = await self.current(id, owner, expected)
        if movie.kind != "movie":
            raise MovieError("Edit scene-flight legs through /studio/scene-flights")
        if not 1 <= batch_size <= self.max_batch_size:
            raise MovieError("Batch size exceeds configured limit")
        inputs = [ShotInput.model_validate(s) for s in shots]
        ids = [s.shot_id for s in inputs]
        if len(ids) != len(set(ids)) or len(ids) > 60:
            raise MovieError("Use at most 60 unique shots")
        for spec in inputs:
            if spec.dialogue:
                try:
                    dialogue.speakers_for(spec.dialogue, movie.characters)
                    dialogue.plan(spec.dialogue, continues=spec.continuation_of is not None)
                except ValueError as e:
                    raise MovieError(f"{spec.name}: {e}") from e
        for existing in movie.shots:
            if existing.shot_id not in ids and any(
                t.status in {"waiting", "submitting", "running"} for t in existing.takes
            ):
                raise MovieError("Cancel active takes before removing a shot", 409)
        links = {s.shot_id: s.continuation_of for s in inputs}
        for sid in ids:
            visited = set()
            cursor = sid
            while cursor is not None:
                if cursor in visited or cursor not in links:
                    raise MovieError(
                        "Continuation must reference another shot without cycles"
                    )
                visited.add(cursor)
                cursor = links[cursor]
        if any(
            c["shot_id"] not in ids and c["status"] == "pending"
            for c in movie.compositions.values()
        ):
            raise MovieError(
                "Wait for opening generation before removing its shot", 409
            )
        old = {s.shot_id: s for s in movie.shots}
        changed = set()
        updated = []
        for spec in inputs:
            shot = old.get(spec.shot_id)
            if shot:
                prior = ShotInput.model_validate(
                    {k: getattr(shot, k) for k in ShotInput.model_fields}
                )
                if prior != spec or movie.style != style:
                    changed.add(spec.shot_id)
                    if (
                        prior.opening_prompt != spec.opening_prompt
                        or prior.continuation_of != spec.continuation_of
                        or movie.style != style
                    ):
                        shot.opening_approved = False
                # Re-validate: a copy with an update keeps nested dialogue
                # lines as plain dicts.
                shot = Shot(**{**shot.model_dump(), **spec.model_dump()})
            else:
                shot = Shot(**spec.model_dump())
            updated.append(shot)
        movie.shots = updated
        movie.brief = brief
        movie.style = style
        if compute_target not in {"shared", "my_machines"}:
            raise MovieError("Choose shared or my_machines compute")
        movie.compute_target = compute_target
        movie.batch_size = batch_size
        self.invalidate(movie, changed)
        retained_cut = [c for c in movie.cut if c.shot_id in ids]
        if retained_cut != movie.cut:
            movie.assembly = None
        movie.cut = retained_cut
        return await self.save(movie, expected)

    async def characters(
        self,
        id: str,
        owner: str,
        expected: int,
        characters: list[CharacterInput | dict[str, Any]],
    ) -> Movie:
        movie = await self.current(id, owner, expected)
        # A saved character echoed back carries server-only fields; ignore them.
        inputs = [
            CharacterInput.model_validate(
                {
                    k: v
                    for k, v in (c if isinstance(c, dict) else c.model_dump()).items()
                    if k in CharacterInput.model_fields
                }
            )
            for c in characters
        ]
        ids = [c.character_id for c in inputs]
        if len(ids) != len(set(ids)) or len(ids) > 8:
            raise MovieError("Use at most 8 unique characters")
        spoken = {line.character_id for s in movie.shots for line in s.dialogue}
        if spoken - set(ids):
            raise MovieError("Remove a character's lines before removing the character")
        old = {c.character_id: c for c in movie.characters}
        frozen = {
            c.voice_asset_key: c.voice_seconds
            for c in movie.characters
            if c.voice_asset_key
        }
        updated = []
        changed = set()
        for spec in inputs:
            key, seconds = spec.voice_asset_key, None
            if key is not None:
                if key in frozen:
                    seconds = frozen[key]
                elif key in movie.upload_keys:
                    key, seconds = await self.freeze_voice(id, key, movie.owner_id)
                else:
                    raise MovieError("Voice reference is not granted to this movie", 403)
            previous = old.get(spec.character_id)
            keep_portrait = (
                previous is not None
                and previous.portrait_appearance == spec.appearance
            )
            character = Character(
                **spec.model_dump(exclude={"voice_asset_key"}),
                voice_asset_key=key,
                voice_seconds=seconds,
                portrait_asset_key=previous.portrait_asset_key if keep_portrait else None,
                portrait_appearance=previous.portrait_appearance if keep_portrait else None,
            )
            if old.get(spec.character_id) != character:
                changed.add(spec.character_id)
            updated.append(character)
        movie.characters = updated
        # A new voice or look changes every take that character speaks in.
        self.invalidate(
            movie,
            {
                s.shot_id
                for s in movie.shots
                if any(line.character_id in changed for line in s.dialogue)
            },
        )
        return await self.save(movie, expected)

    async def attach_voice(
        self, id: str, owner: str, expected: int, cid: str, asset_key: str
    ) -> Movie:
        """Attach a granted clip without replacing the caller's entire cast."""
        movie = await self.current(id, owner, expected)
        if not any(c.character_id == cid for c in movie.characters):
            raise MovieError("Movie character not found", 404)
        cast = [c.model_dump() for c in movie.characters]
        for character in cast:
            if character["character_id"] == cid:
                character["voice_asset_key"] = asset_key
        return await self.characters(id, owner, expected, cast)

    async def freeze_voice(self, id: str, key: str, owner_id: str) -> tuple[str, float]:
        """Verify an uploaded voice clip against H3's reference limits and
        store it immutably; the same bytes always freeze to the same key."""
        media = await self._service_media(owner_id, "Voice upload")
        if media is None:
            raise MovieError("Audio processing is unavailable.", 503)
        content = await self.assets.read_limited(key, MAX_AUDIO_BYTES)
        try:
            seconds = await media.audio_duration(content)
        except Exception as e:
            raise MovieError("Choose a valid WAV or MP3 voice clip under 50 MB.") from e
        if not dialogue.VOICE_MIN_SECONDS <= seconds <= dialogue.VOICE_MAX_SECONDS:
            raise MovieError(
                f"A voice reference is {dialogue.VOICE_MIN_SECONDS:g}-"
                f"{dialogue.VOICE_MAX_SECONDS:g} seconds of the character speaking "
                f"alone; this clip is {seconds:.1f}s"
            )
        wav = content[:4] in {b"RIFF", b"RF64", b"BW64"} and content[8:12] == b"WAVE"
        immutable = (
            f"outputs/movies/{id}/voices/{sha256(content).hexdigest()}."
            + ("wav" if wav else "mp3")
        )
        await self.assets.write_once(
            immutable, content, "audio/wav" if wav else "audio/mpeg"
        )
        return immutable, round(seconds, 3)

    async def upload(self, id: str, owner: str, content_type: str) -> dict[str, Any]:
        if content_type not in {
            "image/png",
            "image/jpeg",
            "image/webp",
            "video/mp4",
            "audio/mpeg",
            "audio/wav",
        }:
            raise MovieError("Unsupported movie upload type")
        if self.work_guard is not None:
            await self.work_guard(owner, 0)
        # Grant is durable and owner-scoped before issuing a signed URL.
        for _ in range(8):
            movie = await self.get(id, owner)
            key = f"uploads/movies/{id}/{uuid4()}"
            movie.upload_keys.append(key)
            try:
                movie = await self.save(movie, movie.revision)
                break
            except MovieError as e:
                if e.status != 409:
                    raise
        else:
            raise MovieError("Movie is busy; retry", 409)
        if not await self.account_policy.is_public(movie.owner_id):
            return {
                "asset_key": key,
                "upload_url": await self.assets.create_upload_url(key, content_type),
                "revision": movie.revision,
            }
        # A public account's grant carries a signed ceiling: the most this kind
        # of upload is ever read with.
        limit = UPLOAD_LIMITS[content_type.split("/")[0]]
        return {
            "asset_key": key,
            "upload_url": await self.assets.create_upload_url(
                key, content_type, max_bytes=limit
            ),
            "upload_headers": upload_size_headers(limit),
            "revision": movie.revision,
        }

    async def opening(
        self, id: str, owner: str, expected: int, sid: str, key: str
    ) -> Movie:
        movie = await self.current(id, owner, expected)
        shot = self.shot(movie, sid)
        if key not in movie.upload_keys:
            raise MovieError("Opening image is not granted to this movie", 403)
        content = await self.assets.read_limited(key, 20 * 1024 * 1024)
        try:
            with Image.open(BytesIO(content)) as im:
                if im.width * im.height > 25_000_000:
                    raise ValueError("Image exceeds 25 megapixels")
                im.load()
                output = BytesIO()
                im.convert("RGB").save(output, "PNG")
                content = output.getvalue()
        except Exception as e:
            raise MovieError("Invalid opening image") from e
        immutable = f"outputs/movies/{id}/openings/{sha256(content).hexdigest()}.png"
        await self.assets.write_once(immutable, content, "image/png")
        shot.opening_asset_key = immutable
        shot.opening_approved = False
        self.invalidate(movie, {sid})
        return await self.save(movie, expected)

    async def approve(self, id: str, owner: str, expected: int, sid: str) -> Movie:
        movie = await self.current(id, owner, expected)
        shot = self.shot(movie, sid)
        if not shot.opening_asset_key and not shot.continuation_of:
            raise MovieError("Choose an opening image before approval")
        shot.opening_approved = True
        return await self.save(movie, expected)

    async def generate(
        self, id: str, owner: str, expected: int, shot_ids: list[str], request_id: str, *, advance_build: bool = True, select_when_ready: bool = False,
        seeds: dict[str, int] | None = None,
    ) -> Movie:
        movie = await self.get(id, owner)
        if request_id in movie.requests:
            if movie.requests[request_id] != shot_ids:
                raise MovieError("Request ID already used with different shots", 409)
            return movie
        if self.pool is None:
            raise MovieError(
                "Network GPU pool is unavailable; movie planning remains available", 503
            )
        if movie.revision != expected:
            raise revision_conflict(movie.revision)
        if not request_id or len(request_id) > 120:
            raise MovieError("A request ID of at most 120 characters is required")
        if (
            not shot_ids
            or len(shot_ids) > movie.batch_size
            or len(set(shot_ids)) != len(shot_ids)
        ):
            raise MovieError("Select a unique batch within the configured batch size")
        active = sum(
            t.status in {"waiting", "submitting", "running"}
            for s in movie.shots
            for t in s.takes
        )
        if active + len(shot_ids) > movie.batch_size:
            raise MovieError("This batch would exceed the active take budget")
        flight = movie.kind == "scene_flight"
        if flight and not self.scene_flights_available:
            raise MovieError(
                "Scene flights need a GPU executor whose H3 template pins a last "
                "frame; this deployment's workers do not declare one yet",
                503,
            )
        planned: dict[str, dict[str, Any]] = {}
        for sid in shot_ids:
            s = self.shot(movie, sid)
            if flight:
                if not s.opening_asset_key or not s.closing_asset_key:
                    raise MovieError(f"{s.name}: choose an origin and a destination image first")
                if len(flight_prompt.build_prompt(s.prompt, s.negative_prompt)) > flight_prompt.MAX_PROMPT_CHARS:
                    raise MovieError(f"{s.name}: shorten the leg prompt; H3 takes at most 2000 characters")
            elif s.dialogue:
                planned[sid] = self.plan_dialogue(movie, s, f"outputs/movies/{id}")
            elif len(motion.build_prompt(s, continues=s.continuation_of is not None)) > 2000:
                raise MovieError(
                    f"{s.name}: shorten the motion directions; H3 takes at most 2000 characters"
                )
            if not s.continuation_of and not s.opening_asset_key:
                raise MovieError(f"Add a starting image for {s.name} first")
            if any(t.status in {"waiting", "submitting", "running"} for t in s.takes):
                raise MovieError(f"{s.name} already has an active take", 409)
        if self.work_guard is not None:
            await self.work_guard(owner, len(shot_ids))
        # Motion and dialogue takes are both MiniMax H3.
        tenant_id = await self.resolve_tenant(movie, h3=True)
        await self.require_machines(tenant_id, MOVIE_TAKES)
        prompts: list[str] = []
        for sid in shot_ids:
            s = self.shot(movie, sid)
            tid = str(uuid4())
            fields: dict[str, Any] = {
                # A motion take's own sound now survives assembly, so say
                # plainly that nobody speaks in it.
                "prompt": motion.build_prompt(s, continues=s.continuation_of is not None),
                "duration_seconds": s.duration_seconds,
                # H3 synthesizes picture and audio on a 24 fps clock. Encoding
                # its frames at 16 fps stretches only the picture by 50%.
                # Persist the recipe so retries keep their original identity.
                "provider_fps": dialogue.FPS,
                "length_frames": dialogue.motion_frames(s.duration_seconds),
            }
            if sid in planned:
                fields = {
                    **planned[sid],
                    "plate_frame_key": f"outputs/movies/{id}/takes/{tid}.plate-768x448.png",
                }
            if flight:
                fields = {
                    "mode": "flight",
                    "prompt": flight_prompt.build_prompt(s.prompt, s.negative_prompt),
                    "duration_seconds": s.duration_seconds,
                    "provider_fps": flight_media.FPS,
                    "length_frames": dialogue.motion_frames(s.duration_seconds),
                    "closing_asset_key": s.closing_asset_key,
                    "closing_frame_key": f"outputs/movies/{id}/takes/{tid}.last-768x448.png",
                    "seed": seeds.get(sid) if seeds and sid in seeds
                    else int(sha256(tid.encode()).hexdigest()[:8], 16),
                    "steps": flight_media.STEPS,
                    "capability_id": flight_media.CAPABILITY_ID,
                }
            prompts.append(str(fields.get("prompt") or ""))
            s.takes.append(
                Take(
                    take_id=tid,
                    select_when_ready=select_when_ready,
                    job_id=tid,
                    tenant_id=tenant_id,
                    request_id=request_id,
                    asset_key=f"outputs/movies/{id}/takes/{tid}.mp4",
                    provider_height=448,
                    provider_frame_key=f"outputs/movies/{id}/takes/{tid}.first-768x448.png",
                    opening_asset_key=s.opening_asset_key
                    if not s.continuation_of
                    else None,
                    **fields,
                )
            )
        # The frozen take prompts are exactly what the machine receives.
        await self.prompt_screening.require_allowed(owner, "flight" if flight else "movie", prompts)
        movie.requests[request_id] = list(shot_ids)
        await self.save(movie, expected)
        return await self.reconcile(id, owner, advance_build=advance_build)

    def plan_dialogue(self, movie: Movie, s: Shot, prefix: str) -> dict[str, Any]:
        """Take fields for a dialogue shot: its length follows the lines, its
        prompt binds each speaker to their own voice, and it lands on its plate."""
        continues = s.continuation_of is not None
        try:
            speakers = dialogue.speakers_for(s.dialogue, movie.characters)
            dialogue.check_voices(speakers)
            take_plan = dialogue.plan(s.dialogue, continues=continues)
        except ValueError as e:
            raise MovieError(f"{s.name}: {e}") from e
        if not s.opening_asset_key:
            raise MovieError(f"Add a plate image for {s.name}; a dialogue shot ends on it")
        prompt = dialogue.build_prompt(
            s.dialogue,
            speakers,
            take_plan,
            style=movie.style,
            setting=s.opening_prompt.strip(),
            camera=s.prompt.strip(),
            soundscape=s.soundscape.strip(),
            continues=continues,
        )
        if len(prompt) > 6000:
            raise MovieError(f"{s.name}: shorten the lines or directions")
        return {
            "mode": "dialogue",
            "prompt": prompt,
            "duration_seconds": take_plan.seconds,
            "length_frames": take_plan.frames,
            "plate_asset_key": s.opening_asset_key,
            "voice_asset_keys": {
                slot: c.voice_asset_key
                for slot, c in zip(dialogue.VOICE_SLOTS, speakers)
                if c.voice_asset_key
            },
        }

    async def _service_media(self, owner_id: str, feature: str):
        """The service's own FFmpeg, for work that has no pool-backed path yet.
        Public accounts never use service compute, so they are refused."""
        if await self.account_policy.is_public(owner_id):
            raise MovieError.refused(
                own_compute_required(f"{feature} is not available for public accounts yet.")
            )
        return self.media

    async def _scoped_media(self, movie: Movie, tenant_id: str | None, scope: str):
        if not hasattr(self.media, "scoped"):
            return self.media
        # Public accounts never fall back to processing on the service itself.
        if await self.account_policy.is_public(movie.owner_id):
            return self.media.scoped(tenant_id, scope, local_fallback=False)
        return self.media.scoped(tenant_id, scope)

    async def build_movie(self, id: str, owner: str, expected: int, request_id: str) -> Movie:
        from magic_video_editor.movies.build import start_build
        if await self.account_policy.is_public(owner):
            # Refuse up front with the coded 403 rather than recording a build
            # that can only fail: all build work needs the owner's own machine,
            # and missing takes need MiniMax H3.
            movie = await self.get(id, owner)
            await self.resolve_tenant(movie, h3=any(not s.selected_take_id for s in movie.shots))
        return await start_build(self, id, owner, expected, request_id)

    async def reconcile(self, id: str, owner: str, *, advance_build: bool = True) -> Movie:
        movie = await self.reconcile_jobs(id, owner)
        if advance_build and movie.build and movie.build.status in {"generating", "assembling"}:
            from magic_video_editor.movies.build import advance
            return await advance(self, movie)
        return movie

    async def reconcile_jobs(self, id: str, owner: str) -> Movie:
        for _ in range(12):
            movie = await self.get(id, owner)
            changed = False
            # Pin prerequisites before crossing the external submission boundary.
            for s in movie.shots:
                for t in s.takes:
                    if t.status != "waiting" or t.stale:
                        continue
                    if s.continuation_of:
                        p = self.shot(movie, s.continuation_of)
                        selected = next(
                            (v for v in p.takes if v.take_id == p.selected_take_id),
                            None,
                        )
                        if (
                            not selected
                            or selected.status != "succeeded"
                            or selected.stale
                            or not selected.final_frame_key
                        ):
                            continue
                        t.predecessor_take_id = selected.take_id
                        t.opening_asset_key = selected.final_frame_key
                    t.status = "submitting"
                    changed = True
            if changed:
                try:
                    movie = await self.save(movie, movie.revision)
                except MovieError as e:
                    if e.status == 409:
                        continue
                    raise
            # Independent ready shots are submitted concurrently; stable IDs make
            # crash recovery and simultaneous reconcilers safe across processes.
            pending = [
                t for s in movie.shots for t in s.takes if t.status == "submitting"
            ]
            if pending and self.pool is not None:
                # The frozen prompt is screened again where it leaves us, so work
                # saved before screening existed (or while it was off) never runs
                # unscreened. Verdicts are cached; a re-check is usually free.
                pending, refused = await self._screen_takes(movie, pending)
                changed = changed or refused
                results = await asyncio.gather(
                    *(self.submit(t) for t in pending), return_exceptions=True
                )
                for t, result in zip(pending, results):
                    if isinstance(result, BaseException):
                        t.error = "GPU submission interrupted; reconcile to retry"
                    else:
                        t.status = "running"
                        t.error = None
                changed = True
            running = [t for s in movie.shots for t in s.takes if t.status == "running"]
            if self.pool is not None:
                for t in running:
                    try:
                        if t.cancel_requested:
                            await self.pool.cancel(t.job_id)
                        record = await self.pool.get(t.job_id)
                        if not record:
                            continue
                        status = str(record.status)
                        if status == "completed":
                            if self.media is None:
                                raise MovieError(
                                    "FFmpeg processing is unavailable", 503
                                )
                            media = await self._scoped_media(movie, t.tenant_id, t.take_id)
                            t.final_frame_key = await media.final_frame(
                                t.asset_key
                            )
                            t.duration_seconds = await media.duration_for_key(
                                t.asset_key
                            )
                            if t.mode == "flight":
                                await self.measure_flight_take(t, record)
                            t.status = "succeeded"
                            if t.select_when_ready and not t.stale:
                                owning_shot = next(shot for shot in movie.shots if any(item.take_id == t.take_id for item in shot.takes))
                                if owning_shot.takes[-1].take_id == t.take_id:
                                    self.invalidate(movie, {v.shot_id for v in movie.shots if v.continuation_of == owning_shot.shot_id}, keep_waiting=True)
                                    owning_shot.selected_take_id = t.take_id
                            t.output_sha256 = getattr(record, "output_sha256", None)
                            t.output_byte_length = getattr(
                                record, "output_byte_length", None
                            )
                            t.error = None
                            changed = True
                        elif status in {"failed", "cancelled"}:
                            t.status = status
                            t.error = (
                                getattr(record, "failure_message", None)
                                or getattr(record, "error", None)
                                or "GPU take failed"
                            )
                            changed = True
                    except CpuPending:
                        continue
                    except ValueError as e:
                        t.status = "failed"
                        t.error = f"Invalid generated video: {str(e)[:200]}"
                        changed = True
                    except Exception:
                        logger.exception("Movie take result processing interrupted")
                        t.error = "Result processing interrupted; reconcile to retry"
                        changed = True
            # The completion audit is written after the fenced job commit. Revisit
            # settled takes so a late audit event can fill in provenance safely.
            audit_for_job = getattr(self.pool, "audit_for_job", None)
            if audit_for_job is not None:
                for shot in movie.shots:
                    for take in shot.takes:
                        if (
                            take.status == "succeeded"
                            and not take.completed_by_worker_id
                        ):
                            try:
                                events = await audit_for_job(take.job_id)
                            except Exception:
                                logger.exception(
                                    "Movie completion provenance is temporarily unavailable"
                                )
                                continue
                            completed = next(
                                (
                                    e
                                    for e in reversed(events)
                                    if str(e.event_type) == "job_completed"
                                ),
                                None,
                            )
                            if completed is not None:
                                take.completed_by_worker_id = completed.worker_id
                                take.completed_at = completed.created_at.isoformat()
                                changed = True
            composition_changed = await self.reconcile_compositions(movie)
            changed = changed or composition_changed
            if movie.assembly and movie.assembly.status == "pending":
                if self.media is None:
                    movie.assembly.status = "failed"
                    movie.assembly.error = "FFmpeg processing unavailable"
                else:
                    try:
                        sources = [
                            (self.take(movie, c.shot_id, c.take_id).asset_key, c)
                            for c in movie.assembly.clips
                        ]
                        media = await self._scoped_media(movie, movie.assembly.tenant_id, movie.assembly.request_id)
                        await media.assemble(
                            sources,
                            movie.assembly.asset_key,
                            soundtracks=movie.assembly.soundtracks,
                        )
                        movie.assembly.status = "succeeded"
                        movie.assembly.error = None
                        movie.assembly.joins = await self.measure_joins(movie)
                    except CpuPending:
                        pass
                    except Exception as e:
                        logger.exception("Movie assembly failed")
                        movie.assembly.status = "failed"
                        movie.assembly.error = f"Assembly failed: {str(e)[:200]}"
                changed = changed or movie.assembly.status != "pending"
            if not changed:
                return movie
            try:
                return await self.save(movie, movie.revision)
            except MovieError as e:
                if e.status != 409:
                    raise
        raise MovieError("Movie is busy; reconcile again", 409)

    async def measure_flight_take(self, t: Take, record: Any) -> None:
        """Record what executed and how closely the clip lands on its pins.
        Measurement is evidence for review; a failure never fails the take."""
        t.executor_preset = getattr(record, "preset", None)
        try:
            t.first_frame_diff, t.last_frame_diff = await flight_media.seam_diffs(
                self.assets,
                t.asset_key,
                t.final_frame_key,
                t.provider_frame_key or t.opening_asset_key,
                t.closing_frame_key,
            )
        except Exception:
            logger.exception("Scene-flight seam measurement unavailable")

    async def freeze_frame(self, source_key: str, frame_key: str, t: Take) -> None:
        # H3 requires multiples of 32 and otherwise stretches its input.
        # Freeze the matching crop once, retaining the approved original.
        if await self.assets.describe(frame_key) is not None:
            return
        content = await self.assets.read_limited(source_key, 20 * 1024 * 1024)
        with Image.open(BytesIO(content)) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError("Opening image exceeds 25 megapixels")
            frame = ImageOps.fit(
                source.convert("RGB"),
                (t.provider_width, t.provider_height),
                Image.Resampling.LANCZOS,
            )
            output = BytesIO()
            frame.save(output, "PNG")
        await self.assets.write_once(frame_key, output.getvalue(), "image/png")

    @staticmethod
    def join_times(clips: list[Clip], fps: float = 24.0) -> list[float]:
        """Where each cut lands in the assembled movie, dissolves included."""
        times = []
        t = 0.0
        for clip, following in pairwise(clips):
            length = clip.out_seconds - clip.in_seconds
            overlap = (
                math.floor(
                    min(
                        clip.transition_seconds,
                        length / 2,
                        (following.out_seconds - following.in_seconds) / 2,
                    )
                    * fps
                )
                / fps
            )
            t += length - overlap
            times.append(round(t, 4))
        return times

    async def measure_joins(self, movie: Movie) -> list[Join]:
        """Measure every cut of the assembled movie; a join over the limit is
        reported, never a failure. Dissolves are measured at their midpoint.
        Measurement runs on the service, so public accounts go unmeasured."""
        assembly = movie.assembly
        assert assembly is not None
        if await self.account_policy.is_public(movie.owner_id):
            return []
        report = getattr(self.media, "join_report", None)
        times = self.join_times(assembly.clips)
        if report is None or not times:
            return []
        try:
            diffs = await report(assembly.asset_key, times)
        except Exception:
            logger.exception("Movie join measurement unavailable")
            return []
        return [
            Join(index=i, at_seconds=at, diff=diff, limit=JOIN_LIMIT)
            for i, (at, diff) in enumerate(zip(times, diffs, strict=True))
        ]

    async def _screen_takes(self, movie: Movie, takes: list[Take]) -> tuple[list[Take], bool]:
        """The takes that may be submitted; a refused take fails with the reason,
        and one that cannot be screened right now waits for the next reconcile."""
        allowed: list[Take] = []
        refused_any = False
        for t in takes:
            try:
                await self.prompt_screening.require_allowed(movie.owner_id, "movie", [t.prompt])
            except PromptRefused as refused:
                t.status = "failed"
                t.error = str(refused)
                refused_any = True
                continue
            except PromptScreenUnavailable:
                t.error = "Prompt screening is unavailable; reconcile to retry"
                continue
            allowed.append(t)
        return allowed, refused_any

    async def submit(self, t: Take) -> Any:
        frame_key = t.opening_asset_key
        if t.provider_frame_key is not None:
            frame_key = t.provider_frame_key
            await self.freeze_frame(t.opening_asset_key, frame_key, t)
        seed = int(sha256(t.take_id.encode()).hexdigest()[:8], 16)
        if t.mode == "flight":
            assert t.closing_asset_key and t.closing_frame_key and t.length_frames
            await self.freeze_frame(t.closing_asset_key, t.closing_frame_key, t)
            images = {"first_frame": frame_key, "last_frame": t.closing_frame_key}
            capability = t.capability_id or flight_media.CAPABILITY_ID
            payload = {
                "prompt": t.prompt,
                "width": t.provider_width,
                "height": t.provider_height,
                "length": t.length_frames,
                "steps": t.steps or flight_media.STEPS,
                "seed": seed if t.seed is None else t.seed,
                "fps": t.provider_fps,
                "images": images,
            }
            keys = tuple(dict.fromkeys(images.values()))
        elif t.mode == "dialogue":
            assert t.plate_asset_key and t.plate_frame_key and t.length_frames
            await self.freeze_frame(t.plate_asset_key, t.plate_frame_key, t)
            images = {"opening_frame": frame_key, "end_plate": t.plate_frame_key}
            capability = dialogue.DIALOGUE_CAPABILITY
            payload = {
                "prompt": t.prompt,
                "width": t.provider_width,
                "height": t.provider_height,
                "length": t.length_frames,
                "steps": dialogue.STEPS,
                "seed": seed,
                "fps": dialogue.FPS,
                "images": images,
                "audios": dict(t.voice_asset_keys),
            }
            keys = tuple(
                dict.fromkeys([*images.values(), *t.voice_asset_keys.values()])
            )
        else:
            capability = "video.minimax_h3.text_to_video.v1"
            payload = {
                "prompt": t.prompt,
                "width": t.provider_width,
                "height": t.provider_height,
                "length": t.length_frames or max(17, min(161, round(t.duration_seconds * 16))),
                "steps": 20,
                "seed": seed,
                "fps": t.provider_fps,
                "images": {"first_frame": frame_key},
            }
            keys = (frame_key,)
        return await self.pool.submit(
            JobSubmission(
                job_id=t.job_id,
                idempotency_key=f"movie-take:{t.take_id}",
                capability_id=capability,
                input_keys=keys,
                output_key=t.asset_key,
                payload=payload,
                tenant_id=t.tenant_id,
                requirements=JobRequirements(min_vram_mb=24_000),
            )
        )

    async def select(
        self, id: str, owner: str, expected: int, sid: str, tid: str
    ) -> Movie:
        movie = await self.current(id, owner, expected)
        s = self.shot(movie, sid)
        t = self.take(movie, sid, tid)
        if t.status != "succeeded" or t.stale:
            raise MovieError("Select a successful current take")
        if s.selected_take_id != tid:
            self.invalidate(
                movie,
                {v.shot_id for v in movie.shots if v.continuation_of == sid},
                keep_waiting=True,
            )
            s.selected_take_id = tid
        return await self.save(movie, expected)

    async def cut(
        self, id: str, owner: str, expected: int, clips: list[Clip | dict[str, Any]]
    ) -> Movie:
        movie = await self.current(id, owner, expected)
        clips = [Clip.model_validate(c) for c in clips]
        if not clips or len(clips) > 120:
            raise MovieError("Cut requires 1 to 120 clips")
        for c in clips:
            t = self.take(movie, c.shot_id, c.take_id)
            if t.status != "succeeded" or t.stale:
                raise MovieError("Cut must use successful current takes")
            if c.in_seconds >= c.out_seconds or c.out_seconds > t.duration_seconds:
                raise MovieError("Trim is outside take duration")
        if movie.cut != clips:
            movie.assembly = None
        movie.cut = clips
        return await self.save(movie, expected)

    async def assemble(
        self, id: str, owner: str, expected: int, request_id: str, *, advance_build: bool = True
    ) -> Movie:
        movie = await self.get(id, owner)
        if movie.assembly and movie.assembly.request_id == request_id:
            return await self.reconcile(id, owner, advance_build=advance_build)
        movie = await self.current(id, owner, expected)
        if not movie.cut:
            raise MovieError("Save a cut before assembly")
        for c in movie.cut:
            if self.take(movie, c.shot_id, c.take_id).stale:
                raise MovieError("Cut has stale takes")
        fingerprint = digest(
            {
                "clips": [c.model_dump() for c in movie.cut],
                "soundtracks": [t.model_dump() for t in movie.soundtracks],
            }
        )
        movie.assembly = Assembly(
            tenant_id=await self.resolve_tenant(movie),
            request_id=request_id,
            asset_key=f"outputs/movies/{id}/cuts/{fingerprint}-{ASSEMBLY_RENDER}.mp4",
            cut_digest=fingerprint,
            clips=movie.cut,
            soundtracks=movie.soundtracks,
        )
        await self.save(movie, expected)
        return await self.reconcile(id, owner, advance_build=advance_build)

    async def release(self, id: str, owner: str, expected: int, name: str) -> Movie:
        movie = await self.current(id, owner, expected)
        if not movie.assembly or movie.assembly.status != "succeeded":
            raise MovieError("Assemble the current cut before release")
        current_digest = digest(
            {
                "clips": [c.model_dump() for c in movie.cut],
                "soundtracks": [t.model_dump() for t in movie.soundtracks],
            }
        )
        if movie.assembly.cut_digest != current_digest or any(
            self.take(movie, c.shot_id, c.take_id).stale for c in movie.cut
        ):
            raise MovieError("Assemble the current cut before release")
        if any(r.name == name for r in movie.releases):
            raise MovieError("Release name already exists", 409)
        movie.releases.append(
            Release(
                release_id=str(uuid4()),
                name=name,
                asset_key=movie.assembly.asset_key,
                cut_digest=movie.assembly.cut_digest,
                source_revision=expected,
            )
        )
        return await self.save(movie, expected)

    async def delete_movie(self, id: str, owner: str, confirm_name: str) -> None:
        """Permanently delete one of the owner's movies (or scene flights). The
        caller repeats its name, so a stray request cannot destroy work."""
        movie = await self.get(id, owner)
        if confirm_name != movie.name:
            raise MovieError(
                "Type the exact name to delete it", 422, code="deletion_not_confirmed"
            )
        await self.purge(movie)

    async def purge(self, movie: Movie) -> None:
        """Cancel its GPU work, delete every stored file (every version), then
        its snapshots and storage ledger. Files go first, so a failure part-way
        leaves a movie that can simply be deleted again."""
        if self.pool is not None:
            jobs = [
                t.job_id for s in movie.shots for t in s.takes
                if t.status in {"submitting", "running"}
            ]
            jobs += [
                intent["job_id"] for intent in movie.compositions.values()
                if intent.get("status") == "pending" and intent.get("job_id")
            ]
            for job_id in jobs:
                with suppress(Exception):
                    await self.pool.cancel(job_id)
        for prefix in (f"uploads/movies/{movie.movie_id}/", f"outputs/movies/{movie.movie_id}/"):
            await self.assets.delete_prefix(prefix)
        await self.store.delete(movie.movie_id)
        if self.retention is not None:
            # After the movie is gone, as for projects: never fail the delete.
            try:
                await self.retention.forget(MOVIE, movie.movie_id)
            except Exception:
                logger.warning("retention.forget_failed", exc_info=True)

    async def erase_owner(self, owner: str) -> None:
        """Delete every movie and scene flight an owner has (account deletion)."""
        for movie in await self.store.list(owner):
            await self.purge(movie)

    async def release_file(self, id: str, owner: str, release_id: str) -> tuple[str, str]:
        """The stored key and download name of one of the owner's releases."""
        movie = await self.get(id, owner)
        release = next((r for r in movie.releases if r.release_id == release_id), None)
        if release is None:
            raise MovieError("Release not found", 404)
        return release.asset_key, f"movie-{UUID(release.release_id)}.mp4"

    async def authorize_asset(self, id: str, owner: str, key: str) -> None:
        movie = await self.get(id, owner)
        allowed = set(movie.upload_keys)
        for s in movie.shots:
            allowed.add(s.opening_asset_key)
            allowed.add(s.closing_asset_key)
            for t in s.takes:
                allowed.update([t.asset_key, t.final_frame_key, t.provider_frame_key, t.closing_frame_key])
        if movie.assembly:
            allowed.add(movie.assembly.asset_key)
        for c in movie.characters:
            allowed.update([c.voice_asset_key, c.portrait_asset_key])
        allowed.update(
            intent["output_key"]
            for intent in movie.compositions.values()
            if intent["status"] == "succeeded"
        )
        allowed.update(t.asset_key for t in movie.soundtracks)
        allowed.update(r.asset_key for r in movie.releases)
        if key not in allowed:
            raise MovieError("Asset not found", 404)

    async def cancel(
        self, id: str, owner: str, expected: int, sid: str, tid: str
    ) -> Movie:
        movie = await self.current(id, owner, expected)
        t = self.take(movie, sid, tid)
        if t.status in {"succeeded", "failed", "cancelled"}:
            return movie
        t.cancel_requested = True
        if t.status == "waiting":
            t.status = "cancelled"
        await self.save(movie, expected)
        return await self.reconcile(id, owner)

    async def _screen_new_intents(self, movie: Movie, expected: int) -> None:
        """Screen the prompts of the image and audition jobs this save creates."""
        await self.prompt_screening.require_allowed(movie.owner_id, "movie", [
            str(intent.get("prompt") or "") for intent in movie.compositions.values()
            if intent.get("created_revision") == expected + 1 and intent.get("status") == "pending"
        ])

    async def compose(
        self,
        id: str,
        owner: str,
        expected: int,
        sid: str,
        request_id: str,
        reference_asset_keys: list[str],
    ) -> Movie:
        movie = await self.get(id, owner)
        if request_id in movie.compositions:
            if (
                movie.compositions[request_id]["shot_id"] != sid
                or movie.compositions[request_id].get("original_references", [])
                != reference_asset_keys
            ):
                raise MovieError("Composition request ID already used", 409)
            return await self.reconcile(id, owner)
        if self.composition is None and self.pool is None:
            raise MovieError(
                "Opening composition provider unavailable; upload an opening image", 503
            )
        movie = await self.current(id, owner, expected)
        s = self.shot(movie, sid)
        if any(
            c["shot_id"] == sid and c["status"] == "pending"
            for c in movie.compositions.values()
        ):
            raise MovieError(
                "A composition for this shot is pending; reconcile it first", 409
            )
        if self.work_guard is not None:
            await self.work_guard(owner, 1)
        if await self.account_policy.is_public(owner):
            # The HTTP composition provider is shared compute; refuse before
            # freezing any references.
            await self.resolve_tenant(movie, shared_only=self.composition is not None)
        if not s.opening_prompt.strip():
            raise MovieError("Write an opening composition prompt first")
        if len(reference_asset_keys) > 4:
            raise MovieError("Use at most four references")
        if (
            self.composition is None
            and len(f"{movie.style}\n{s.opening_prompt}".strip()) > 2000
        ):
            raise MovieError(
                "Combined style and opening prompt must not exceed 2000 characters"
            )
        # Freeze uploaded references before passing them to a provider.
        if self.composition is None and len(reference_asset_keys) > 3:
            raise MovieError(
                "Network opening generation supports at most three references"
            )
        references = []
        for key in reference_asset_keys:
            if key not in movie.upload_keys:
                raise MovieError("Reference is not granted to this movie", 403)
            content = await self.assets.read_limited(key, 20 * 1024 * 1024)
            try:
                with Image.open(BytesIO(content)) as im:
                    if im.width * im.height > 25_000_000:
                        raise ValueError("Image too large")
                    out = BytesIO()
                    im.convert("RGB").save(out, "PNG")
                    content = out.getvalue()
            except Exception as e:
                raise MovieError("Invalid reference image") from e
            immutable = (
                f"outputs/movies/{id}/references/{sha256(content).hexdigest()}.png"
            )
            await self.assets.write_once(immutable, content, "image/png")
            references.append(immutable)
        subject = s.opening_asset_key
        if not subject:
            out = BytesIO()
            Image.new("RGB", (768, 432), (240, 240, 240)).save(out, "PNG")
            subject = f"outputs/movies/{id}/composition-canvas.png"
            await self.assets.write_once(subject, out.getvalue(), "image/png")
        key = f"outputs/movies/{id}/compositions/{uuid4()}.png"
        tenant_id = await self.resolve_tenant(movie)
        if self.composition is None:
            await self.require_machines(tenant_id, SUBJECTS)
        movie.compositions[request_id] = {
            "created_revision": expected + 1,
            "shot_id": sid,
            "status": "pending",
            "job_id": str(uuid4()),
            "tenant_id": tenant_id,
            "provider": "http" if self.composition is not None else "pool",
            "output_key": key,
            "subject_key": subject,
            "reference_keys": references,
            "prompt": f"{movie.style}\n{s.opening_prompt}".strip(),
            "opening_prompt": s.opening_prompt,
            "style": movie.style,
            "original_opening_key": s.opening_asset_key,
            "original_references": reference_asset_keys,
            "error": None,
        }
        await self._screen_new_intents(movie, expected)
        await self.save(movie, expected)
        return await self.reconcile(id, owner)

    PORTRAIT_DIRECTION = (
        "Character reference sheet: {appearance}. Front three-quarter view from the "
        "chest up, neutral expression, even soft light, plain mid-grey background, "
        "no text."
    )
    STORYBOARD_DIRECTION = (
        "One single cinematic film frame, 16:9 widescreen, one continuous scene "
        "filling the whole image: {opening}. Exactly {count} {people} in frame, "
        "each a different person, all clearly visible: {cast}. Each keeps the face, "
        "hair, skin, build and clothing of their own reference image; nobody else "
        "appears. Photographic, consistent light and palette. Strictly no split "
        "screen, no panels, no collage, no grid, no borders, no dividing lines, no "
        "text, no captions."
    )

    async def portrait(
        self, id: str, owner: str, expected: int, cid: str, request_id: str
    ) -> Movie:
        """Compose a character's reference sheet from their appearance text."""
        movie = await self.get(id, owner)
        if request_id in movie.compositions:
            return await self.reconcile(id, owner)
        if self.pool is None:
            raise MovieError("Network GPU pool is unavailable", 503)
        movie = await self.current(id, owner, expected)
        character = next((c for c in movie.characters if c.character_id == cid), None)
        if character is None:
            raise MovieError("Character not found", 404)
        if not character.appearance.strip():
            raise MovieError(f"Describe {character.name}'s appearance first")
        if self.work_guard is not None:
            await self.work_guard(owner, 1)
        tenant_id = await self.resolve_tenant(movie)
        await self.require_machines(tenant_id, SUBJECTS)
        movie.compositions[request_id] = {
            "kind": "portrait",
            "created_revision": expected + 1,
            "shot_id": None,
            "character_id": cid,
            "status": "pending",
            "job_id": str(uuid4()),
            "tenant_id": tenant_id,
            "provider": "pool",
            "output_key": f"outputs/movies/{id}/portraits/{uuid4()}.png",
            "subject_key": None,
            "reference_keys": [],
            "prompt": f"{movie.style}\n"
            + self.PORTRAIT_DIRECTION.format(appearance=character.appearance.strip()),
            "appearance": character.appearance,
            "style": movie.style,
            "error": None,
        }
        await self._screen_new_intents(movie, expected)
        await self.save(movie, expected)
        return await self.reconcile(id, owner)

    # One solo line with room around it. H3 renders picture and speech on its
    # native 24 fps clock, so the take is sized on that clock and its grid,
    # as motion takes are: nine seconds is 216 frames, 226 on the grid.
    AUDITION_SECONDS = 9.0
    # Auditions submitted before the native clock carried no recipe; a retry
    # must resubmit exactly what they were first submitted with.
    LEGACY_AUDITION_FPS = 16
    LEGACY_AUDITION_LENGTH = 145

    AUDITION_DIRECTION = (
        "subject_definitions:\n<Subject 1> is {name}, {appearance}.\n\n"
        "summary:\n<Subject 1> (S1) speaks one line to camera in a quiet room.\n\n"
        "detailed_description:\nThe target video is {style}. [Shot 1] A medium "
        "close-up of <Subject 1> facing the camera in even, soft light, still "
        "except for natural speech. After a brief beat of quiet, <Subject 1> (S1), "
        "in a {voice}, says: <d>[English] {line}</d> Exactly as the voice stops, "
        "<Subject 1>'s lips close. No other words, narration or singing occur; "
        "the shot holds on the quiet final moment.\n\n"
        "overall_soundscape:\nQuiet room tone only.\n\nnon_diegetic_music:\nN/A"
    )

    async def audition(
        self, id: str, owner: str, expected: int, cid: str, request_id: str
    ) -> Movie:
        """Cast a voice from its description: one short solo take whose audio,
        once extracted and checked against the reference limits, becomes the
        character's voice reference. The same model that will speak the lines
        performs the audition, so nothing is dubbed."""
        movie = await self.get(id, owner)
        if request_id in movie.compositions:
            return await self.reconcile(id, owner)
        if self.pool is None:
            raise MovieError("Network GPU pool is unavailable", 503)
        # The take's audio is extracted and probed on the service.
        if await self._service_media(movie.owner_id, "Voice auditions") is None:
            raise MovieError("Audio processing is unavailable.", 503)
        movie = await self.current(id, owner, expected)
        character = next((c for c in movie.characters if c.character_id == cid), None)
        if character is None:
            raise MovieError("Character not found", 404)
        if not character.voice.strip():
            raise MovieError(f"Describe {character.name}'s voice first")
        if self.work_guard is not None:
            await self.work_guard(owner, 1)
        tenant_id = await self.resolve_tenant(movie, h3=True)
        await self.require_machines(tenant_id, MOVIE_TAKES)
        job_id = str(uuid4())
        movie.compositions[request_id] = {
            "kind": "audition",
            "created_revision": expected + 1,
            "shot_id": None,
            "character_id": cid,
            "status": "pending",
            "job_id": job_id,
            # The audition is performed by the same MiniMax H3 model that speaks.
            "tenant_id": tenant_id,
            "provider": "pool",
            "output_key": f"outputs/movies/{id}/auditions/{job_id}.mp4",
            "portrait_key": character.portrait_asset_key,
            "prompt": self.AUDITION_DIRECTION.format(
                name=character.name,
                appearance=character.appearance.strip() or "a person",
                style=movie.style or "cinematic realism",
                voice=character.voice.strip(),
                line=character.audition_line.strip(),
            ),
            "voice": character.voice,
            # The provider recipe is part of the job's identity: persist it so
            # an idempotent resubmission never changes it.
            "fps": dialogue.FPS,
            "length": dialogue.motion_frames(self.AUDITION_SECONDS),
            "error": None,
        }
        await self._screen_new_intents(movie, expected)
        await self.save(movie, expected)
        return await self.reconcile(id, owner)

    async def reconcile_audition(self, movie: Movie, intent: dict[str, Any]) -> bool:
        """Submit (idempotently) and, once the take lands, freeze its audio as
        the character's voice reference."""
        assert self.pool is not None and self.media is not None
        portrait = intent.get("portrait_key")
        frame_key = None
        if portrait:
            frame_key = intent["output_key"].removesuffix(".mp4") + ".first-768x448.png"
            take = Take(
                take_id=intent["job_id"],
                request_id="audition",
                job_id=intent["job_id"],
                asset_key=intent["output_key"],
                prompt="",
                duration_seconds=0,
                provider_height=448,
            )
            await self.freeze_frame(portrait, frame_key, take)
        payload: dict[str, Any] = {
            "prompt": intent["prompt"],
            "width": 768,
            "height": 448,
            "length": intent.get("length", self.LEGACY_AUDITION_LENGTH),
            "steps": 20,
            "seed": int(sha256(intent["job_id"].encode()).hexdigest()[:8], 16),
            "fps": intent.get("fps", self.LEGACY_AUDITION_FPS),
            "images": {"first_frame": frame_key} if frame_key else {},
        }
        await self.pool.submit(
            JobSubmission(
                job_id=intent["job_id"],
                idempotency_key=f"movie-audition:{intent['job_id']}",
                capability_id="video.minimax_h3.text_to_video.v1",
                input_keys=(frame_key,) if frame_key else (),
                output_key=intent["output_key"],
                payload=payload,
                tenant_id=intent.get("tenant_id"),
                requirements=JobRequirements(min_vram_mb=24_000),
            )
        )
        record = await self.pool.get(intent["job_id"])
        if record is None or str(record.status) not in {"completed", "failed", "cancelled"}:
            return False
        if str(record.status) != "completed":
            raise TerminalCompositionProviderError("Audition generation failed")
        content = await self.assets.read_limited(intent["output_key"], 256 * 1024 * 1024)
        wav = await self.media.extract_audio(content)
        seconds = await self.media.audio_duration(wav)
        if not dialogue.VOICE_MIN_SECONDS <= seconds <= dialogue.VOICE_MAX_SECONDS:
            raise ValueError(f"audition audio is {seconds:.1f}s")
        key = f"outputs/movies/{movie.movie_id}/voices/{sha256(wav).hexdigest()}.wav"
        await self.assets.write_once(key, wav, "audio/wav")
        intent["voice_asset_key"] = key
        intent["voice_seconds"] = round(seconds, 3)
        return True

    async def storyboard(
        self, id: str, owner: str, expected: int, request_id: str
    ) -> Movie:
        """Compose one frame per shot in story order, each drawn from the cast's
        portraits and the previous shot's frame, so the scene evolves while faces,
        costumes and the place stay put. Frames become the shots' plates."""
        movie = await self.get(id, owner)
        if any(k.startswith(f"{request_id}:") for k in movie.compositions):
            return await self.reconcile(id, owner)
        if self.pool is None:
            raise MovieError("Network GPU pool is unavailable", 503)
        movie = await self.current(id, owner, expected)
        if not movie.shots:
            raise MovieError("Add shots before storyboarding")
        if any(c["status"] == "pending" for c in movie.compositions.values()):
            raise MovieError("Wait for pending compositions before storyboarding", 409)
        by_id = {c.character_id: c for c in movie.characters}
        for s in movie.shots:
            if not s.opening_prompt.strip():
                raise MovieError(f"Write an opening frame direction for {s.name}")
            for cid in {line.character_id for line in s.dialogue}:
                if not by_id[cid].portrait_asset_key:
                    raise MovieError(f"Compose a portrait for {by_id[cid].name} first")
        if self.work_guard is not None:
            # Frames are composed one after another, never concurrently.
            await self.work_guard(owner, 1)
        tenant = await self.resolve_tenant(movie)
        # One plate per chain: a conversation held in one setup keeps one
        # frame for every shot, composed once from everyone who speaks in it.
        # Composing a frame per shot produced near-copies and collages that
        # the takes then drifted towards.
        by_id = {c.character_id: c for c in movie.characters}
        head_of: dict[str, str] = {}
        chain_cast: dict[str, list[str]] = {}
        for s in movie.shots:
            head = head_of.get(s.continuation_of or "", s.shot_id)
            head_of[s.shot_id] = head
            members = chain_cast.setdefault(head, [])
            for line in s.dialogue:
                if line.character_id not in members:
                    members.append(line.character_id)
        for s in movie.shots:
            key = f"{request_id}:{s.shot_id}"
            head = head_of[s.shot_id]
            if head != s.shot_id:
                movie.compositions[key] = {
                    "kind": "storyboard",
                    "created_revision": expected + 1,
                    "shot_id": s.shot_id,
                    "status": "pending",
                    "job_id": str(uuid4()),
                    "tenant_id": tenant,
                    "provider": "reuse",
                    "reuse_of": f"{request_id}:{head}",
                    "output_key": None,
                    "subject_key": None,
                    "reference_keys": [],
                    "prompt": "",
                    "opening_prompt": s.opening_prompt,
                    "style": movie.style,
                    "original_opening_key": s.opening_asset_key,
                    "original_references": [],
                    "error": None,
                }
                continue
            cast = chain_cast[head][:3]
            # Bind each person to their reference by index and restate their
            # look: a bare list of names let the model merge two references
            # into one figure.
            names = (
                "; ".join(
                    f"{by_id[cid].name} (reference image {i}"
                    + (f": {by_id[cid].appearance.strip()}" if by_id[cid].appearance.strip() else "")
                    + ")"
                    for i, cid in enumerate(cast, 1)
                )
                or "the people described"
            )
            movie.compositions[key] = {
                "kind": "storyboard",
                "created_revision": expected + 1,
                "shot_id": s.shot_id,
                "status": "pending",
                "job_id": str(uuid4()),
                "tenant_id": tenant,
                "provider": "pool",
                "output_key": f"outputs/movies/{id}/storyboard/{uuid4()}.png",
                "subject_key": None,
                "reference_keys": [],
                "reference_sources": {"portraits": cast, "after": None},
                "after_request_id": None,
                "prompt": f"{movie.style}\n"
                + self.STORYBOARD_DIRECTION.format(
                    opening=s.opening_prompt.strip(),
                    cast=names,
                    count=len(cast) or "the",
                    people="person" if len(cast) == 1 else "people",
                ),
                "opening_prompt": s.opening_prompt,
                "style": movie.style,
                "original_opening_key": s.opening_asset_key,
                "original_references": [],
                "error": None,
            }
        await self._screen_new_intents(movie, expected)
        await self.save(movie, expected)
        return await self.reconcile(id, owner)

    def resolve_references(self, movie: Movie, intent: dict[str, Any]) -> bool:
        """Fill a storyboard frame's references once its predecessor has landed."""
        sources = intent.get("reference_sources")
        if not sources or intent["reference_keys"]:
            return True
        after = sources.get("after")
        previous_key = None
        if after is not None:
            predecessor = movie.compositions.get(after)
            if predecessor is None or predecessor["status"] != "succeeded":
                return False
            previous_key = predecessor["output_key"]
        portraits = {c.character_id: c.portrait_asset_key for c in movie.characters}
        keys = [portraits[cid] for cid in sources.get("portraits", []) if portraits.get(cid)]
        if previous_key:
            keys.append(previous_key)
        intent["reference_keys"] = keys[:3]
        return True

    async def reconcile_compositions(self, movie: Movie) -> bool:
        from magic_video_editor.contracts import CompositionGenerationRequest

        changed = False
        for request_id, intent in movie.compositions.items():
            if (
                intent["status"] == "superseded"
                and intent.get("provider") == "pool"
                and not intent.get("settled", False)
            ):
                if self.pool is not None:
                    await self.pool.cancel(intent["job_id"])
                    record = await self.pool.get(intent["job_id"])
                    if record is None or str(record.status) in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        intent["settled"] = True
                        changed = True
                continue
            if intent["status"] != "pending":
                continue
            # Screened where it is dispatched, like takes; see `_screen_takes`.
            try:
                await self.prompt_screening.require_allowed(
                    movie.owner_id, "movie", [str(intent.get("prompt") or "")]
                )
            except PromptRefused as refused:
                intent["status"] = "failed"
                intent["error"] = str(refused)
                changed = True
                continue
            except PromptScreenUnavailable:
                continue
            try:
                if intent.get("provider") == "reuse":
                    source = movie.compositions.get(intent["reuse_of"])
                    if source is None or source["status"] == "pending":
                        continue
                    if source["status"] != "succeeded":
                        intent["status"] = "failed"
                        intent["error"] = "The frame this shot shares was not composed"
                        changed = True
                        continue
                    intent["output_key"] = source["output_key"]
                    intent["status"] = "succeeded"
                    s = next((s for s in movie.shots if s.shot_id == intent["shot_id"]), None)
                    if s and s.opening_prompt == intent["opening_prompt"]:
                        s.opening_asset_key = intent["output_key"]
                        s.opening_approved = False
                        self.invalidate(movie, {s.shot_id}, keep_compositions=True)
                    changed = True
                    continue
                if intent.get("kind") == "audition":
                    if await self.account_policy.is_public(movie.owner_id):
                        # Started before the account became public; its audio
                        # would be processed on the service.
                        intent["status"] = "failed"
                        intent["error"] = "Voice auditions are not available for public accounts yet."
                        changed = True
                        continue
                    if not await self.reconcile_audition(movie, intent):
                        continue
                    intent["status"] = "succeeded"
                    intent["error"] = None
                    character = next(
                        (c for c in movie.characters if c.character_id == intent["character_id"]),
                        None,
                    )
                    if character and character.voice == intent["voice"]:
                        character.voice_asset_key = intent["voice_asset_key"]
                        character.voice_seconds = intent["voice_seconds"]
                        self.invalidate(
                            movie,
                            {
                                s.shot_id
                                for s in movie.shots
                                if any(line.character_id == character.character_id for line in s.dialogue)
                            },
                        )
                    changed = True
                    continue
                existing = await self.assets.describe(intent["output_key"])
                if not existing and intent.get("provider") == "pool":
                    if not self.resolve_references(movie, intent):
                        continue
                    if not await self.reconcile_pool_composition(intent):
                        continue
                elif not existing:
                    result = await self.composition.compose_frame(
                        CompositionGenerationRequest(
                            subject_key=intent["subject_key"],
                            reference_keys=tuple(intent["reference_keys"]),
                            output_key=intent["output_key"],
                            prompt=intent["prompt"],
                            model="nanobanana_2_lite",
                            idempotency_key=f"movie-composition:{movie.movie_id}:{request_id}",
                        )
                    )
                    if result != intent["output_key"]:
                        raise ValueError("Provider returned an unexpected output key")
                content = await self.assets.read_limited(
                    intent["output_key"], 20 * 1024 * 1024
                )
                with Image.open(BytesIO(content)) as im:
                    if im.width * im.height > 25_000_000:
                        raise ValueError("Composition too large")
                    im.verify()
                intent["status"] = "succeeded"
                intent["error"] = None
                if intent.get("kind") == "portrait":
                    character = next(
                        (c for c in movie.characters if c.character_id == intent["character_id"]),
                        None,
                    )
                    if character and character.appearance == intent["appearance"]:
                        character.portrait_asset_key = intent["output_key"]
                        character.portrait_appearance = character.appearance
                    changed = True
                    continue
                s = next(
                    (s for s in movie.shots if s.shot_id == intent["shot_id"]), None
                )
                if (
                    s
                    and s.opening_prompt == intent["opening_prompt"]
                    and movie.style == intent["style"]
                    and s.opening_asset_key == intent["original_opening_key"]
                ):
                    s.opening_asset_key = intent["output_key"]
                    s.opening_approved = False
                    # A storyboard frame landing must not supersede the next
                    # frame in its own chain.
                    self.invalidate(
                        movie,
                        {s.shot_id},
                        keep_compositions=intent.get("kind") == "storyboard",
                    )
            except MachineCannotRun as refusal:
                intent["status"] = "failed"
                intent["error"] = refusal.message
            except TerminalCompositionProviderError:
                logger.exception(
                    "Movie opening generation failed: job_id=%s", intent.get("job_id")
                )
                intent["status"] = "failed"
                intent["error"] = (
                    "The GPU could not generate this opening; try a new composition"
                )
            except (ValueError, UnidentifiedImageError):
                logger.exception(
                    "Invalid movie opening result: job_id=%s", intent.get("job_id")
                )
                intent["status"] = "failed"
                intent["error"] = (
                    "The audition did not yield a usable voice clip; audition again"
                    if intent.get("kind") == "audition"
                    else "The composition provider returned an invalid result; try a new composition"
                )
            except Exception:
                logger.exception("Movie composition interrupted")
                intent["error"] = "Composition interrupted; reconcile to retry"
            changed = True
        return changed

    async def reconcile_pool_composition(self, intent: dict[str, Any]) -> bool:
        """One non-blocking pool check, with durable IDs and an approved wide crop."""
        if self.pool is None:
            return False
        raw_key = intent["output_key"].removesuffix(".png") + ".raw.png"
        references = tuple(intent["reference_keys"])
        payload = {
            "prompt": intent["prompt"],
            "steps": 20,
            "seed": int(sha256(intent["job_id"].encode()).hexdigest()[:8], 16),
        }
        if references:
            payload["images"] = {
                f"ref_{i + 1}": key for i, key in enumerate(references)
            }
        # A job already in the pool is checked as submitted: a machine that went
        # away since must not fail an opening that is running or already done.
        if await self.pool.get(intent["job_id"]) is None:
            # The 16 GB route, or the 12 GB one when that is all the owner's
            # machines serve; refused with the reason when they serve neither.
            capability_id, requirements = await subject_route(
                self.pool, intent.get("tenant_id"),
                house_tenants=self.house_tenants,
                revoked=revoked_by(self.enrolment),
            )
            await self.pool.submit(
                JobSubmission(
                    job_id=intent["job_id"],
                    idempotency_key=f"movie-opening:{intent['job_id']}",
                    capability_id=capability_id,
                    input_keys=references,
                    output_key=raw_key,
                    payload=payload,
                    tenant_id=intent.get("tenant_id"),
                    requirements=requirements,
                )
            )
        record = await self.pool.get(intent["job_id"])
        if record is None or str(record.status) not in {
            "completed",
            "failed",
            "cancelled",
        }:
            return False
        if str(record.status) != "completed":
            logger.warning(
                "Movie opening job terminal: job_id=%s status=%s failure_code=%s attempts=%s",
                intent["job_id"],
                str(record.status),
                getattr(record, "failure_code", None),
                getattr(record, "attempts", None),
            )
            raise TerminalCompositionProviderError("Opening generation failed")
        content = await self.assets.read_limited(raw_key, 20 * 1024 * 1024)
        with Image.open(BytesIO(content)) as source:
            if source.width * source.height > 25_000_000:
                raise ValueError("Composition too large")
            frame = ImageOps.fit(
                source.convert("RGB"), (768, 432), Image.Resampling.LANCZOS
            )
            output = BytesIO()
            frame.save(output, "PNG")
        await self.assets.write_once(
            intent["output_key"], output.getvalue(), "image/png"
        )
        return True

    async def import_clip(
        self, id: str, owner: str, expected: int, sid: str, key: str
    ) -> Movie:
        movie = await self.current(id, owner, expected)
        s = self.shot(movie, sid)
        if key not in movie.upload_keys:
            raise MovieError("Clip is not granted to this movie", 403)
        media = await self._service_media(movie.owner_id, "Clip import")
        if media is None:
            raise MovieError("FFmpeg processing unavailable", 503)
        content = await self.assets.read_limited(key, 256 * 1024 * 1024)
        try:
            duration = await media.duration(content)
        except ValueError as e:
            raise MovieError(str(e)) from e
        fingerprint = sha256(content).hexdigest()
        immutable = f"outputs/movies/{id}/imports/{fingerprint}.mp4"
        await self.assets.write_once(immutable, content, "video/mp4")
        frame = await media.final_frame(immutable)
        existing = next(
            (t for t in s.takes if t.request_id == f"import:{fingerprint}"), None
        )
        if existing:
            return movie
        tid = str(uuid4())
        s.takes.append(
            Take(
                take_id=tid,
                job_id=tid,
                request_id=f"import:{fingerprint}",
                status="succeeded",
                asset_key=immutable,
                final_frame_key=frame,
                prompt="Imported clip",
                duration_seconds=duration,
            )
        )
        return await self.save(movie, expected)

    async def accept_take(
        self, id: str, owner: str, expected: int, sid: str, tid: str
    ) -> Movie:
        """Record a human continuity review without changing generation provenance."""
        movie = await self.current(id, owner, expected)
        shot = self.shot(movie, sid)
        take = self.take(movie, sid, tid)
        if take.status != "succeeded":
            raise MovieError("Only a successful take can be accepted")
        reviewed_parent = None
        if shot.continuation_of:
            parent = self.shot(movie, shot.continuation_of)
            if not parent.selected_take_id:
                raise MovieError("Select the predecessor take before continuity review")
            selected = self.take(movie, parent.shot_id, parent.selected_take_id)
            if selected.status != "succeeded" or selected.stale:
                raise MovieError(
                    "The predecessor selection must be current and successful"
                )
            reviewed_parent = selected.take_id
        take.stale = False
        take.reviewed_for_predecessor_take_id = reviewed_parent
        take.reviewed_spec_digest = digest(
            {
                "style": movie.style,
                "shot": shot.model_dump(mode="json", include=set(ShotInput.model_fields)),
            }
        )
        movie.assembly = None
        return await self.save(movie, expected)

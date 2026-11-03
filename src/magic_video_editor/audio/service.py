"""Audio sets: gather candidates for each cue, review them, select, and release a pack.

Phase 1 of docs/design/audio-studio.md: candidates come from Freesound (licence
filtered, CC0 by default) or the owner's own uploads; nothing is generated. The
review, selection and release gates follow Under Fire's Voice Studio: a verdict is
bound to the file's hash, selection takes only kept files the set's licence policy
allows, and a release freezes the selection into a create-once pack.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

from magic_video_editor.audio import release as packs
from magic_video_editor.audio.freesound import (
    FreeSound,
    FreesoundClient,
    FreesoundRateLimited,
    FreesoundRefused,
    FreesoundUnavailable,
)
from magic_video_editor.audio.media import AudioMedia
from magic_video_editor.audio.models import (
    MAX_CANDIDATES_PER_CUE,
    MAX_CUES,
    AudioRelease,
    AudioSet,
    Candidate,
    Cue,
    CueInput,
    Licence,
    LicencePolicy,
    Review,
)
from magic_video_editor.audio.store import AudioStore, Conflict
from magic_video_editor.contracts import AssetStore, upload_size_headers
from magic_video_editor.studio.accounts import AccountPolicy, own_compute_required

logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_RELEASE_BYTES = 256 * 1024 * 1024
TOO_LARGE = "A release holds at most 256 MB, counting every copy and its manifest."
UPLOAD_TYPES = {"audio/mpeg", "audio/wav"}
UPLOADED_LICENCE = "LicenseRef-Owner-Upload"
# A replacement original must match the preview's length within this much.
ORIGINAL_TOLERANCE_SECONDS = 0.5
ORIGINAL_TOLERANCE_RATIO = 0.02


class AudioError(Exception):
    def __init__(
        self,
        message: str,
        status: int = 422,
        *,
        current_revision: int | None = None,
        code: str | None = None,
    ) -> None:
        self.status = status
        self.current_revision = current_revision
        self.code = code
        super().__init__(message)


def now() -> str:
    return datetime.now(UTC).isoformat()


def media_type_of(content: bytes) -> Literal["audio/mpeg", "audio/wav"] | None:
    if content[:4] in {b"RIFF", b"RF64", b"BW64"} and content[8:12] == b"WAVE":
        return "audio/wav"
    if content[:3] == b"ID3" or (len(content) > 1 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    return None


def licence_allowed(licence: Licence, policy: LicencePolicy) -> bool:
    if licence.source == "upload" or licence.spdx == "CC0-1.0":
        return True
    return policy == "cc0_cc_by" and licence.spdx in {"CC-BY-4.0", "CC-BY-3.0"}


class AudioService:
    def __init__(
        self,
        store: AudioStore,
        assets: AssetStore,
        *,
        freesound: FreesoundClient | None = None,
        media: AudioMedia | None = None,
        account_policy: AccountPolicy | None = None,
        work_guard: Callable[[str, int], Awaitable[None]] | None = None,
        movie_service: Any | None = None,
    ) -> None:
        self.store = store
        self.assets = assets
        self.freesound = freesound
        self.media = media
        self.account_policy = account_policy if account_policy is not None else AccountPolicy()
        self.work_guard = work_guard
        self.movie_service = movie_service
        # One release is zipped at a time, off the event loop, so memory stays bounded
        # and other requests keep being served while it compresses.
        self.pack_slots = asyncio.Semaphore(1)

    # -- the document --------------------------------------------------------

    async def capabilities(self, owner: str) -> dict[str, Any]:
        public = await self.account_policy.is_public(owner)
        return {
            "account_tier": (await self.account_policy.tier(owner)).value,
            "free_sounds_available": self.freesound is not None and self.freesound.available,
            "upload_available": True,
            # Public accounts never use the service's FFmpeg: their candidates keep
            # the original only, with no loudness numbers.
            "processing_available": self.media is not None and not public,
            "replace_original_available": self.media is not None and not public,
            "send_to_movie_available": self.movie_service is not None and not public,
            "generation_available": False,
            "licence_policies": ["cc0", "cc0_cc_by"],
            "max_cues": MAX_CUES,
            "max_candidates_per_cue": MAX_CANDIDATES_PER_CUE,
            "max_file_bytes": MAX_AUDIO_BYTES,
            "upload_types": sorted(UPLOAD_TYPES),
        }

    async def create(self, owner: str, name: str) -> AudioSet:
        return await self.store.create(AudioSet(set_id=str(uuid4()), owner_id=owner, name=name))

    async def get(self, id: str, owner: str) -> AudioSet:
        audio_set = await self.store.get(id)
        if audio_set is None or audio_set.owner_id != owner:
            raise AudioError("Audio set not found", 404)
        return audio_set

    async def list(self, owner: str) -> list[AudioSet]:
        return await self.store.list(owner)

    async def current(self, id: str, owner: str, expected: int) -> AudioSet:
        audio_set = await self.get(id, owner)
        if audio_set.revision != expected:
            raise self.conflict(audio_set.revision)
        return audio_set

    @staticmethod
    def conflict(current: int) -> AudioError:
        return AudioError(
            f"Revision conflict; current revision is {current}", 409, current_revision=current
        )

    async def save(self, audio_set: AudioSet, expected: int) -> AudioSet:
        try:
            saved = await self.store.append(audio_set, expected)
        except Conflict as e:
            raise self.conflict(e.revision) from e
        await self.account_assets(saved)
        return saved

    async def account_assets(self, audio_set: AudioSet) -> None:
        """Idempotent ownership ledger; retries never double-count stored audio."""
        try:
            keys = set(audio_set.upload_keys)
            for c in audio_set.candidates:
                keys.add(c.asset_key)
                if c.rendition_key:
                    keys.add(c.rendition_key)
                keys.update(c.provenance.get("replaced_keys", []))
            keys.update(r.asset_key for r in audio_set.releases)
            for key in keys - await self.store.accounted_keys(audio_set.set_id):
                descriptor = await self.assets.describe(key)
                if descriptor is not None:
                    await self.store.account_asset(audio_set.set_id, key, descriptor.size)
        except Exception:
            logger.exception("Audio asset accounting will retry on next saved revision")

    async def update(
        self,
        id: str,
        owner: str,
        expected: int,
        *,
        name: str,
        brief: str,
        licence_policy: LicencePolicy,
        cues: list[CueInput],
    ) -> AudioSet:
        """Replace the set's details and cues. A removed cue takes its candidates
        out of the document (their files stay until the set is deleted); a
        selection keeps only what the new policy and `keep` still allow."""
        audio_set = await self.current(id, owner, expected)
        if len(cues) > MAX_CUES:
            raise AudioError(f"Use at most {MAX_CUES} cues.")
        ids = [c.cue_id for c in cues]
        if len(ids) != len(set(ids)):
            raise AudioError("Each cue needs its own id.")
        previous = {c.cue_id: c for c in audio_set.cues}
        audio_set.name, audio_set.brief, audio_set.licence_policy = name, brief, licence_policy
        audio_set.candidates = [c for c in audio_set.candidates if c.cue_id in ids]
        by_id = {c.candidate_id: c for c in audio_set.candidates}
        audio_set.cues = []
        for cue in cues:
            selected = [
                cid for cid in (previous[cue.cue_id].selected if cue.cue_id in previous else [])
                if cid in by_id and self.selectable(by_id[cid], licence_policy)
            ][: cue.keep]
            audio_set.cues.append(Cue(**cue.model_dump(), selected=selected))
        return await self.save(audio_set, expected)

    @staticmethod
    def selectable(candidate: Candidate, policy: LicencePolicy) -> bool:
        review = candidate.review
        return (
            review is not None
            and review.outcome == "keep"
            and review.sha256 == candidate.sha256
            and licence_allowed(candidate.licence, policy)
        )

    def cue(self, audio_set: AudioSet, cue_id: str) -> Cue:
        cue = next((c for c in audio_set.cues if c.cue_id == cue_id), None)
        if cue is None:
            raise AudioError("Cue not found", 404)
        return cue

    def candidate(self, audio_set: AudioSet, candidate_id: str) -> Candidate:
        candidate = next((c for c in audio_set.candidates if c.candidate_id == candidate_id), None)
        if candidate is None:
            raise AudioError("Candidate not found", 404)
        return candidate

    # -- candidates ----------------------------------------------------------

    async def search(
        self, id: str, owner: str, query: str, min_seconds: float, max_seconds: float, page: int
    ) -> list[FreeSound]:
        audio_set = await self.get(id, owner)
        if self.freesound is None or not self.freesound.available:
            raise AudioError("Free-sound search is not configured.", 503)
        if min_seconds >= max_seconds:
            raise AudioError("The shortest length must be below the longest.")
        try:
            return await self.freesound.search(
                owner, query, audio_set.licence_policy,
                min_seconds=min_seconds, max_seconds=max_seconds, page=page,
            )
        except FreesoundRateLimited as e:
            raise AudioError(str(e), 429) from e
        except FreesoundUnavailable as e:
            raise AudioError(str(e), 502) from e

    async def _room_for(self, audio_set: AudioSet, cue_id: str) -> None:
        self.cue(audio_set, cue_id)
        if sum(c.cue_id == cue_id for c in audio_set.candidates) >= MAX_CANDIDATES_PER_CUE:
            raise AudioError(
                f"A cue holds at most {MAX_CANDIDATES_PER_CUE} candidates.", 409
            )
        if self.work_guard is not None:
            await self.work_guard(audio_set.owner_id, 0)

    async def _freeze(
        self, audio_set: AudioSet, content: bytes
    ) -> tuple[str, Literal["audio/mpeg", "audio/wav"], float | None, Any, str | None]:
        """Store the bytes create-once under their hash, and measure and render
        them where this account may use the service's FFmpeg."""
        media_type = media_type_of(content)
        if media_type is None:
            raise AudioError("Choose a WAV or MP3 file.")
        digest = sha256(content).hexdigest()
        ext = packs.EXTENSIONS[media_type]
        key = f"outputs/audio/{audio_set.set_id}/candidates/{digest}.{ext}"
        duration = measured = rendition_key = None
        if self.media is not None and not await self.account_policy.is_public(audio_set.owner_id):
            try:
                duration, measured, rendition = await self.media.process(content)
            except Exception as e:
                raise AudioError("Choose a valid WAV or MP3 file no longer than one hour.") from e
            # A rendition is kept only when it fits the same limit every later
            # read uses (release, movie cue); a long stereo WAV may not.
            if rendition is not None and len(rendition) <= MAX_AUDIO_BYTES:
                rendition_key = f"outputs/audio/{audio_set.set_id}/renditions/{digest}.wav"
                await self.assets.write_once(rendition_key, rendition, "audio/wav")
        await self.assets.write_once(key, content, media_type)
        return key, media_type, duration, measured, rendition_key

    async def import_free_sound(
        self, id: str, owner: str, expected: int, cue_id: str, source_id: str
    ) -> AudioSet:
        audio_set = await self.current(id, owner, expected)
        await self._room_for(audio_set, cue_id)
        if self.freesound is None or not self.freesound.available:
            raise AudioError("Free-sound search is not configured.", 503)
        if any(
            c.cue_id == cue_id and c.origin == "freesound" and c.licence.source_id == source_id
            for c in audio_set.candidates
        ):
            raise AudioError("That sound is already a candidate for this cue.", 409)
        try:
            sound = await self.freesound.sound(owner, source_id, audio_set.licence_policy)
            content = await self.freesound.download_preview(sound)
        except FreesoundRefused as e:
            raise AudioError(str(e)) from e
        except FreesoundRateLimited as e:
            raise AudioError(str(e), 429) from e
        except FreesoundUnavailable as e:
            raise AudioError(str(e), 502) from e
        key, media_type, duration, measured, rendition_key = await self._freeze(audio_set, content)
        audio_set.candidates.append(
            Candidate(
                candidate_id=str(uuid4()),
                cue_id=cue_id,
                origin="freesound",
                asset_key=key,
                sha256=key.rsplit("/", 1)[1].split(".")[0],
                byte_length=len(content),
                media_type=media_type,
                duration_seconds=duration if duration is not None else sound.duration_seconds,
                measured=measured,
                rendition_key=rendition_key,
                licence=Licence(
                    spdx=sound.spdx,
                    licence_url=sound.licence_url,
                    author=sound.author,
                    source="freesound",
                    source_id=sound.source_id,
                    source_url=sound.source_url,
                    retrieved_at=now(),
                    variant="preview",
                    attribution_required=sound.attribution_required,
                ),
                provenance={
                    "name": sound.name,
                    "tags": sound.tags,
                    "preview_url": sound.preview_url,
                    "reported_duration_seconds": sound.duration_seconds,
                },
                created_at=now(),
            )
        )
        return await self.save(audio_set, expected)

    async def upload(self, id: str, owner: str, content_type: str) -> dict[str, Any]:
        """A create-once upload grant, recorded on the set before the URL is issued."""
        if content_type not in UPLOAD_TYPES:
            raise AudioError("Upload a WAV or MP3 file.")
        if self.work_guard is not None:
            await self.work_guard(owner, 0)
        for _ in range(8):
            audio_set = await self.get(id, owner)
            key = f"uploads/audio/{id}/{uuid4()}"
            audio_set.upload_keys.append(key)
            try:
                audio_set = await self.save(audio_set, audio_set.revision)
                break
            except AudioError as e:
                if e.status != 409:
                    raise
        else:
            raise AudioError("Audio set is busy; retry", 409)
        public = await self.account_policy.is_public(owner)
        limit = MAX_AUDIO_BYTES if public else None
        return {
            "asset_key": key,
            "upload_url": await self.assets.create_upload_url(key, content_type, max_bytes=limit),
            "upload_headers": upload_size_headers(limit),
            "revision": audio_set.revision,
        }

    async def _granted(self, audio_set: AudioSet, asset_key: str) -> bytes:
        if asset_key not in audio_set.upload_keys:
            raise AudioError("Upload not granted to this audio set.", 403)
        return await self.assets.read_limited(asset_key, MAX_AUDIO_BYTES)

    async def add_upload(
        self, id: str, owner: str, expected: int, cue_id: str, asset_key: str, filename: str
    ) -> AudioSet:
        audio_set = await self.current(id, owner, expected)
        await self._room_for(audio_set, cue_id)
        content = await self._granted(audio_set, asset_key)
        key, media_type, duration, measured, rendition_key = await self._freeze(audio_set, content)
        digest = key.rsplit("/", 1)[1].split(".")[0]
        if any(c.cue_id == cue_id and c.sha256 == digest for c in audio_set.candidates):
            raise AudioError("That file is already a candidate for this cue.", 409)
        audio_set.candidates.append(
            Candidate(
                candidate_id=str(uuid4()),
                cue_id=cue_id,
                origin="upload",
                asset_key=key,
                sha256=digest,
                byte_length=len(content),
                media_type=media_type,
                duration_seconds=duration,
                measured=measured,
                rendition_key=rendition_key,
                licence=Licence(spdx=UPLOADED_LICENCE, source="upload", retrieved_at=now()),
                provenance={"filename": filename} if filename else {},
                created_at=now(),
            )
        )
        return await self.save(audio_set, expected)

    async def replace_original(
        self, id: str, owner: str, expected: int, candidate_id: str, asset_key: str
    ) -> AudioSet:
        """Swap a Freesound preview for the original the owner downloaded. It must
        be as long as the preview; its review starts again, as it is a new file."""
        audio_set = await self.current(id, owner, expected)
        candidate = self.candidate(audio_set, candidate_id)
        if candidate.origin != "freesound" or candidate.licence.variant != "preview":
            raise AudioError("Only a Freesound preview can be replaced by its original.", 409)
        if self.media is None:
            raise AudioError("Audio processing is unavailable.", 503)
        if await self.account_policy.is_public(owner):
            refusal = own_compute_required("Replacing a preview is not available for public accounts yet.")
            raise AudioError(refusal.message, 403, code=refusal.code)
        content = await self._granted(audio_set, asset_key)
        try:
            seconds = await self.media.duration(content)
        except Exception as e:
            raise AudioError("Choose a valid WAV or MP3 file.") from e
        expected_seconds = candidate.provenance.get("reported_duration_seconds") or candidate.duration_seconds or 0
        tolerance = max(ORIGINAL_TOLERANCE_SECONDS, expected_seconds * ORIGINAL_TOLERANCE_RATIO)
        if abs(seconds - expected_seconds) > tolerance:
            raise AudioError(
                f"The original should be about {expected_seconds:.1f}s long; this file is {seconds:.1f}s."
            )
        key, media_type, duration, measured, rendition_key = await self._freeze(audio_set, content)
        replaced = [candidate.asset_key, *([candidate.rendition_key] if candidate.rendition_key else [])]
        index = audio_set.candidates.index(candidate)
        audio_set.candidates[index] = candidate.model_copy(
            update={
                "asset_key": key,
                "sha256": key.rsplit("/", 1)[1].split(".")[0],
                "byte_length": len(content),
                "media_type": media_type,
                "duration_seconds": duration,
                "measured": measured,
                "rendition_key": rendition_key,
                "licence": candidate.licence.model_copy(update={"variant": "original"}),
                "provenance": {
                    **candidate.provenance,
                    "preview_sha256": candidate.sha256,
                    "replaced_keys": [*candidate.provenance.get("replaced_keys", []), *replaced],
                },
                "review": None,
            }
        )
        for cue in audio_set.cues:
            cue.selected = [c for c in cue.selected if c != candidate_id]
        return await self.save(audio_set, expected)

    # -- review, selection, release -------------------------------------------

    async def review(
        self,
        id: str,
        owner: str,
        expected: int,
        candidate_id: str,
        outcome: Literal["keep", "reject"],
        note: str,
        reason: str,
        actor_kind: Literal["session", "api_key"],
        sha256: str | None = None,
    ) -> AudioSet:
        """`sha256`, when given, is the file the reviewer listened to; a verdict on
        a file that has since been replaced is refused rather than moved onto it."""
        audio_set = await self.current(id, owner, expected)
        candidate = self.candidate(audio_set, candidate_id)
        if sha256 is not None and sha256 != candidate.sha256:
            raise AudioError(
                "This candidate's file changed since it was played; listen again and review it.",
                409, code="candidate_file_changed",
            )
        candidate.review = Review(
            outcome=outcome, note=note, reason=reason, sha256=candidate.sha256,
            actor_id=owner, actor_kind=actor_kind, at=now(),
        )
        if outcome == "reject":
            for cue in audio_set.cues:
                cue.selected = [c for c in cue.selected if c != candidate_id]
        return await self.save(audio_set, expected)

    async def select(
        self, id: str, owner: str, expected: int, cue_id: str, candidate_ids: list[str]
    ) -> AudioSet:
        audio_set = await self.current(id, owner, expected)
        cue = self.cue(audio_set, cue_id)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise AudioError("Select each candidate once.")
        if len(candidate_ids) > cue.keep:
            raise AudioError(f"This cue ships {cue.keep} variant(s); select at most that many.")
        for candidate_id in candidate_ids:
            candidate = self.candidate(audio_set, candidate_id)
            if candidate.cue_id != cue_id:
                raise AudioError("That candidate belongs to another cue.")
            if not self.selectable(candidate, audio_set.licence_policy):
                raise AudioError(
                    "Select only candidates you kept, whose licence this set allows.", 409
                )
        cue.selected = list(candidate_ids)
        return await self.save(audio_set, expected)

    def readiness(self, audio_set: AudioSet) -> dict[str, Any]:
        problems = []
        if not audio_set.cues:
            problems.append({"cue_id": None, "message": "Add at least one cue."})
        by_id = {c.candidate_id: c for c in audio_set.candidates}
        for cue in audio_set.cues:
            if len(cue.selected) < cue.keep:
                problems.append({
                    "cue_id": cue.cue_id,
                    "message": f"Select {cue.keep} variant(s); {len(cue.selected)} selected.",
                })
            for candidate_id in cue.selected:
                candidate = by_id.get(candidate_id)
                if candidate is None or not self.selectable(candidate, audio_set.licence_policy):
                    problems.append({
                        "cue_id": cue.cue_id,
                        "message": "A selected file is no longer kept or allowed; select again.",
                    })
        selected = [by_id[c] for cue in audio_set.cues for c in cue.selected if c in by_id]
        return {
            "ready": not problems,
            "problems": problems,
            "files": len(selected),
            "attribution_required": any(c.licence.attribution_required for c in selected),
            "previews": sum(c.licence.variant == "preview" for c in selected),
        }

    async def get_readiness(self, id: str, owner: str) -> dict[str, Any]:
        return self.readiness(await self.get(id, owner))

    async def release(self, id: str, owner: str, expected: int, name: str) -> AudioSet:
        audio_set = await self.current(id, owner, expected)
        ready = self.readiness(audio_set)
        if not ready["ready"]:
            raise AudioError(ready["problems"][0]["message"], 409)
        if any(r.name == name for r in audio_set.releases):
            raise AudioError("Release name already exists", 409)
        if self.work_guard is not None:
            await self.work_guard(owner, 0)
        by_id = {c.candidate_id: c for c in audio_set.candidates}
        # The pack writes a copy of a file for every cue that selects it, so the
        # limit counts every occurrence, from stored sizes, before anything is read.
        occurrences = [
            key
            for cue in audio_set.cues
            for candidate_id in cue.selected
            for key in filter(None, (by_id[candidate_id].asset_key, by_id[candidate_id].rendition_key))
        ]
        sizes: dict[str, int] = {}
        for key in dict.fromkeys(occurrences):
            descriptor = await self.assets.describe(key)
            if descriptor is None:
                raise AudioError("A selected file is missing from storage.", 503)
            sizes[key] = descriptor.size
        if sum(sizes[key] for key in occurrences) > MAX_RELEASE_BYTES:
            raise AudioError(TOO_LARGE, 413)
        release_id = str(uuid4())
        created_at = now()
        # Only one release holds its files in memory at a time: the slot is taken
        # before they are read, and they are dropped before it is given back.
        async with self.pack_slots:
            files = {key: await self.assets.read_limited(key, MAX_AUDIO_BYTES) for key in sizes}
            try:
                content, manifest, manifest_sha = await asyncio.to_thread(
                    packs.build_pack, audio_set, release_id, name, created_at, files,
                    max_bytes=MAX_RELEASE_BYTES,
                )
            except packs.PackTooLarge as e:
                raise AudioError(TOO_LARGE, 413) from e
            finally:
                del files
        key = f"outputs/audio/{id}/releases/{release_id}.zip"
        await self.assets.write_once(key, content, "application/zip")
        audio_set.releases.append(
            AudioRelease(
                release_id=release_id,
                name=name,
                asset_key=key,
                manifest_sha256=manifest_sha,
                source_revision=expected,
                created_at=created_at,
                file_count=len(manifest["files"]),
                attribution_required=manifest["attribution_required"],
            )
        )
        return await self.save(audio_set, expected)

    async def release_file(self, id: str, owner: str, release_id: str) -> tuple[str, str]:
        audio_set = await self.get(id, owner)
        found = next((r for r in audio_set.releases if r.release_id == release_id), None)
        if found is None:
            raise AudioError("Release not found", 404)
        return found.asset_key, f"audio-{UUID(found.release_id)}.zip"

    async def authorize_asset(self, id: str, owner: str, key: str) -> None:
        audio_set = await self.get(id, owner)
        allowed = {c.asset_key for c in audio_set.candidates}
        allowed |= {c.rendition_key for c in audio_set.candidates if c.rendition_key}
        allowed |= {r.asset_key for r in audio_set.releases}
        if key not in allowed:
            raise AudioError("Asset not found", 404)

    async def send_to_movie(
        self, id: str, owner: str, candidate_id: str, movie_id: str, movie_revision: int
    ) -> Any:
        """Add a kept candidate to one of the owner's movies as a soundtrack cue at
        the start, through the movie's own soundtrack check. Returns the movie."""
        from magic_video_editor.movies.service import MovieError
        from magic_video_editor.movies.soundtracks import AudioTrack, save_soundtracks

        if self.movie_service is None:
            raise AudioError("Movies are unavailable.", 503)
        if await self.account_policy.is_public(owner):
            refusal = own_compute_required("Movie soundtracks are not available for public accounts yet.")
            raise AudioError(refusal.message, 403, code=refusal.code)
        audio_set = await self.get(id, owner)
        candidate = self.candidate(audio_set, candidate_id)
        if candidate.review is None or candidate.review.outcome != "keep":
            raise AudioError("Keep the candidate before sending it to a movie.", 409)
        # A movie soundtrack has no credits and a movie release carries none, so a
        # sound whose licence requires attribution would lose it on the way.
        if candidate.licence.attribution_required:
            raise AudioError(
                "This sound's licence requires a credit, which movies cannot carry yet. "
                "Use a CC0 sound or your own file, or credit it from the audio release.",
                409, code="attribution_required",
            )
        # The copy grows the owner's stored files, like any upload.
        if self.work_guard is not None:
            await self.work_guard(owner, 0)
        source = candidate.rendition_key or candidate.asset_key
        content = await self.assets.read_limited(source, MAX_AUDIO_BYTES)
        try:
            movie = await self.movie_service.current(movie_id, owner, movie_revision)
            key = f"uploads/movies/{movie_id}/audio-{sha256(content).hexdigest()}"
            await self.assets.write_once(
                key, content, "audio/wav" if source.endswith(".wav") else "audio/mpeg"
            )
            if key not in movie.upload_keys:
                movie.upload_keys.append(key)
                movie = await self.movie_service.save(movie, movie_revision)
            tracks = [*movie.soundtracks, AudioTrack(asset_key=key)]
            return await save_soundtracks(self.movie_service, movie_id, owner, movie.revision, tracks)
        except MovieError as e:
            raise AudioError(str(e), e.status, current_revision=e.current_revision, code=e.code) from e

    # -- deletion ------------------------------------------------------------

    async def delete_set(self, id: str, owner: str, confirm_name: str) -> None:
        audio_set = await self.get(id, owner)
        if confirm_name != audio_set.name:
            raise AudioError("Type the exact name to delete it", 422, code="deletion_not_confirmed")
        await self.purge(audio_set)

    async def purge(self, audio_set: AudioSet) -> None:
        """Every stored file first, then the snapshots and ledger, so a failure
        part-way leaves a set that can simply be deleted again."""
        for prefix in (f"uploads/audio/{audio_set.set_id}/", f"outputs/audio/{audio_set.set_id}/"):
            await self.assets.delete_prefix(prefix)
        await self.store.delete(audio_set.set_id)

    async def erase_owner(self, owner: str) -> None:
        """Delete every audio set an owner has (account deletion)."""
        for audio_set in await self.store.list(owner):
            await self.purge(audio_set)

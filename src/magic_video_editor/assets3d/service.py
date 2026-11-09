"""3D assets: gather source pictures and mesh candidates, review them, select a chain,
and release a pack.

The first Studio part of docs/design/3d-studio.md (S1a): no engine runs yet.
Candidates come from the owner's own GLB uploads ("bring your own mesh"); the mesh
and clean stages report themselves unavailable until a 3D engine worker is
enrolled. The review, selection and release gates follow Audio: a verdict is bound
to the file's hash, selection takes only kept candidates whose parent is the
selected candidate of its own stage, and a release freezes that chain into a
create-once pack.

Nothing here uses the service's compute beyond reading a GLB's JSON table, so
every tier gets the same uploads, viewer and releases.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from outbound_gpu_worker_pool.contracts import ProfileNotQualified

from magic_video_editor.assets3d import release as packs
from magic_video_editor.assets3d import stages as engine
from magic_video_editor.assets3d.media import Assets3DMedia, GlbError
from magic_video_editor.assets3d.models import (
    GLB_TYPE,
    MAX_CANDIDATES_PER_PARENT,
    MAX_MODEL_BYTES,
    MAX_SOURCE_BYTES,
    MAX_SOURCES,
    PARENT_STAGE,
    ROLES,
    STAGES,
    Asset3D,
    Asset3DCandidate,
    Asset3DOperation,
    Asset3DRelease,
    Asset3DReview,
    Asset3DSource,
    Kind,
    Role,
    Stage,
)
from magic_video_editor.assets3d.stages import PROFILE, StageEngine, StageUnavailable
from magic_video_editor.assets3d.store import Assets3DStore, Conflict, OverAllowance
from magic_video_editor.contracts import AssetDescriptor, AssetStore, upload_size_headers
from magic_video_editor.plugins.gcs import SIGNED_UPLOAD_URL_TTL
from magic_video_editor.studio.accounts import AccountPolicy

logger = logging.getLogger(__name__)

MAX_RELEASE_BYTES = 300 * 1024 * 1024
TOO_LARGE = "A release holds at most 300 MB, counting the model, the pictures and the manifest."
IMAGE_TYPES: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})
UPLOAD_LIMITS: dict[str, int] = {
    **{media_type: MAX_SOURCE_BYTES for media_type in IMAGE_TYPES},
    GLB_TYPE: MAX_MODEL_BYTES,
}
# Grants issued but not yet used as a source or mesh. Each reserves its full size
# limit against the owner's storage, so this bounds what one asset can hold back.
MAX_PENDING_UPLOADS = 8
# Every grant stays listed on the asset (its file is deleted with it), so the
# document's size is bounded too: four views, eight meshes, and replacements.
MAX_UPLOADS = 64
# A grant unused this long can no longer be uploaded to: its signed URL expired
# well before. The margin covers a PUT that started just before expiry.
PENDING_EXPIRY = SIGNED_UPLOAD_URL_TTL + timedelta(hours=1)
# Engine stage runs one asset keeps, finished ones included.
MAX_OPERATIONS = 128


class Asset3DError(Exception):
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


def stored_type(descriptor: AssetDescriptor) -> str:
    return (descriptor.content_type or "").split(";")[0].strip().lower()


class Assets3DService:
    def __init__(
        self,
        store: Assets3DStore,
        assets: AssetStore,
        *,
        media: Assets3DMedia | None = None,
        account_policy: AccountPolicy | None = None,
        work_guard: Callable[[str, int], Awaitable[None]] | None = None,
        storage_allowance: Callable[[str], Awaitable[int | None]] | None = None,
        stages: StageEngine | None = None,
    ) -> None:
        self.store = store
        self.assets = assets
        self.media = media if media is not None else Assets3DMedia()
        self.account_policy = account_policy if account_policy is not None else AccountPolicy()
        self.work_guard = work_guard
        # What the owner may still store in 3D assets: their storage limit less
        # what every other section holds (None when unlimited). Upload grants
        # reserve against it atomically.
        self.storage_allowance = storage_allowance
        # Engine stages on the pool; off (and reported so) unless configured.
        self.stages = stages if stages is not None else StageEngine()
        # One release is zipped and stored at a time, off the event loop, so
        # memory stays bounded and other requests keep being served meanwhile.
        self.pack_slots = asyncio.Semaphore(1)

    # -- the document --------------------------------------------------------

    async def capabilities(self, owner: str) -> dict[str, Any]:
        return {
            "account_tier": (await self.account_policy.tier(owner)).value,
            "stages": await self.stages.availability(owner, await self.account_policy.is_public(owner)),
            # Uploads, the viewer and releases read files and write a zip; no
            # tier is refused them, since none of it is generation.
            "upload_available": True,
            "own_mesh_available": True,
            "viewer_available": True,
            "release_available": True,
            "kinds": ["character", "prop"],
            "source_roles": list(ROLES),
            "stage_order": list(STAGES),
            "max_sources": MAX_SOURCES,
            "max_candidates_per_parent": MAX_CANDIDATES_PER_PARENT,
            "max_model_bytes": MAX_MODEL_BYTES,
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_release_bytes": MAX_RELEASE_BYTES,
            "upload_types": sorted(UPLOAD_LIMITS),
        }

    async def create(self, owner: str, name: str, kind: Kind) -> Asset3D:
        return await self.store.create(
            Asset3D(asset_id=str(uuid4()), owner_id=owner, name=name, kind=kind)
        )

    async def get(self, id: str, owner: str) -> Asset3D:
        asset = await self.store.get(id)
        if asset is None or asset.owner_id != owner:
            raise Asset3DError("3D asset not found", 404)
        return asset

    async def list(self, owner: str) -> list[Asset3D]:
        return await self.store.list(owner)

    async def writable(self, id: str, owner: str) -> Asset3D:
        asset = await self.get(id, owner)
        if asset.deleting:
            raise Asset3DError("3D asset not found", 404)
        return asset

    async def current(self, id: str, owner: str, expected: int) -> Asset3D:
        asset = await self.writable(id, owner)
        if asset.revision != expected:
            raise self.conflict(asset.revision)
        return asset

    @staticmethod
    def conflict(current: int) -> Asset3DError:
        return Asset3DError(
            f"Revision conflict; current revision is {current}", 409, current_revision=current
        )

    async def save(self, asset: Asset3D, expected: int) -> Asset3D:
        try:
            saved = await self.store.append(asset, expected)
        except Conflict as e:
            raise self.conflict(e.revision) from e
        await self.account_assets(saved)
        return saved

    async def account_assets(self, asset: Asset3D) -> None:
        """Idempotent ownership ledger; retries never double-count stored files.
        Every upload grant is listed, so a replaced source stays counted until the
        asset is deleted."""
        try:
            keys = set(asset.upload_keys)
            keys.update(s.asset_key for s in asset.sources)
            keys.update(c.asset_key for c in asset.candidates)
            keys.update(r.asset_key for r in asset.releases)
            for key in keys - await self.store.accounted_keys(asset.asset_id):
                descriptor = await self.assets.describe(key)
                if descriptor is not None:
                    await self.store.account_asset(asset.asset_id, key, descriptor.size)
        except Exception:
            logger.exception("3D asset accounting will retry on next saved revision")

    def candidate(self, asset: Asset3D, candidate_id: str) -> Asset3DCandidate:
        candidate = next((c for c in asset.candidates if c.candidate_id == candidate_id), None)
        if candidate is None:
            raise Asset3DError("Candidate not found", 404)
        return candidate

    # -- uploads: source pictures and your own mesh --------------------------

    @staticmethod
    async def _ledger(update: Awaitable[None]) -> None:
        """A ledger write after the document is saved. A failure leaves the count
        conservative (a reservation stays at its full limit), so it is logged."""
        try:
            await update
        except Exception:
            logger.exception("3D asset storage ledger update failed")

    async def _expire_pending(self, asset: Asset3D) -> tuple[list[str], dict[str, int]]:
        """Take grants whose URL has long expired off the pending list: the ones
        never uploaded leave the asset, the ones uploaded stay as stored files.
        Returns both, for the ledger once the change is saved."""
        cutoff = datetime.now(UTC) - PENDING_EXPIRY
        never_used: list[str] = []
        stored: dict[str, int] = {}
        for key, granted_at in list(asset.pending_uploads.items()):
            if datetime.fromisoformat(granted_at) > cutoff:
                continue
            descriptor = await self.assets.describe(key)
            del asset.pending_uploads[key]
            if descriptor is None:
                asset.upload_keys.remove(key)
                never_used.append(key)
            else:
                stored[key] = descriptor.size
        return never_used, stored

    async def _reconcile(self, asset: Asset3D) -> None:
        """Save the asset with its expired grants taken off, then settle or drop
        their reservations. Nothing to do when none has expired."""
        for _ in range(8):
            never_used, stored = await self._expire_pending(asset)
            if not never_used and not stored:
                return
            try:
                await self.save(asset, asset.revision)
                break
            except Asset3DError as e:
                if e.status != 409:
                    raise
            fresh = await self.store.get(asset.asset_id)
            if fresh is None or fresh.deleting:
                return
            asset = fresh
        else:
            return
        for gone in never_used:
            await self._ledger(self.store.forget_asset(asset.asset_id, gone))
        for kept, size in stored.items():
            await self._ledger(self.store.settle_asset(asset.asset_id, kept, size))

    async def reconcile_owner(self, owner: str) -> None:
        """Release every expired grant the owner holds, on any of their assets."""
        for asset in await self.store.list(owner):
            if not asset.deleting:
                await self._reconcile(asset)

    async def _guard(self, owner: str, requested: int = 0) -> None:
        """The storage and work guard (`requested`: jobs about to start), after
        giving back what expired grants hold: reservations that can no longer be
        used never keep an owner out."""
        if self.work_guard is None:
            return
        try:
            await self.work_guard(owner, requested)
        except Asset3DError as e:
            if e.status != 507:
                raise
            await self.reconcile_owner(owner)
            await self.work_guard(owner, requested)

    async def upload(
        self, id: str, owner: str, content_type: str, byte_length: int | None = None
    ) -> dict[str, Any]:
        """A create-once upload grant, recorded on the asset before the URL is
        issued, and reserved against the owner's storage until it is used: at
        `byte_length` when the caller says how large the file is (the signed
        ceiling is then that size), else at the type's full limit. The reservation
        is checked and recorded in one step per owner. An asset holds at most
        `MAX_PENDING_UPLOADS` unused grants."""
        limit = UPLOAD_LIMITS.get(content_type)
        if limit is None:
            raise Asset3DError("Upload a PNG, JPEG or WebP picture, or a GLB model.")
        if byte_length is not None:
            if byte_length > limit:
                raise Asset3DError(f"The file is larger than {limit // (1024 * 1024)} MB.")
            limit = byte_length
        await self._reconcile(await self.writable(id, owner))
        await self._guard(owner)
        allowance = await self.storage_allowance(owner) if self.storage_allowance is not None else None
        reconciled = False
        for _ in range(8):
            asset = await self.writable(id, owner)
            if len(asset.pending_uploads) >= MAX_PENDING_UPLOADS:
                raise Asset3DError(
                    f"This asset has {MAX_PENDING_UPLOADS} uploads not yet added; add them as "
                    "pictures or meshes first, or wait an hour for unused ones to expire.",
                    409, code="too_many_pending_uploads",
                )
            if len(asset.upload_keys) >= MAX_UPLOADS:
                raise Asset3DError(
                    f"A 3D asset takes at most {MAX_UPLOADS} uploads; start a new asset.",
                    409, code="too_many_uploads",
                )
            key = f"uploads/assets3d/{id}/{uuid4()}"
            asset.upload_keys.append(key)
            asset.pending_uploads[key] = now()
            # The grant and its reservation are saved in one transaction, before
            # any URL exists: a failure of any kind leaves neither behind.
            try:
                saved = await self.store.append_reserving(asset, asset.revision, owner, key, limit, allowance)
            except Conflict:
                continue
            except OverAllowance:
                if not reconciled:
                    reconciled = True
                    await self.reconcile_owner(owner)
                    continue
                raise Asset3DError(
                    "Storage limit reached: this upload would not fit in what you have left. "
                    "Free storage, or give the file's `byte_length` to reserve only its size.",
                    507,
                ) from None
            await self.account_assets(saved)
            asset = saved
            break
        else:
            raise Asset3DError("3D asset is busy; retry", 409)
        return {
            "asset_key": key,
            "upload_url": await self.assets.create_upload_url(key, content_type, max_bytes=limit),
            "upload_headers": upload_size_headers(limit),
            "revision": asset.revision,
        }

    async def _stored_upload(
        self, asset: Asset3D, asset_key: str, limit: int, types: frozenset[str], kind: str
    ) -> AssetDescriptor:
        """The granted upload's size and stored type, checked before any byte is
        read. The file is used where it was uploaded, never copied, since the grant
        is create-once; the bytes are later checked to be what the type says."""
        if asset_key not in asset.upload_keys:
            raise Asset3DError("Upload not granted to this 3D asset.", 403)
        descriptor = await self.assets.describe(asset_key)
        if descriptor is None:
            raise Asset3DError("Nothing has been uploaded to this key yet; PUT the file first.", 404)
        if stored_type(descriptor) not in types:
            raise Asset3DError(
                f"This file was uploaded as {stored_type(descriptor) or 'an unknown type'}; "
                f"upload a {kind} with its own Content-Type ({', '.join(sorted(types))})."
            )
        if descriptor.size > limit:
            raise Asset3DError(f"The file is larger than {limit // (1024 * 1024)} MB.")
        return descriptor

    async def add_source(
        self, id: str, owner: str, expected: int, role: Role, asset_key: str, file_name: str,
        actor_kind: Literal["session", "api_key"],
    ) -> Asset3D:
        """Set the picture for one view. A new picture for a role replaces the old
        one in the document; the old file stays stored until the asset is deleted."""
        asset = await self.current(id, owner, expected)
        # No storage guard: the file is already stored and counted (reserved at
        # grant time), and adding it only shrinks that to its real size.
        descriptor = await self._stored_upload(
            asset, asset_key, MAX_SOURCE_BYTES, IMAGE_TYPES, "PNG, JPEG or WebP picture"
        )
        digest, media_type = await self.media.image(self.assets, asset_key, descriptor.size)
        if media_type is None:
            raise Asset3DError("Choose a PNG, JPEG or WebP picture.")
        if media_type != stored_type(descriptor):
            raise Asset3DError(
                f"The file is {media_type} but was uploaded as {stored_type(descriptor)}; "
                f"upload it again with Content-Type {media_type}."
            )
        previous = next((s for s in asset.sources if s.role == role), None)
        if previous is not None and previous.sha256 == digest:
            raise Asset3DError(f"That picture is already the {role} view.", 409)
        source = Asset3DSource(
            source_id=str(uuid4()),
            role=role,
            asset_key=asset_key,
            sha256=digest,
            byte_length=descriptor.size,
            media_type=media_type,
            provenance={"file_name": file_name, "uploaded_by": actor_kind},
            created_at=now(),
        )
        asset.sources = sorted(
            [s for s in asset.sources if s.role != role] + [source],
            key=lambda s: ROLES.index(s.role),
        )
        asset.pending_uploads.pop(asset_key, None)
        saved = await self.save(asset, expected)
        # The reservation becomes the file's real size.
        await self._ledger(self.store.settle_asset(id, asset_key, descriptor.size))
        return saved

    async def add_mesh(
        self, id: str, owner: str, expected: int, asset_key: str, file_name: str,
        actor_kind: Literal["session", "api_key"],
    ) -> Asset3D:
        """Bring your own mesh: a GLB upload becomes a `mesh` candidate, measured
        from its own structure, with no gate (no engine judged it)."""
        asset = await self.current(id, owner, expected)
        if sum(c.stage == "mesh" and c.parent_id is None for c in asset.candidates) >= MAX_CANDIDATES_PER_PARENT:
            raise Asset3DError(
                f"A stage holds at most {MAX_CANDIDATES_PER_PARENT} candidates from one parent.", 409
            )
        # No storage guard, as for pictures: the upload is already counted.
        descriptor = await self._stored_upload(
            asset, asset_key, MAX_MODEL_BYTES, frozenset({GLB_TYPE}), "GLB (binary glTF 2.0) file"
        )
        try:
            digest, measured = await self.media.glb(self.assets, asset_key, descriptor.size)
        except GlbError as e:
            raise Asset3DError(str(e)) from e
        if any(c.stage == "mesh" and c.sha256 == digest for c in asset.candidates):
            raise Asset3DError("That file is already a mesh candidate.", 409)
        asset.candidates.append(
            Asset3DCandidate(
                candidate_id=str(uuid4()),
                stage="mesh",
                origin="upload",
                asset_key=asset_key,
                sha256=digest,
                byte_length=descriptor.size,
                measured=measured,
                provenance={"file_name": file_name, "uploaded_by": actor_kind},
                created_at=now(),
            )
        )
        asset.pending_uploads.pop(asset_key, None)
        saved = await self.save(asset, expected)
        await self._ledger(self.store.settle_asset(id, asset_key, descriptor.size))
        return saved

    # -- review, selection, release -------------------------------------------

    async def review(
        self,
        id: str,
        owner: str,
        expected: int,
        candidate_id: str,
        outcome: Literal["keep", "reject"],
        note: str,
        actor_kind: Literal["session", "api_key"],
        sha256: str | None = None,
    ) -> Asset3D:
        """`sha256`, when given, is the file the reviewer looked at; a verdict on a
        different file is refused rather than moved onto it."""
        asset = await self.current(id, owner, expected)
        candidate = self.candidate(asset, candidate_id)
        if sha256 is not None and sha256 != candidate.sha256:
            raise Asset3DError(
                "This candidate's file is not the one reviewed; look again and review it.",
                409, code="candidate_file_changed",
            )
        candidate.review = Asset3DReview(
            outcome=outcome, note=note, sha256=candidate.sha256,
            actor_id=owner, actor_kind=actor_kind, at=now(),
        )
        if outcome == "reject" and asset.selections.get(candidate.stage) == candidate_id:
            del asset.selections[candidate.stage]
        return await self.save(asset, expected)

    @staticmethod
    def selectable(candidate: Asset3DCandidate) -> bool:
        review = candidate.review
        return review is not None and review.outcome == "keep" and review.sha256 == candidate.sha256

    async def select(
        self, id: str, owner: str, expected: int, selections: dict[Stage, str | None]
    ) -> Asset3D:
        """Set or clear (`None`) the selected candidate of each named stage, all at
        once. A newly selected candidate with a parent needs that parent selected
        in its own stage, in this request or before. Selecting a different parent
        leaves later stages alone; readiness then reports the mixed chain."""
        asset = await self.current(id, owner, expected)
        chosen = dict(asset.selections)
        for stage, candidate_id in selections.items():
            if candidate_id is None:
                chosen.pop(stage, None)
                continue
            candidate = self.candidate(asset, candidate_id)
            if candidate.stage != stage:
                raise Asset3DError(f"That candidate is a {candidate.stage} candidate, not {stage}.")
            if not self.selectable(candidate):
                raise Asset3DError("Select only candidates you kept.", 409)
            chosen[stage] = candidate_id
        for stage, candidate_id in selections.items():
            if candidate_id is None:
                continue
            parent_id = self.candidate(asset, candidate_id).parent_id
            parent_stage = PARENT_STAGE[stage]
            if parent_id is not None and (parent_stage is None or chosen.get(parent_stage) != parent_id):
                raise Asset3DError(
                    f"This {stage} candidate was made from a {parent_stage} candidate "
                    "that is not selected; select its parent first.",
                    409, code="parent_not_selected",
                )
        asset.selections = {stage: chosen[stage] for stage in STAGES if stage in chosen}
        return await self.save(asset, expected)

    def readiness(self, asset: Asset3D) -> dict[str, Any]:
        problems: list[dict[str, Any]] = []
        by_id = {c.candidate_id: c for c in asset.candidates}
        if not any(asset.selections.get(stage) for stage in packs.RELEASED_STAGES):
            problems.append({"stage": None, "message": "Select a mesh to release."})
        for stage in STAGES:
            candidate_id = asset.selections.get(stage)
            if candidate_id is None:
                continue
            candidate = by_id.get(candidate_id)
            if candidate is None or not self.selectable(candidate):
                problems.append({
                    "stage": stage,
                    "message": f"The selected {stage} candidate is no longer kept; review and select again.",
                })
                continue
            if candidate.gate is not None and not candidate.gate.passed:
                problems.append({
                    "stage": stage,
                    "message": f"The selected {stage} candidate failed its {candidate.gate.name} gate.",
                })
            parent_stage = PARENT_STAGE[stage]
            if candidate.parent_id is not None and (
                parent_stage is None or asset.selections.get(parent_stage) != candidate.parent_id
            ):
                problems.append({
                    "stage": stage,
                    "message": (
                        f"The selected {stage} candidate was made from a {parent_stage} candidate "
                        "that is not selected; the chain is mixed."
                    ),
                })
        selected = packs.chain(asset)
        model = selected[-1] if selected else None
        return {
            "ready": not problems,
            "problems": problems,
            "model": None if model is None else {"stage": model.stage, "candidate_id": model.candidate_id},
            "sources": len(asset.sources),
        }

    async def get_readiness(self, id: str, owner: str) -> dict[str, Any]:
        return self.readiness(await self.get(id, owner))

    async def release(self, id: str, owner: str, expected: int, name: str) -> Asset3D:
        await self.writable(id, owner)
        # The guard first: when it gives back expired grants it saves new
        # revisions, possibly of this asset, and a release built from a revision
        # read before that could only end in a 409 with its zip already stored.
        await self._guard(owner)
        asset = await self.current(id, owner, expected)
        ready = self.readiness(asset)
        if not ready["ready"]:
            raise Asset3DError(ready["problems"][0]["message"], 409)
        if any(r.name == name for r in asset.releases):
            raise Asset3DError("Release name already exists", 409)
        model = packs.chain(asset)[-1]
        # The pack writes a copy for every place a file appears (one upload may be
        # two views), so the limit counts every occurrence, from stored sizes,
        # before anything is read: an oversized release is refused without
        # waiting for the pack slot.
        occurrences = [model.asset_key, *(s.asset_key for s in asset.sources)]
        limits = {key: MAX_SOURCE_BYTES for key in occurrences} | {model.asset_key: MAX_MODEL_BYTES}
        sizes: dict[str, int] = {}
        for key in limits:
            descriptor = await self.assets.describe(key)
            if descriptor is None:
                raise Asset3DError("A selected file is missing from storage.", 503)
            sizes[key] = descriptor.size
        if sum(sizes[key] for key in occurrences) > MAX_RELEASE_BYTES:
            raise Asset3DError(TOO_LARGE, 413)
        release_id = str(uuid4())
        created_at = now()
        key = f"outputs/assets3d/{id}/releases/{release_id}.zip"
        # Only one release holds its files in memory at a time: the slot is taken
        # before they are read and held until the zip is stored, and both the
        # files and the zip are dropped before it is given back.
        async with self.pack_slots:
            files = {key: await self.assets.read_limited(key, limit) for key, limit in limits.items()}
            try:
                content, manifest, manifest_sha = await asyncio.to_thread(
                    packs.build_pack, asset, release_id, name, created_at, files,
                    max_bytes=MAX_RELEASE_BYTES,
                )
            except packs.PackTooLarge as e:
                raise Asset3DError(TOO_LARGE, 413) from e
            finally:
                del files
            try:
                size = len(content)
                # A revision changed while packing would refuse the save below;
                # find out before storing a zip nothing would record.
                latest = await self.store.get(id)
                if latest is None or latest.deleting:
                    raise Asset3DError("3D asset not found", 404)
                if latest.revision != expected:
                    raise self.conflict(latest.revision)
                await self.assets.write_once(key, content, "application/zip")
            finally:
                del content
        # The deletion fence: `purge` marks the asset before removing its files,
        # so if no mark is visible now that the zip is stored, any later purge
        # removes it. If the asset is gone or going, the zip is removed here.
        fresh = await self.store.get(id)
        if fresh is None or fresh.deleting:
            await self.assets.delete_prefix(f"outputs/assets3d/{id}/")
            raise Asset3DError("3D asset not found", 404)
        # Counted now, whatever the save below does: a concurrent edit makes it a
        # 409, and the stored zip must not then sit outside the owner's storage.
        await self._ledger(self.store.settle_asset(id, key, size))
        asset.releases.append(
            Asset3DRelease(
                release_id=release_id,
                name=name,
                asset_key=key,
                manifest_sha256=manifest_sha,
                source_revision=expected,
                created_at=created_at,
                file_count=len(manifest["files"]),
                model_candidate_id=model.candidate_id,
            )
        )
        return await self.save(asset, expected)

    async def release_file(self, id: str, owner: str, release_id: str) -> tuple[str, str]:
        asset = await self.get(id, owner)
        found = next((r for r in asset.releases if r.release_id == release_id), None)
        if found is None:
            raise Asset3DError("Release not found", 404)
        return found.asset_key, f"3d-{UUID(found.release_id)}.zip"

    async def authorize_asset(self, id: str, owner: str, key: str) -> None:
        asset = await self.get(id, owner)
        allowed = {s.asset_key for s in asset.sources}
        allowed |= {c.asset_key for c in asset.candidates}
        allowed |= {r.asset_key for r in asset.releases}
        if key not in allowed:
            raise Asset3DError("Asset not found", 404)

    # -- engine stages ---------------------------------------------------------

    async def running_jobs(self, owner: str) -> int:
        """Stage jobs submitted or being submitted, for the owner's concurrency cap."""
        return sum(
            operation.status in ("submitting", "running")
            for asset in await self.store.list(owner)
            for operation in asset.operations
        )

    async def submit_stage(
        self, id: str, owner: str, stage: Stage, request_id: str, parent_id: str | None,
        settings: dict[str, Any],
    ) -> Asset3D:
        """Run an engine stage: resolve and pin a worker, journal the job on the
        asset, submit it, and return the asset. A repeated `request_id` returns the
        same run (and resubmits it if its submission was interrupted); reused for
        a different request, it is refused."""
        asset = await self.writable(id, owner)
        if self._journalled(asset, request_id, stage, parent_id, settings):
            return await self.reconcile(id, owner)
        public = await self.account_policy.is_public(owner)
        # Nothing reaches the pool, and nothing is resolved, while stages are off.
        reason = self.stages.blocked()
        if reason is not None:
            raise Asset3DError(reason, 409, code="stage_unavailable")
        inputs = self._stage_inputs(asset, stage, parent_id)
        try:
            resolved = engine.checked_settings(stage, settings)
        except ValueError as e:
            raise Asset3DError(str(e)) from e
        try:
            pin = await self.stages.pin(owner, public, stage)
        except StageUnavailable as e:
            raise Asset3DError(str(e), 409, code="stage_unavailable") from e
        await self._guard(owner, 1)
        spec = engine.engine_stage(stage)
        operation_id = str(uuid4())
        operation = Asset3DOperation(
            operation_id=operation_id, request_id=request_id, stage=stage, parent_id=parent_id,
            settings=resolved, inputs=inputs, capability_id=engine.CAPABILITIES[stage],
            profile=PROFILE, worker_id=pin["worker_id"], recipe_key=pin["recipe_key"],
            recipe_version=pin["recipe_version"], configuration_key=pin["configuration_key"],
            configuration_version=pin["configuration_version"], min_vram_mb=pin["min_vram_mb"],
            deadline_seconds=spec.deadline_seconds, tenant_id=pin["tenant_id"], job_id=str(uuid4()),
            output_key=f"outputs/assets3d/{id}/candidates/{operation_id}.glb",
            created_at=now(), updated_at=now(),
        )
        for _ in range(8):
            asset = await self.writable(id, owner)
            # A concurrent call may have journalled this request_id since the first
            # look; it is the same run only if it is the same request.
            if self._journalled(asset, request_id, stage, parent_id, settings):
                break
            if len(asset.operations) >= MAX_OPERATIONS:
                raise Asset3DError(f"A 3D asset keeps at most {MAX_OPERATIONS} stage runs.", 409)
            # The inputs are checked again on the revision the journal lands in.
            if self._stage_inputs(asset, stage, parent_id) != inputs:
                raise Asset3DError("The asset changed; send the request again.", 409)
            asset.operations.append(operation)
            try:
                await self.save(asset, asset.revision)
                break
            except Asset3DError as e:
                if e.status != 409:
                    raise
        else:
            raise Asset3DError("3D asset is busy; retry", 409)
        return await self.reconcile(id, owner)

    @staticmethod
    def _journalled(
        asset: Asset3D, request_id: str, stage: Stage, parent_id: str | None, settings: dict[str, Any]
    ) -> bool:
        """Whether this request_id is already journalled for this same request. A
        setting the request left out matches whatever default was recorded; one it
        gave must match. The id journalled for any other request is refused."""
        existing = next((o for o in asset.operations if o.request_id == request_id), None)
        if existing is None:
            return False
        if (existing.stage != stage or existing.parent_id != parent_id
                or any(existing.settings.get(k) != v for k, v in settings.items())):
            raise Asset3DError(
                "This request_id was already used for a different stage request.",
                409, code="request_id_reused",
            )
        return True

    def _stage_inputs(self, asset: Asset3D, stage: Stage, parent_id: str | None) -> dict[str, str]:
        """The stage's inputs, by the engine's names, or why it cannot run on them."""
        if stage == "mesh":
            if parent_id is not None:
                raise Asset3DError("A mesh is made from the source pictures; it takes no parent.")
            views = {s.role: s.asset_key for s in asset.sources}
            if "front" not in views:
                raise Asset3DError("Add a front picture before generating a mesh.", 409)
            inputs = views
        else:
            if parent_id is None:
                raise Asset3DError("Name the mesh candidate to clean (`parent_id`).")
            parent = self.candidate(asset, parent_id)
            if parent.stage != "mesh":
                raise Asset3DError("A clean is made from a mesh candidate.")
            if not self.selectable(parent) and asset.selections.get("mesh") != parent_id:
                raise Asset3DError("Keep or select the mesh before cleaning it.", 409)
            if parent.gate is not None and not parent.gate.passed:
                raise Asset3DError(
                    f"That mesh failed its {parent.gate.name} gate; it cannot be cleaned.", 409
                )
            inputs = {"mesh": parent.asset_key}
        made = sum(c.stage == stage and c.parent_id == parent_id for c in asset.candidates)
        running = sum(o.stage == stage and o.parent_id == parent_id
                      and o.status in ("submitting", "running") for o in asset.operations)
        if made + running >= MAX_CANDIDATES_PER_PARENT:
            raise Asset3DError(
                f"A stage holds at most {MAX_CANDIDATES_PER_PARENT} candidates from one parent.", 409
            )
        return inputs

    async def reconcile(self, id: str, owner: str) -> Asset3D:
        """Submit journalled stage jobs, and fold finished ones into candidates.
        Safe to run anywhere, any number of times: submission replays the same
        job, and results land through compare-and-swap."""
        for _ in range(8):
            asset = await self.get(id, owner)
            pool = self.stages.pool
            if pool is None:
                return asset
            if asset.deleting:
                # Being deleted: nothing more runs, and nothing becomes a candidate.
                # Whatever a stage already stored goes, as a release's zip would.
                await self._cancel_open(asset)
                await self.assets.delete_prefix(f"outputs/assets3d/{id}/")
                return asset
            changed = False
            submitted = []
            for operation in asset.operations:
                if operation.status == "submitting":
                    changed = True
                    submitted.append(operation)
                    try:
                        await pool.submit(engine.submission(id, operation))
                    except ProfileNotQualified as e:
                        operation.status, operation.error = "failed", e.reason[:500]
                    except Exception:
                        logger.exception("3D stage submission interrupted")
                        operation.error = "Submission interrupted; reconcile to retry"
                    else:
                        operation.status, operation.error = "running", None
                    operation.updated_at = now()
                elif operation.status == "running":
                    record = await pool.get(operation.job_id)
                    status = str(record.status) if record is not None else None
                    if status == "completed":
                        await self._fold(asset, operation, record)
                        changed = True
                    elif status in ("failed", "cancelled"):
                        operation.status = status  # type: ignore[assignment]
                        operation.error = (record.failure_message or record.error or "The stage failed")[:500]
                        operation.updated_at = now()
                        changed = True
            if submitted:
                # The deletion fence, as for release zips: `purge` marks before it
                # cancels, so if no mark is visible now that the jobs exist, its
                # cancellation finds them; if one is, they are cancelled here.
                fresh = await self.store.get(id)
                if fresh is None or fresh.deleting:
                    for operation in submitted:
                        try:
                            await pool.cancel(operation.job_id)
                        except Exception:
                            logger.exception("3D stage job could not be cancelled after a delete")
                    raise Asset3DError("3D asset not found", 404)
            if not changed:
                return asset
            try:
                return await self.save(asset, asset.revision)
            except Asset3DError as e:
                if e.status != 409:
                    raise
        raise Asset3DError("3D asset is busy; reconcile again", 409)

    async def _fold(self, asset: Asset3D, operation: Asset3DOperation, record: Any) -> None:
        """A completed job's GLB becomes a candidate, re-read by Studio as a cross-check;
        its provenance and gate come from the attested diagnostics only."""
        operation.updated_at = now()
        descriptor = await self.assets.describe(operation.output_key)
        if descriptor is None:
            operation.status, operation.error = "failed", "The stage reported success but stored no file."
            return
        # Counted as soon as it is seen, whatever happens to the save.
        await self._ledger(self.store.settle_asset(asset.asset_id, operation.output_key, descriptor.size))
        if descriptor.size > MAX_MODEL_BYTES:
            operation.status, operation.error = "failed", "The stage's GLB is larger than 200 MB."
            return
        try:
            digest, measured = await self.media.glb(self.assets, operation.output_key, descriptor.size)
        except GlbError as e:
            operation.status, operation.error = "failed", f"The stage's output is not a valid GLB: {e}"[:500]
            return
        if record.output_sha256 and record.output_sha256 != digest:
            operation.status, operation.error = "failed", "The stored GLB is not the one the worker attested."
            return
        diagnostics = engine.attested(record)
        asset.candidates.append(
            Asset3DCandidate(
                candidate_id=operation.operation_id,
                stage=operation.stage,
                parent_id=operation.parent_id,
                origin="engine",
                asset_key=operation.output_key,
                sha256=digest,
                byte_length=descriptor.size,
                measured=measured,
                gate=engine.gate_from(diagnostics, operation.stage),
                provenance=engine.provenance_from(record, operation),
                created_at=now(),
            )
        )
        operation.status, operation.error, operation.candidate_id = "succeeded", None, operation.operation_id

    # -- deletion ------------------------------------------------------------

    async def delete_asset(self, id: str, owner: str, confirm_name: str) -> None:
        asset = await self.get(id, owner)
        if confirm_name != asset.name:
            raise Asset3DError("Type the exact name to delete it", 422, code="deletion_not_confirmed")
        await self.purge(asset)

    async def purge(self, asset: Asset3D) -> None:
        """The deletion mark first, then every stored file, then the snapshots and
        ledger, so a failure part-way leaves an asset that can simply be deleted
        again. The mark fences releases still writing a zip (see `release`)."""
        await self.store.mark_deleting(asset.asset_id)
        # The marked revision, not the one the caller read: a stage journalled in
        # between is in it, and none can be journalled after it. Running stages
        # would otherwise write their output into a deleted prefix.
        marked = await self.store.get(asset.asset_id) or asset
        await self._cancel_open(marked)
        for prefix in (f"uploads/assets3d/{asset.asset_id}/", f"outputs/assets3d/{asset.asset_id}/"):
            await self.assets.delete_prefix(prefix)
        await self.store.delete(asset.asset_id)

    async def _cancel_open(self, asset: Asset3D) -> None:
        if self.stages.pool is None:
            return
        for operation in asset.operations:
            if operation.status in ("submitting", "running"):
                try:
                    await self.stages.pool.cancel(operation.job_id)
                except Exception:
                    logger.exception("3D stage job could not be cancelled on delete")

    async def erase_owner(self, owner: str) -> None:
        """Delete every 3D asset an owner has (account deletion)."""
        for asset in await self.store.list(owner):
            await self.purge(asset)

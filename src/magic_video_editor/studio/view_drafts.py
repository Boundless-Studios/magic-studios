"""Direction drafts made from a turnaround's picked frames, and their checks.

A draft is immutable: its processed image and descriptor are written once, each
check is an append-only receipt, and discarding marks the manifest entry (inside
the revision fence) and writes a tombstone receipt. Nothing is ever rewritten.
Every stored draft asset is counted toward its owner's storage, keyed, and
released with the project. A draft becomes a reference only through setup
preview/apply, which revalidates all of it (`resolve_draft`). Studio never
rotates or mirrors pixels and never binds a draft by itself.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from PIL import Image

from magic_video_editor.contracts import AssetNotFound, AssetStorageUnavailable, AssetTooLarge

from .models import ViewDraftEntry, ViewId, ViewReference

DRAFT_ID = re.compile(r"^draft-[0-9a-f]{32}$")
PROCESSING = "key_green_screen+keep_main_subject+normalise_subject_framing@1024.v1"
_DESCRIPTOR_LIMIT = 1_000_000
logger = logging.getLogger(__name__)


class DraftUnavailable(ValueError):
    """The draft cannot be made, checked or bound in its current state."""


def _prefix(project_id: str) -> str:
    return f"sources/studio/{project_id}/view-drafts/"


def draft_key_prefix(project_id: str) -> str:
    """Where a project's draft assets live; their usage is counted per key."""
    return _prefix(project_id)


async def _write(service, manifest, key: str, content: bytes, content_type: str) -> bool:
    """Write a draft asset once and count it toward the owner's storage, keyed.

    The count is idempotent per key, so a retry or a lost race never adds it
    twice. A project deleted meanwhile has already released its keys: give this
    one back too.
    """
    from .service import ProjectNotFound

    written = await service._assets.write_once(key, content, content_type)
    descriptor = await service._assets.describe(key)
    await service._owner_usage.record_artifact(manifest.owner_id, key,
                                               descriptor.size if descriptor is not None else len(content))
    if await service._store.get(manifest.project_id) is None:
        await service._owner_usage.release_artifact(manifest.owner_id, key)
        raise ProjectNotFound(manifest.project_id)
    return written


def _entry(manifest, draft_id: str) -> ViewDraftEntry:
    entry = next((d for d in manifest.view_drafts if d.draft_id == draft_id), None)
    if entry is None:
        raise ValueError("draft is not in this project")
    return entry


def _process(frame: bytes) -> bytes:
    """The one processing path every draft takes, as the accepted evidence did."""
    from magic_video_editor.plugins.subject_pool import key_green_screen, keep_main_subject, normalise_subject_framing

    return normalise_subject_framing(keep_main_subject(key_green_screen(frame)))


def _card_digest(card: list[str], intent: dict[str, Any], version: str) -> str:
    return sha256(json.dumps({"card": card, "intent": intent, "version": version},
                             sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def create(service, project_id: str, expected_revision: int, turnaround_id: str, pick_revision: int,
                 heading: ViewId, standing: bool, request_id: str, actor_id: str) -> dict[str, Any]:
    """Make a frame draft for one picked heading. Repeating a request returns the same draft."""
    from magic_video_editor.plugins.subject_pool import TerminalSubjectProviderError

    from .service import (
        MAX_MOTION_OUTPUT_BYTES, MotionAssetError, MotionDerivationError, MotionOperationNotReady,
        SuggestionsUnavailable,
    )
    from .store import RevisionConflict
    from .turnaround_cards import CARDS_VERSION, placement_card, synthesized_parts
    from .turnarounds import PICKS_VERSION, _decode_frames, _pick_analysis, _take

    if standing:
        # Refused before the idempotent return, so a frame draft never answers for it.
        raise DraftUnavailable("The standing pass is not available yet; use the frame as it is")
    current = await service._require_owned_project(project_id, actor_id)
    draft_id = "draft-" + sha256(f"{project_id}:{turnaround_id}:{pick_revision}:{heading.value}:{request_id}"
                                 .encode()).hexdigest()[:32]
    if any(d.draft_id == draft_id for d in current.manifest.view_drafts):
        return await describe(service, current.manifest, draft_id)
    if current.revision != expected_revision:
        raise RevisionConflict(current.revision)
    await service._require_storage_headroom(actor_id)
    take = _take(current.manifest, turnaround_id)
    if take.candidate is None or take.candidate.metadata is None or not take.stored_bytes_recorded:
        # Settled means counted too: until then reconciliation still owns the take.
        raise MotionOperationNotReady("turnaround has not settled")
    if heading is take.source_heading:
        raise DraftUnavailable(f"{heading.value} is the source; it already has a reference")
    _require_picks(take, pick_revision)
    pick = next((p for p in take.pick_set.picks if p.heading is heading), None)
    if pick is None:
        raise DraftUnavailable(f"{heading.value} has no saved pick in this turnaround")
    try:
        # The suggestion the pick was checked against, kept with the draft.
        analysis = await _pick_analysis(service, project_id, take)
        suggested = analysis["suggested_picks"].get(heading.value, {}).get("frame")
    except SuggestionsUnavailable:
        suggested = None
    try:
        video = await service._assets.read_limited(take.candidate.artifact_key, MAX_MOTION_OUTPUT_BYTES)
    except (AssetNotFound, AssetTooLarge) as exc:
        raise MotionAssetError("motion_asset_missing", "turnaround asset cannot be read") from exc
    if sha256(video).hexdigest() != take.candidate.sha256:
        raise MotionAssetError("motion_asset_missing", "turnaround asset digest does not match metadata")
    try:
        frames = await asyncio.to_thread(_decode_frames, video)
        if not 0 <= pick.frame_index < len(frames):
            raise DraftUnavailable("the picked frame is outside the take")
        raw = frames[pick.frame_index]
        processed = await asyncio.to_thread(_process, raw)
        with Image.open(io.BytesIO(processed)) as opened:
            width, height = opened.size
    except TerminalSubjectProviderError as exc:
        raise DraftUnavailable(f"frame {pick.frame_index} could not be keyed: {exc}") from exc
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if isinstance(exc, DraftUnavailable):
            raise
        raise MotionDerivationError("frame_draft_failed", f"frame {pick.frame_index} could not be drafted: {exc}") from exc
    image_key = _prefix(project_id) + f"{draft_id}.png"
    await _write(service, current.manifest, image_key, processed, "image/png")
    stored = await service._assets.read_limited(image_key, MAX_MOTION_OUTPUT_BYTES)
    if sha256(stored).hexdigest() != sha256(processed).hexdigest():
        raise DraftUnavailable("the draft image failed its integrity check; nothing was created")
    card = placement_card(take.source_heading, heading, take.equipment)
    intent = {"source_heading": take.source_heading.value, "heading": heading.value,
              "equipment": [row.model_dump(mode="json") for row in take.equipment]}
    descriptor = {
        "draft_id": draft_id, "project_id": project_id, "turnaround_id": turnaround_id,
        "heading": heading.value, "standing": False,
        "pick_revision": pick_revision, "frame_index": pick.frame_index,
        "suggested_frame": suggested, "picks_version": PICKS_VERSION,
        "overrides": [f for f in take.pick_set.findings if f.get("heading") == heading.value],
        "source_view_id": take.source_view_id, "source_heading": take.source_heading.value,
        "source_reference": take.source_reference.model_dump(mode="json"),
        "candidate_id": take.candidate.candidate_id, "video_sha256": take.candidate.sha256,
        "raw_frame_sha256": sha256(raw).hexdigest(), "processing": PROCESSING,
        "image": {"key": image_key, "sha256": sha256(processed).hexdigest(), "width": width, "height": height},
        "placement_card": card, "card_intent": intent, "cards_version": CARDS_VERSION,
        "card_digest": _card_digest(card, intent, CARDS_VERSION),
        "synthesized_parts": synthesized_parts(take.source_heading, heading, take.equipment),
        "created_by": actor_id, "created_at": datetime.now(UTC).isoformat(),
    }
    await _write(service, current.manifest, _prefix(project_id) + f"{draft_id}.json",
                 json.dumps(descriptor, separators=(",", ":")).encode(), "application/json")
    entry = ViewDraftEntry(draft_id=draft_id, turnaround_id=turnaround_id, heading=heading,
                           created_at=datetime.now(UTC))
    while True:
        try:
            await service._store.append(project_id, current.revision, current.manifest.model_copy(
                update={"view_drafts": (*current.manifest.view_drafts, entry)}))
            break
        except RevisionConflict:
            current = await service._require_owned_project(project_id, actor_id)
            if any(d.draft_id == draft_id for d in current.manifest.view_drafts):
                break
            # If the picks were saved again meanwhile, the draft is still recorded,
            # so its stored assets stay listed and discardable, but it reports
            # itself stale and cannot be bound (`_lineage_problem`).
    current = await service._require_owned_project(project_id, actor_id)
    return await describe(service, current.manifest, draft_id)


def _require_picks(take, pick_revision: int) -> None:
    if take.pick_set is None or take.pick_set.revision != pick_revision:
        raise DraftUnavailable("drafts come from the current saved pick set; save picks or read them again")


async def _descriptor(service, project_id: str, draft_id: str) -> dict[str, Any]:
    raw = await service._assets.read_limited(_prefix(project_id) + f"{draft_id}.json", _DESCRIPTOR_LIMIT)
    descriptor = json.loads(raw)
    if descriptor.get("draft_id") != draft_id or descriptor.get("project_id") != project_id:
        raise DraftUnavailable("draft descriptor does not match this project")
    return descriptor


async def _latest_check(service, project_id: str, entry: ViewDraftEntry) -> dict[str, Any] | None:
    if not entry.check_count:
        return None
    raw = await service._assets.read_limited(
        _prefix(project_id) + f"{entry.draft_id}.check-{entry.check_count}.json", _DESCRIPTOR_LIMIT)
    return json.loads(raw)


def _passed(check: dict[str, Any] | None, descriptor: dict[str, Any]) -> bool:
    return bool(check and check.get("facing") and check.get("equipment") and check.get("identity")
                and check.get("card_digest") == descriptor["card_digest"]
                and check.get("pick_revision") == descriptor["pick_revision"])


async def describe(service, manifest, draft_id: str) -> dict[str, Any]:
    from .service import project_file_path

    entry = _entry(manifest, draft_id)
    descriptor = await _descriptor(service, manifest.project_id, draft_id)
    check = await _latest_check(service, manifest.project_id, entry)
    discarded = entry.discarded_at is not None
    bound = _bound_views(manifest, draft_id)
    stale = _lineage_problem(manifest, entry, descriptor)
    return {**descriptor, "image_path": project_file_path(manifest.project_id, descriptor["image"]["key"]),
            "check": check, "checked": _passed(check, descriptor), "discarded": discarded,
            "bound_view_ids": bound, "stale_reason": stale,
            "bindable": _passed(check, descriptor) and not discarded and not bound and stale is None}


def _lineage_problem(manifest, entry: ViewDraftEntry, descriptor: dict[str, Any]) -> str | None:
    """Why the draft no longer matches its turnaround, picks or source, or None."""
    from .turnarounds import _take

    try:
        take = _take(manifest, entry.turnaround_id)
    except ValueError:
        return "the draft's turnaround is no longer in this project"
    if (take.candidate is None or not take.stored_bytes_recorded or take.candidate.sha256 != descriptor["video_sha256"]
            or take.candidate.candidate_id != descriptor["candidate_id"]):
        return "the draft's turnaround video no longer matches"
    if take.pick_set is None or take.pick_set.revision != descriptor["pick_revision"]:
        return "the saved picks changed since this draft was made; make it again from them"
    if descriptor["image"]["key"] != _prefix(manifest.project_id) + f"{entry.draft_id}.png":
        return "draft image lineage does not match this project"
    view = next((v for v in manifest.views if str(v.view_id) == descriptor["source_view_id"]), None)
    if view is None or view.reference is None:
        # A removed source fences the draft, as it fences a late turnaround; only a
        # replaced reference is something the person may confirm.
        return "the direction this draft was turned from has been removed"
    return None


def _bound_views(manifest, draft_id: str) -> list[str]:
    return [str(v.view_id) for v in manifest.views if v.reference and v.reference.revision_id == draft_id]


async def list_drafts(service, project_id: str, actor_id: str) -> list[dict[str, Any]]:
    current = await service._require_owned_project(project_id, actor_id)
    return [await describe(service, current.manifest, d.draft_id) for d in current.manifest.view_drafts]


async def check(service, project_id: str, draft_id: str, expected_revision: int, facing: bool,
                equipment: bool, identity: bool, actor_id: str) -> dict[str, Any]:
    """Record the person's three-box check as a new receipt, with the card they saw.

    If the manifest update loses a race, the receipt it wrote stays as an orphan
    and the retry takes the next number; a lost race never blocks later checks.
    """
    from .store import RevisionConflict

    current = await service._require_owned_project(project_id, actor_id, expected_revision)
    # Every receipt is stored and counted, so checks respect the storage limit too.
    await service._require_storage_headroom(actor_id)
    descriptor = await _descriptor(service, project_id, draft_id)
    checked_at = datetime.now(UTC).isoformat()
    while True:
        entry = _entry(current.manifest, draft_id)
        if entry.discarded_at is not None:
            raise DraftUnavailable("this draft was discarded")
        number = entry.check_count + 1
        while True:
            receipt = {"draft_id": draft_id, "number": number, "actor_id": actor_id, "checked_at": checked_at,
                       "facing": facing, "equipment": equipment, "identity": identity,
                       "pick_revision": descriptor["pick_revision"], "placement_card": descriptor["placement_card"],
                       "card_intent": descriptor["card_intent"], "cards_version": descriptor["cards_version"],
                       "card_digest": descriptor["card_digest"]}
            if await _write(service, current.manifest, _prefix(project_id) + f"{draft_id}.check-{number}.json",
                            json.dumps(receipt, separators=(",", ":")).encode(), "application/json"):
                break
            number += 1
        drafts = tuple(d.model_copy(update={"check_count": number}) if d.draft_id == draft_id else d
                       for d in current.manifest.view_drafts)
        try:
            await service._store.append(project_id, current.revision,
                                        current.manifest.model_copy(update={"view_drafts": drafts}))
            break
        except RevisionConflict:
            current = await service._require_owned_project(project_id, actor_id)
    current = await service._require_owned_project(project_id, actor_id)
    return await describe(service, current.manifest, draft_id)


async def discard(service, project_id: str, draft_id: str, expected_revision: int, actor_id: str) -> dict[str, Any]:
    """Discard the draft. It can never be bound afterwards, even by an older plan.

    The mark is a manifest change, so it shares setup apply's revision fence: an
    apply that read the draft as live cannot commit after it. A draft that is a
    direction's reference cannot be discarded until that reference is replaced.
    """
    from .store import RevisionConflict

    current = await service._require_owned_project(project_id, actor_id)
    # A discard already committed answers again, even for the caller's original
    # revision: a retry after a lost response recovers instead of conflicting.
    if _entry(current.manifest, draft_id).discarded_at is not None:
        return await describe(service, current.manifest, draft_id)
    if current.revision != expected_revision:
        raise RevisionConflict(current.revision)
    discarded_at = datetime.now(UTC)
    while True:
        entry = _entry(current.manifest, draft_id)
        if entry.discarded_at is not None:
            return await describe(service, current.manifest, draft_id)
        if _bound_views(current.manifest, draft_id):
            raise DraftUnavailable("this draft is a direction's reference; replace that reference before discarding it")
        drafts = tuple(d.model_copy(update={"discarded_at": discarded_at}) if d.draft_id == draft_id else d
                       for d in current.manifest.view_drafts)
        try:
            result = await service._store.append(project_id, current.revision,
                                                 current.manifest.model_copy(update={"view_drafts": drafts}))
            break
        except RevisionConflict:
            current = await service._require_owned_project(project_id, actor_id)
    tombstone = {"draft_id": draft_id, "actor_id": actor_id, "discarded_at": discarded_at.isoformat(),
                 "revision": result.revision}
    try:
        await _write(service, result.manifest, _prefix(project_id) + f"{draft_id}.discarded.json",
                     json.dumps(tombstone, separators=(",", ":")).encode(), "application/json")
    except AssetStorageUnavailable:
        # The manifest mark is what fences binding; the receipt is its audit copy.
        logger.warning("could not write the discard receipt for %s", draft_id)
    return await describe(service, result.manifest, draft_id)


async def resolve_draft(service, manifest, draft_id: str, heading: ViewId | None):
    """The setup-binding branch: a checked, undiscarded draft of this heading, revalidated.

    Returns the reference to bind and whether its turnaround's source has since
    been replaced (which the plan must confirm).
    """
    from .setup import SetupConflict

    try:
        entry = _entry(manifest, draft_id)
        descriptor = await _descriptor(service, manifest.project_id, draft_id)
        latest = await _latest_check(service, manifest.project_id, entry)
    except (AssetNotFound, AssetTooLarge, ValueError) as exc:
        raise SetupConflict("draft does not belong to this project") from exc
    if heading is None or descriptor["heading"] != heading.value or entry.heading is not heading:
        raise SetupConflict("a draft binds only to the direction with its own heading")
    if entry.discarded_at is not None:
        raise SetupConflict("this draft was discarded")
    if not _passed(latest, descriptor):
        raise SetupConflict("a draft is bound only after its facing, equipment and identity check")
    problem = _lineage_problem(manifest, entry, descriptor)
    if problem is not None:
        raise SetupConflict(problem)
    image = descriptor["image"]
    view = next(v for v in manifest.views if str(v.view_id) == descriptor["source_view_id"])
    superseded = view.reference.revision_id != descriptor["source_reference"]["revision_id"]
    return ViewReference(revision_id=draft_id, key=image["key"], sha256=image["sha256"],
                         width=image["width"], height=image["height"]), superseded


async def record_bound(service, project_id: str, before, result) -> None:
    """After apply: a receipt for each draft that became a direction's reference.

    Keyed by the applied revision, so binding the same draft to the same view
    again leaves a second receipt. The setup has already been committed, so a
    receipt that cannot be written is logged rather than failing the apply.
    """
    previous = {(str(v.view_id), v.reference.revision_id) for v in before.views if v.reference}
    for view in result.manifest.views:
        if not (view.reference and DRAFT_ID.match(view.reference.revision_id)):
            continue
        if (str(view.view_id), view.reference.revision_id) in previous:
            continue
        receipt = {"draft_id": view.reference.revision_id, "view_id": str(view.view_id),
                   "revision": result.revision, "bound_at": datetime.now(UTC).isoformat()}
        try:
            await _write(service, result.manifest,
                         _prefix(project_id) + f"{view.reference.revision_id}.bound-{view.view_id}-r{result.revision}.json",
                         json.dumps(receipt, separators=(",", ":")).encode(), "application/json")
        except Exception:
            logger.exception("could not write the bind receipt for %s at revision %s",
                             view.reference.revision_id, result.revision)

"""Atomic, actor-bound reconciliation of the explicitly requested sprite matrix.

Only revision IDs are accepted as reference bindings. Storage keys are resolved from
project lineage and their bytes are revalidated both at preview and apply.
"""

import io
import json
import re
import secrets
from datetime import UTC, datetime
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from pydantic import Field, model_validator

from magic_video_editor.contracts import AssetNotFound, AssetTooLarge, JobStatus

from .models import (
    Approval,
    ApprovalState,
    CharacterView,
    FrozenModel,
    MotionSpecification,
    MotionState,
    SpriteLayout,
    SubjectAssessment,
    SubjectRevision,
    SubjectRevisionSource,
    ViewId,
    ViewReference,
    view_matches_layout,
)


class SetupConflict(ValueError):
    pass


class SetupBlocked(ValueError):
    pass


class SetupConfirmationRequired(ValueError):
    pass


class SetupReference(FrozenModel):
    revision_id: str = Field(min_length=1, max_length=255)


class SetupView(FrozenModel):
    view_id: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$"
    )
    heading: ViewId | None = None
    name: str = Field(min_length=1, max_length=200)
    reference: SetupReference | None = None
    motions: tuple[MotionSpecification, ...] = Field(default=(), max_length=32)


class ProjectSetup(FrozenModel):
    schema_version: Literal[1] = 1
    sprite_layout: SpriteLayout
    views: tuple[SetupView, ...] = Field(default=(), max_length=8)
    identity_reference: SetupReference | None = None

    @model_validator(mode="after")
    def valid_matrix(self):
        if self.sprite_layout is SpriteLayout.UNASSIGNED:
            if self.views:
                raise ValueError("unassigned setup cannot contain views")
            return self
        ids = [v.view_id for v in self.views]
        headings = [v.heading for v in self.views]
        if len(set(ids)) != len(ids) or len(set(headings)) != len(headings):
            raise ValueError(
                "view identities and declared headings must be unique; explicitly remove a colliding view"
            )
        if self.sprite_layout is SpriteLayout.SINGLE_VIEW:
            if len(self.views) > 1 or any(
                v.view_id != "single" or v.heading is not None for v in self.views
            ):
                raise ValueError(
                    "single_view requires neutral single identity without a heading"
                )
        elif any(
            v.heading is None or not view_matches_layout(v.heading, self.sprite_layout)
            for v in self.views
        ):
            raise ValueError("declared heading does not match sprite layout")
        motion_ids = [m.motion_id for v in self.views for m in v.motions]
        if len(set(motion_ids)) != len(motion_ids):
            raise ValueError("motion identities must be unique across the project")
        for view in self.views:
            slots = [m.semantic_slot or m.preset or m.motion_id for m in view.motions]
            if len(set(slots)) != len(slots):
                raise ValueError("each view requires unique semantic export slots")
        return self


class SetupImpact(FrozenModel):
    impact_id: str
    kind: str
    view_id: str | None = None
    motion_id: str | None = None
    name: str
    candidate_count: int = 0
    revision_count: int = 0
    reference_count: int = 0
    approval_count: int = 0
    requires_confirmation: bool = False


class ProjectSetupPlan(FrozenModel):
    plan_token: str
    expected_revision: int
    desired_setup: ProjectSetup
    impacts: tuple[SetupImpact, ...] = ()
    blocked_operations: tuple[str, ...] = ()


def current_setup(manifest):
    return ProjectSetup(
        sprite_layout=manifest.sprite_layout,
        views=tuple(
            SetupView(
                view_id=str(v.view_id),
                heading=v.heading
                if v.setup_version
                else (None if str(v.view_id) == "single" else v.view_id),
                name=v.name,
                reference=SetupReference(revision_id=v.reference.revision_id)
                if v.reference
                else None,
                motions=tuple(
                    m.specification
                    for m in manifest.motions
                    if m.specification.view_id == v.view_id
                ),
            )
            for v in manifest.views
        ),
        identity_reference=SetupReference(
            revision_id=manifest.identity_reference.revision_id
        )
        if manifest.identity_reference
        else SetupReference(revision_id=manifest.subject.current_revision.revision_id)
        if manifest.subject.current_revision
        else None,
    )


async def resolve_reference(service, manifest, binding, heading=None, superseded=None, view_id=None):
    """`heading` and `view_id` are the direction being bound (None for the identity
    anchor); draft IDs whose turnaround source has since changed are added to
    `superseded`. A draft is revalidated unless it is already this same view's
    reference."""
    if binding is None:
        return None
    subject = next(
        (r for r in manifest.subject.revisions if r.revision_id == binding.revision_id),
        None,
    )
    reference = next(
        (
            v.reference
            for v in manifest.views
            if v.reference and v.reference.revision_id == binding.revision_id
        ),
        None,
    )
    is_draft = re.fullmatch(r"draft-[0-9a-f]{32}", binding.revision_id) is not None
    kept = is_draft and any(
        str(v.view_id) == str(view_id) and v.heading == heading
        and v.reference and v.reference.revision_id == binding.revision_id
        for v in manifest.views
    )
    if subject is not None:
        key, digest = subject.artifact_key, subject.sha256
    elif reference is not None and (not is_draft or kept):
        key, digest = reference.key, reference.sha256
    elif is_draft:
        from .view_drafts import resolve_draft

        drafted, changed = await resolve_draft(service, manifest, binding.revision_id, heading)
        if changed and superseded is not None:
            superseded.append(binding.revision_id)
        key, digest = drafted.key, drafted.sha256
    else:
        if not re.fullmatch(r"setup-[0-9a-f]{32}", binding.revision_id):
            raise SetupConflict("reference revision does not belong to this project")
        try:
            descriptor = await service._assets.read_limited(
                f"sources/studio/{manifest.project_id}/setup-references/{binding.revision_id}.json",
                4096,
            )
            reference = ViewReference.model_validate_json(descriptor)
        except (AssetNotFound, AssetTooLarge, ValueError) as exc:
            raise SetupConflict(
                "reference revision does not belong to this project"
            ) from exc
        expected_key = (
            f"sources/studio/{manifest.project_id}/subjects/{binding.revision_id}.image"
        )
        if (
            reference.revision_id != binding.revision_id
            or reference.key != expected_key
        ):
            raise SetupConflict("staged reference lineage does not match this project")
        key, digest = reference.key, reference.sha256
    try:
        from .service import MAX_SUBJECT_REFERENCE_BYTES

        content = await service._assets.read_limited(key, MAX_SUBJECT_REFERENCE_BYTES)
        if sha256(content).hexdigest() != digest:
            raise SetupConflict("reference bytes do not match their recorded hash")
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            if (
                image.format not in {"PNG", "JPEG", "WEBP"}
                or getattr(image, "n_frames", 1) != 1
            ):
                raise SetupConflict("reference must be one PNG, JPEG or WebP image")
            if (
                not 1 <= width <= 8192
                or not 1 <= height <= 8192
                or width * height > 16_777_216
            ):
                raise SetupConflict("reference image dimensions exceed limits")
            image.load()
    except (
        AssetNotFound,
        AssetTooLarge,
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
    ) as exc:
        raise SetupConflict("reference is not a decodable image") from exc
    return ViewReference(
        revision_id=binding.revision_id,
        key=key,
        sha256=digest,
        width=width,
        height=height,
    )


async def reconcile(service, current, desired):
    manifest = current.manifest
    old_views = {str(v.view_id): v for v in manifest.views}
    old_motions = {m.motion_id: m for m in manifest.motions}
    identity = await resolve_reference(service, manifest, desired.identity_reference)
    subject = manifest.subject.current_revision
    if (
        manifest.views or manifest.identity_reference
    ) and desired.identity_reference != current_setup(manifest).identity_reference:
        raise SetupConflict("setup cannot replace the established identity anchor")
    views, motions, impacts = [], [], []

    def impact(kind, view_id, motion=None, view=None):
        # Frame edits preserve parent lineage even when their inherited recipe
        # is absent. Count each saved candidate exactly once as take or revision.
        revisions = (
            sum(
                bool(c.recipe or c.parent_revision_ids or c.source.value == "derived")
                for c in motion.candidates
            )
            if motion
            else 0
        )
        count = len(motion.candidates) - revisions if motion else 0
        references = int(view is not None and view.reference is not None)
        # Candidate selection is not an approval. Project signoff is counted
        # once below, rather than once per selected take.
        approvals = 0
        impacts.append(
            SetupImpact(
                impact_id=f"{kind}:{view_id}:{motion.motion_id if motion else ''}",
                kind=kind,
                view_id=view_id,
                motion_id=motion.motion_id if motion else None,
                name=motion.specification.name
                if motion
                else view.name
                if view
                else view_id,
                candidate_count=count,
                revision_count=revisions,
                reference_count=references,
                approval_count=approvals,
                requires_confirmation=bool(count or revisions or references)
                and kind != "add_view",
            )
        )

    superseded = []
    for v in desired.views:
        reference = await resolve_reference(service, manifest, v.reference, v.heading, superseded, v.view_id)
        old = old_views.get(v.view_id)
        preserve_legacy = (
            old is not None and old.setup_version is None and old.view_id == v.heading
        )
        view = (
            old.model_copy(update={"name": v.name, "reference": reference})
            if preserve_legacy
            else CharacterView(
                view_id=v.view_id,
                heading=v.heading,
                setup_version=1,
                name=v.name,
                reference=reference,
            )
        )
        views.append(view)
        if old is None:
            impact("add_view", v.view_id)
        elif (old.heading if old.setup_version else old.view_id) != v.heading:
            impact("change_heading", v.view_id, view=old)
        if old and old.reference != reference:
            impact("replace_reference", v.view_id, view=old)
        for spec in v.motions:
            if spec.motion_id in manifest.removed_motion_ids:
                raise SetupConflict(
                    "removed motion identity cannot be reused; choose a new motion_id"
                )
            spec = spec.model_copy(
                update={
                    "view_id": v.view_id,
                    "view_heading": v.heading if view.setup_version else None,
                    "setup_version": view.setup_version,
                    "view_reference": reference,
                }
            )
            from .service import _compile_preset_specification, _subject_display_name

            spec = _compile_preset_specification(spec, _subject_display_name(manifest))
            previous = old_motions.get(spec.motion_id)
            if previous and str(previous.specification.view_id) != v.view_id:
                raise SetupConflict(
                    "motion identity cannot move between views; remove it and use a new identity"
                )
            if previous is None:
                impacts.append(
                    SetupImpact(
                        impact_id=f"add_motion:{spec.motion_id}",
                        kind="add_motion",
                        view_id=v.view_id,
                        motion_id=spec.motion_id,
                        name=spec.name,
                    )
                )
                motions.append(MotionState(specification=spec))
            else:
                if previous.specification != spec:
                    impact("change_motion", v.view_id, motion=previous)
                motions.append(previous.model_copy(update={"specification": spec}))
    # Legacy viewless projects keep their existing motion path. The setup
    # schema does not reinterpret unassigned motions as a neutral single view.
    if desired.sprite_layout is SpriteLayout.UNASSIGNED:
        motions = list(manifest.motions)
    requested_ids = {m.motion_id for m in motions}
    for m in manifest.motions:
        if m.motion_id not in requested_ids:
            impact("remove_motion", str(m.specification.view_id), motion=m)
    for v in manifest.views:
        if str(v.view_id) not in {v.view_id for v in desired.views}:
            impact("remove_view", str(v.view_id), view=v)
    if desired.sprite_layout != manifest.sprite_layout:
        impacts.append(
            SetupImpact(
                impact_id="change_layout",
                kind="change_layout",
                name=desired.sprite_layout.value,
            )
        )
    removed_ids = tuple(
        sorted(set(manifest.removed_motion_ids) | (set(old_motions) - requested_ids))
    )
    updates = {
        "sprite_layout": desired.sprite_layout,
        "views": tuple(views),
        "motions": tuple(motions),
        "removed_motion_ids": removed_ids,
    }
    if (
        identity is not None
        and desired.sprite_layout is not SpriteLayout.UNASSIGNED
        and (manifest.identity_reference is not None or not manifest.views)
    ):
        updates["identity_reference"] = identity
    if (
        identity is not None
        and manifest.identity_reference is None
        and not manifest.views
        and desired.sprite_layout is not SpriteLayout.UNASSIGNED
    ):
        if subject is None or subject.revision_id != identity.revision_id:
            revision = SubjectRevision(
                revision_id=identity.revision_id,
                artifact_key=identity.key,
                sha256=identity.sha256,
                width=identity.width,
                height=identity.height,
                source=SubjectRevisionSource.UPLOADED,
                created_at=datetime.now(UTC),
            )
            subject_state = manifest.subject.with_revision(revision)
        else:
            subject_state = manifest.subject
        identity_bytes = await service._assets.read_limited(identity.key, 20_000_000)
        if sha256(identity_bytes).hexdigest() != identity.sha256:
            raise SetupConflict("identity bytes changed during verification")
        with Image.open(io.BytesIO(identity_bytes)) as image:
            alpha = image.convert("RGBA").getchannel("A")
            occupancy = sum(alpha.histogram()[1:]) / float(
                identity.width * identity.height
            )
        if occupancy == 0:
            raise SetupConflict("identity artwork must contain visible pixels")
        # This policy verifies the supplied identity bytes and visibility only.
        # It makes no cutout, game-readiness or motion-quality claim.
        assessment = SubjectAssessment(
            assessment_id=sha256(
                f"setup-identity-v1:{identity.sha256}".encode()
            ).hexdigest(),
            policy_version="setup-identity-v1",
            artifact_sha256=identity.sha256,
            width=identity.width,
            height=identity.height,
            occupancy_ratio=occupancy,
        )
        updates["subject"] = subject_state.model_copy(update={"assessment": assessment})
    changed = manifest.model_copy(update=updates)
    if "subject" in updates:
        from .manifest import approve_subject

        changed = approve_subject(
            changed,
            revision_id=identity.revision_id,
            assessment_id=assessment.assessment_id,
            approver_id=manifest.owner_id,
            policy_version=assessment.policy_version,
        )
    # Validate the entire resulting manifest before a single durable append.
    changed = type(manifest).model_validate(changed.model_dump())
    for draft_id in superseded:
        # The draft was made from a reference that has since been replaced.
        impacts.append(
            SetupImpact(
                impact_id=f"draft_source_superseded:{draft_id}",
                kind="draft_source_superseded",
                name="Draft from a replaced source",
                requires_confirmation=True,
            )
        )
    if impacts:
        if manifest.set_approval.state in {
            ApprovalState.APPROVED,
            ApprovalState.WAIVED,
        }:
            impacts.append(
                SetupImpact(
                    impact_id="invalidate_set_approval",
                    kind="invalidate_set_approval",
                    name="Project signoff",
                    approval_count=1,
                    requires_confirmation=True,
                )
            )
        changed = changed.model_copy(
            update={
                "set_approval": Approval(),
                "set_assessment_id": None,
                "set_assessment_digest": None,
                "set_assessment": None,
            }
        )
    return changed, tuple(impacts)


async def blocked_operations(service, project_id, manifest, impacts):
    if service._jobs is None:
        return ()
    # Completed but unsettled jobs also block: structural edits cannot race a
    # delayed settlement or silently discard the operator's pending output.
    from .service import is_settled

    affected_views = {
        i.view_id
        for i in impacts
        if i.kind in {"remove_view", "change_heading", "replace_reference"}
    }
    affected_motions = {
        i.motion_id for i in impacts if i.kind in {"remove_motion", "change_motion"}
    }
    affected_motions.update(
        m.motion_id
        for m in manifest.motions
        if str(m.specification.view_id) in affected_views
    )
    records = []
    registered_operations = set()
    cursor = None
    from magic_video_editor.contracts import job_activity_cursor

    while True:
        page = await service._jobs.list_for_project(
            project_id, cursor=cursor, limit=100
        )
        registered_operations.update(j.manifest_operation_id for j in page)
        records.extend(
            j.job_id
            for j in page
            if j.payload.get("motion_id") in affected_motions
            and (
                j.status in {JobStatus.QUEUED, JobStatus.PROCESSING}
                or j.status is JobStatus.COMPLETED
                and not is_settled(j, manifest)
            )
        )
        if len(page) < 100:
            records.extend(
                f"dispatch:{r.operation_id}:{r.motion_id}:recovery_required"
                for r in manifest.dispatch_reservations
                if r.operation_id not in registered_operations
                and r.motion_id in affected_motions
            )
            return tuple(records)
        cursor = job_activity_cursor(page[-1])


async def preview(service, project_id, expected_revision, desired, actor_id):
    current = await service._require_owned_project(
        project_id, actor_id, expected_revision
    )
    desired = ProjectSetup.model_validate(desired)
    _, impacts = await reconcile(service, current, desired)
    blocked = (
        await blocked_operations(service, project_id, current.manifest, impacts)
        if impacts
        else ()
    )
    token = secrets.token_hex(32)
    descriptor = {
        "project_id": project_id,
        "actor_id": actor_id,
        "expected_revision": expected_revision,
        "desired_setup": desired.model_dump(mode="json"),
        "impacts": [i.model_dump(mode="json") for i in impacts],
    }
    await service._assets.write_once(
        f"sources/studio/{project_id}/setup-plans/{token}.json",
        json.dumps(descriptor, separators=(",", ":")).encode(),
        "application/json",
    )
    return ProjectSetupPlan(
        plan_token=token,
        expected_revision=expected_revision,
        desired_setup=desired,
        impacts=impacts,
        blocked_operations=blocked,
    )


async def apply(service, project_id, expected_revision, token, confirmed, actor_id):
    # Authorize before reading any project-scoped plan bytes. Opaque immutable
    # descriptors survive API replica changes and process restarts.
    current = await service._require_owned_project(
        project_id, actor_id, expected_revision
    )
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise SetupConflict("invalid setup plan token; preview again")
    try:
        raw = await service._assets.read_limited(
            f"sources/studio/{project_id}/setup-plans/{token}.json", 10 * 1024 * 1024
        )
        binding = json.loads(raw)
        if (
            binding["project_id"],
            binding["actor_id"],
            binding["expected_revision"],
        ) != (project_id, actor_id, expected_revision):
            raise SetupConflict(
                "setup plan is invalid for this actor, project or revision; preview again"
            )
        desired = ProjectSetup.model_validate(binding["desired_setup"])
    except (AssetNotFound, AssetTooLarge, ValueError, KeyError, TypeError) as exc:
        raise SetupConflict(
            "setup plan is invalid for this actor, project or revision; preview again"
        ) from exc
    manifest, impacts = await reconcile(service, current, desired)
    if impacts and await blocked_operations(
        service, project_id, current.manifest, impacts
    ):
        raise SetupBlocked("settle active or completed operations before editing setup")
    required = {i.impact_id for i in impacts if i.requires_confirmation}
    if not required.issubset(set(confirmed)):
        raise SetupConfirmationRequired(
            "confirm exact impacts: " + ", ".join(sorted(required))
        )
    if manifest == current.manifest:
        return current
    result = await service._store.append(project_id, current.revision, manifest)
    from .view_drafts import record_bound

    await record_bound(service, project_id, current.manifest, result)
    return result


async def stage_reference(service, project_id, content, content_type, actor_id):
    await service._require_owned_project(project_id, actor_id)
    await service._require_storage_headroom(actor_id)
    if (
        content_type not in {"image/png", "image/jpeg", "image/webp"}
        or len(content) > 10 * 1024 * 1024
    ):
        raise SetupConflict("reference requires PNG, JPEG or WebP up to 10 MB")
    try:
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            if (
                image.format not in {"PNG", "JPEG", "WEBP"}
                or getattr(image, "n_frames", 1) != 1
                or not 1 <= width <= 8192
                or not 1 <= height <= 8192
                or width * height > 16_777_216
            ):
                raise SetupConflict(
                    "reference must be a single image within dimension limits"
                )
            image.load()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise SetupConflict("reference is not a decodable image") from exc
    revision_id = "setup-" + uuid4().hex
    key = f"sources/studio/{project_id}/subjects/{revision_id}.image"
    reference = ViewReference(
        revision_id=revision_id,
        key=key,
        sha256=sha256(content).hexdigest(),
        width=width,
        height=height,
    )
    await service._assets.write_once(key, content, content_type)
    await service._assets.write_once(
        f"sources/studio/{project_id}/setup-references/{revision_id}.json",
        reference.model_dump_json().encode(),
        "application/json",
    )
    return reference

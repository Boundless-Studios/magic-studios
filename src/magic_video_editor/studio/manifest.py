"""Pure transitions for animation project manifests."""

from hashlib import sha256

from magic_video_editor.studio.models import (
    AnimationProjectManifest,
    Approval,
    ApprovalState,
    CompositionRevision,
    ExternalSubject,
    MotionCandidate,
    MotionKind,
    MotionSpecification,
    MotionState,
    PromptSource,
    ReleaseDelivery,
    RevisionLifecycle,
    SubjectAssessment,
    SubjectRevision,
)
from magic_video_editor.studio.presets import PRESET_DEFINITIONS


class DuplicateMotionIds(ValueError):
    """Raised by `add_motions` when the batch collides with an existing motion_id."""

    def __init__(self, motion_ids: tuple[str, ...]) -> None:
        self.motion_ids = motion_ids
        super().__init__(f"motions already exist: {list(motion_ids)}")


def add_motions(
    manifest: AnimationProjectManifest,
    specifications: tuple[MotionSpecification, ...],
) -> AnimationProjectManifest:
    """Add many motions in one revision (T6). Pure: no dispatch, no I/O.

    Rejects the whole batch, wholesale, when any motion_id already exists or repeats
    within the batch itself -- a partial add would leave the caller unsure which of
    "the recommended set" actually landed.
    """
    existing_ids = {motion.motion_id for motion in manifest.motions}
    seen: set[str] = set()
    duplicates: list[str] = []
    for specification in specifications:
        if specification.motion_id in existing_ids or specification.motion_id in seen:
            duplicates.append(specification.motion_id)
        seen.add(specification.motion_id)
    if duplicates:
        raise DuplicateMotionIds(tuple(duplicates))
    updated = manifest
    for specification in specifications:
        updated = updated.with_motion(specification)
    return updated


def add_motion_candidate(
    manifest: AnimationProjectManifest, *, motion_id: str, candidate: MotionCandidate
) -> AnimationProjectManifest:
    """Append a generated candidate without making a creative selection."""
    for motion in manifest.motions:
        if motion.motion_id == motion_id:
            if any(
                item.candidate_id == candidate.candidate_id
                for item in motion.candidates
            ):
                return manifest
            updated = motion.model_copy(
                update={"candidates": (*motion.candidates, candidate)}
            )
            return manifest.model_copy(
                update={
                    "motions": tuple(
                        updated if item.motion_id == motion_id else item
                        for item in manifest.motions
                    ),
                    "set_approval": Approval(),
                    "set_assessment_id": None,
                    "set_assessment_digest": None,
                    "set_assessment": None,
                }
            )
    raise ValueError("motion is not present in the manifest")


def add_derived_motion_candidate(
    manifest: AnimationProjectManifest, *, motion_id: str, candidate: MotionCandidate
) -> AnimationProjectManifest:
    """Append and select a non-destructive candidate revision."""
    appended = add_motion_candidate(manifest, motion_id=motion_id, candidate=candidate)
    return select_motion_candidate(
        appended, motion_id=motion_id, candidate_id=candidate.candidate_id
    )


def update_motion_specification(
    manifest: AnimationProjectManifest,
    *,
    motion_id: str,
    specification: MotionSpecification,
) -> AnimationProjectManifest:
    """Replace a motion's specification (references/style/script, T17) in place, preserving its
    candidates and selection -- editing the direction does not discard prior generations."""
    for motion in manifest.motions:
        if motion.motion_id != motion_id:
            continue
        if specification.motion_id != motion_id:
            raise ValueError("motion specification id must match the target motion")
        updated = motion.model_copy(update={"specification": specification})
        motions = tuple(
            updated if item.motion_id == motion_id else item
            for item in manifest.motions
        )
        return manifest.model_copy(update={"motions": motions})
    raise ValueError("motion is not present in the manifest")


def add_composition(
    manifest: AnimationProjectManifest, *, revision: CompositionRevision
) -> AnimationProjectManifest:
    """Settle a newly composed opening-frame still, superseding whatever was
    current (T15). Pure: the caller has already validated subject lineage."""
    if any(
        item.revision_id == revision.revision_id
        for item in manifest.composition.revisions
    ):
        return manifest
    return manifest.model_copy(
        update={"composition": manifest.composition.with_composition(revision)}
    )


def approve_composition(
    manifest: AnimationProjectManifest, *, revision_id: str
) -> AnimationProjectManifest:
    """Pin the current composition revision as approved (T15)."""
    return manifest.model_copy(
        update={"composition": manifest.composition.with_approval(revision_id)}
    )


def set_external_subject(
    manifest: AnimationProjectManifest, *, external_subject: ExternalSubject | None
) -> AnimationProjectManifest:
    """Bind (or clear) the project's external target identity (T22).

    A pure re-tag: unlike `with_subject`/`with_motion` this touches no
    approval, candidate, or revision, so setting it never invalidates the
    subject or set approvals -- the operator can point a project at a target
    character at any point in the workflow without redoing signoff.
    """
    return manifest.model_copy(update={"external_subject": external_subject})


def record_release_delivery(
    manifest: AnimationProjectManifest, *, delivery: ReleaseDelivery
) -> AnimationProjectManifest:
    """Append one target delivery attempt (Part B). Pure append: a retry adds a
    new entry rather than replacing the last one, so `manifest.release_deliveries`
    is the full history for a release, not just its current status -- callers
    read `latest_release_delivery` for "what is true now"."""
    return manifest.model_copy(
        update={"release_deliveries": (*manifest.release_deliveries, delivery)}
    )


def latest_release_delivery(
    manifest: AnimationProjectManifest, *, release_id: str
) -> ReleaseDelivery | None:
    """The most recent delivery attempt recorded for `release_id`, or `None`
    if none was ever recorded (delivery not configured, or not a bound
    release)."""
    for delivery in reversed(manifest.release_deliveries):
        if delivery.release_id == release_id:
            return delivery
    return None


def deliveries_for(
    manifest: AnimationProjectManifest, *, release_id: str
) -> tuple[ReleaseDelivery, ...]:
    """Every delivery attempt recorded for `release_id`, oldest first -- the
    full history a release's response exposes as `deliveries`."""
    return tuple(
        delivery
        for delivery in manifest.release_deliveries
        if delivery.release_id == release_id
    )


def recompile_preset_motion_prompts(
    manifest: AnimationProjectManifest, *, subject_name: str
) -> AnimationProjectManifest:
    """Recompile every still-templated preset motion's prompt against
    `subject_name` (T16). Binding or updating the external subject changes
    what `{subject_name}` should read in each preset's prompt template, but
    a preset motion's prompt was compiled once, at add time, and otherwise
    only changes through an explicit edit -- so without this, generations
    keep naming the project or a stale external subject after the binding
    changes.

    Pure and selective: only `MotionKind.PRESET` motions whose
    `prompt_source` is still `PromptSource.PRESET` (never hand-edited) are
    touched. A motion the operator deliberately gave a custom prompt for
    (`prompt_source == PromptSource.CUSTOM`) is left exactly as authored.
    """
    name = (subject_name or "").strip() or "The subject"
    motions: list[MotionState] = []
    changed = False
    for motion in manifest.motions:
        specification = motion.specification
        if (
            specification.kind is not MotionKind.PRESET
            or specification.prompt_source is not PromptSource.PRESET
            or specification.preset is None
        ):
            motions.append(motion)
            continue
        definition = PRESET_DEFINITIONS[specification.preset]
        recompiled_prompt = definition.prompt_template.format(
            subject_name=name,
            action=(specification.action or "").strip() or definition.default_action,
        )
        if recompiled_prompt == specification.prompt:
            motions.append(motion)
            continue
        changed = True
        motions.append(
            motion.model_copy(
                update={
                    "specification": specification.model_copy(
                        update={"prompt": recompiled_prompt}
                    )
                }
            )
        )
    if not changed:
        return manifest
    return manifest.model_copy(update={"motions": tuple(motions)})


def create_project_manifest(
    project_id: str, name: str, owner_id: str | None = None
) -> AnimationProjectManifest:
    return AnimationProjectManifest(
        project_id=project_id,
        name=name,
        owner_id=owner_id,
    )


def approve_subject(
    manifest: AnimationProjectManifest,
    *,
    revision_id: str,
    assessment_id: str,
    approver_id: str,
    policy_version: str,
    waived_finding_ids: tuple[str, ...] = (),
    waiver_reason: str | None = None,
) -> AnimationProjectManifest:
    current = manifest.subject.current_revision
    if current is None or revision_id != current.revision_id:
        raise ValueError("subject approval requires the current subject revision")
    if len(set(waived_finding_ids)) != len(waived_finding_ids):
        raise ValueError("waived finding ids must be unique")
    if waived_finding_ids and not (waiver_reason and waiver_reason.strip()):
        raise ValueError("waived subject approval requires a nonempty reason")

    approval = Approval(
        state=(ApprovalState.WAIVED if waived_finding_ids else ApprovalState.APPROVED),
        assessment_id=assessment_id,
        approver_id=approver_id,
        policy_version=policy_version,
        artifact_sha256=current.sha256,
        waived_finding_ids=waived_finding_ids,
        waiver_reason=waiver_reason.strip() if waiver_reason else None,
    )
    return manifest.model_copy(
        update={
            "subject": manifest.subject.model_copy(update={"approval": approval}),
            "set_approval": Approval(),
        }
    )


def select_subject_revision(
    manifest: AnimationProjectManifest,
    *,
    revision_id: str,
    assessment: SubjectAssessment | None,
    approval: Approval,
) -> AnimationProjectManifest:
    """Make an existing subject revision current (studio-ux-flow phase 3).

    Unlike `with_subject` (used when a *new* revision is generated or uploaded),
    this never wipes composition or motion candidates -- choosing among takes
    already made is not the same event as producing a fresh one, and motion
    takes made from another subject take are left in place for the frontend to
    tag stale (`MotionCandidate.subject_revision_id`), never silently dropped.
    """
    revision = next(
        (
            item
            for item in manifest.subject.revisions
            if item.revision_id == revision_id
        ),
        None,
    )
    if revision is None:
        raise ValueError("revision is not present for this subject")
    if revision.lifecycle is RevisionLifecycle.REJECTED:
        raise ValueError("a rejected subject revision cannot be made current")
    return manifest.model_copy(
        update={
            "subject": manifest.subject.model_copy(
                update={
                    "current_revision": revision,
                    "assessment": assessment,
                    "approval": approval,
                }
            ),
            "set_approval": Approval(),
            "set_assessment_id": None,
            "set_assessment_digest": None,
            "set_assessment": None,
        }
    )


def _find_subject_revision(
    manifest: AnimationProjectManifest, revision_id: str
) -> SubjectRevision:
    revision = next(
        (
            item
            for item in manifest.subject.revisions
            if item.revision_id == revision_id
        ),
        None,
    )
    if revision is None:
        raise ValueError("revision is not present for this subject")
    return revision


def _with_subject_lifecycle(
    manifest: AnimationProjectManifest,
    *,
    revision_id: str,
    lifecycle: RevisionLifecycle,
) -> tuple[SubjectRevision, ...]:
    return tuple(
        item.model_copy(update={"lifecycle": lifecycle})
        if item.revision_id == revision_id
        else item
        for item in manifest.subject.revisions
    )


def reject_subject_revision(
    manifest: AnimationProjectManifest,
    *,
    revision_id: str,
) -> AnimationProjectManifest:
    """Take a subject revision out of the choosable set (it is never deleted).

    Motion candidates made from it keep their `subject_revision_id`; as with
    `select_subject_revision` they are left for the frontend to tag stale.
    """
    revision = _find_subject_revision(manifest, revision_id)
    if revision.lifecycle is RevisionLifecycle.REJECTED:
        raise ValueError("subject revision is already rejected")
    revisions = _with_subject_lifecycle(
        manifest, revision_id=revision_id, lifecycle=RevisionLifecycle.REJECTED
    )
    subject_update: dict[str, object] = {"revisions": revisions}
    if manifest.subject.current_revision_id == revision_id:
        fallback = next(
            (
                item
                for item in reversed(revisions)
                if item.lifecycle is not RevisionLifecycle.REJECTED
            ),
            None,
        )
        subject_update |= {
            "current_revision": fallback,
            "assessment": None,
            "approval": Approval(),
        }
    return manifest.model_copy(
        update={
            "subject": manifest.subject.model_copy(update=subject_update),
            "set_approval": Approval(),
            "set_assessment_id": None,
            "set_assessment_digest": None,
            "set_assessment": None,
        }
    )


def restore_subject_revision(
    manifest: AnimationProjectManifest,
    *,
    revision_id: str,
) -> AnimationProjectManifest:
    """Return a rejected revision to the choosable set without selecting it."""
    revision = _find_subject_revision(manifest, revision_id)
    if revision.lifecycle is not RevisionLifecycle.REJECTED:
        raise ValueError("subject revision is not rejected")
    return manifest.model_copy(
        update={
            "subject": manifest.subject.model_copy(
                update={
                    "revisions": _with_subject_lifecycle(
                        manifest,
                        revision_id=revision_id,
                        lifecycle=RevisionLifecycle.READY,
                    )
                }
            )
        }
    )


def select_motion_candidate(
    manifest: AnimationProjectManifest,
    *,
    motion_id: str,
    candidate_id: str,
) -> AnimationProjectManifest:
    for motion in manifest.motions:
        if motion.motion_id != motion_id:
            continue
        if not any(
            candidate.candidate_id == candidate_id for candidate in motion.candidates
        ):
            raise ValueError("candidate is not present for the requested motion")
        selected = motion.model_copy(update={"selected_candidate_id": candidate_id})
        motions = tuple(
            selected if candidate_motion.motion_id == motion_id else candidate_motion
            for candidate_motion in manifest.motions
        )
        return manifest.model_copy(
            update={
                "motions": motions,
                "set_approval": Approval(),
                "set_assessment_id": None,
                "set_assessment_digest": None,
                "set_assessment": None,
            }
        )
    raise ValueError("motion is not present in the manifest")


def approve_set(
    manifest: AnimationProjectManifest,
    *,
    assessment_id: str,
    approver_id: str,
    policy_version: str,
    waived_finding_ids: tuple[str, ...] = (),
    waiver_reason: str | None = None,
) -> AnimationProjectManifest:
    if len(set(waived_finding_ids)) != len(waived_finding_ids):
        raise ValueError("waived finding ids must be unique")
    if waiver_reason is not None and waiver_reason.strip() and not waived_finding_ids:
        raise ValueError("waiver reason requires waived finding ids")
    _require_current_subject_approval(manifest)
    missing = [
        motion.motion_id
        for motion in manifest.motions
        if motion.required and motion.selected_candidate is None
    ]
    if missing:
        raise ValueError(f"every required motion needs a selected candidate: {missing}")
    current_set_digest = _set_digest(manifest)
    if (
        manifest.set_assessment_id != assessment_id
        or manifest.set_assessment_digest != current_set_digest
    ):
        raise ValueError("assessment digest does not match the current set digest")
    if manifest.set_assessment is None:
        raise ValueError("set approval requires persisted quality evidence")
    if (
        policy_version != manifest.set_assessment.policy_version
        or policy_version != "set-policy-v1"
    ):
        raise ValueError("set approval policy does not match current assessment policy")
    # Quality findings are warnings, never blockers (operator decision, 2026-09-05):
    # set approval always proceeds. `waived_finding_ids` is kept only as an audit
    # record of which findings were acknowledged at approval time.
    valid_waiver_codes = {finding.code for finding in manifest.set_assessment.findings}
    if manifest.subject.assessment is not None:
        valid_waiver_codes.update(
            finding.code.value for finding in manifest.subject.assessment.findings
        )
    invalid_waivers = set(waived_finding_ids) - valid_waiver_codes
    if invalid_waivers:
        raise ValueError(
            f"waivers do not match known findings: {sorted(invalid_waivers)}"
        )
    approval = Approval(
        state=(ApprovalState.WAIVED if waived_finding_ids else ApprovalState.APPROVED),
        assessment_id=assessment_id,
        approver_id=approver_id,
        policy_version=policy_version,
        artifact_sha256=current_set_digest,
        manifest_digest=current_set_digest,
        waived_finding_ids=waived_finding_ids,
        waiver_reason=waiver_reason.strip() if waiver_reason else None,
    )
    return manifest.model_copy(update={"set_approval": approval})


def manifest_digest(manifest: AnimationProjectManifest) -> str:
    canonical = manifest.model_dump_json(exclude_none=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _set_digest(manifest: AnimationProjectManifest) -> str:
    """Digest creative set state, excluding assessment evidence, signoff, the
    external target tag (T22) and turnaround takes -- `external_subject` is metadata
    about where the set ships and a turnaround is reference acquisition, neither a
    creative change, so neither may knock a signed-off set back into "needs
    reassessment"."""
    unsigned = manifest.model_copy(
        update={
            "external_subject": None,
            "set_approval": Approval(),
            "set_assessment_id": None,
            "set_assessment_digest": None,
            "set_assessment": None,
            "releases": (),
            "release_deliveries": (),
            # Turnarounds and their drafts are reference-acquisition work, never part of the set.
            "turnarounds": (),
            "view_drafts": (),
        }
    )
    return manifest_digest(unsigned)


def set_digest(manifest: AnimationProjectManifest) -> str:
    """Return the creative-set digest used to bind assessment and signoff."""
    return _set_digest(manifest)


def summarize_manifest_change(
    previous: AnimationProjectManifest | None, current: AnimationProjectManifest
) -> str:
    """Describe, for the History page, what a manifest revision changed.

    History does not store a summary on every transition (that would touch every
    manifest-mutating function above); instead it diffs two consecutive revisions.
    """
    if previous is None:
        return "Created the project"
    return (
        _summarize_subject_change(previous, current)
        or _summarize_motion_change(previous, current)
        or _summarize_set_change(previous, current)
        or "Updated the project"
    )


def _summarize_subject_change(
    previous: AnimationProjectManifest, current: AnimationProjectManifest
) -> str | None:
    if len(current.subject.revisions) > len(previous.subject.revisions):
        return "Generated a new subject"
    became_approved = current.subject.approval.state in (
        ApprovalState.APPROVED,
        ApprovalState.WAIVED,
    )
    if (
        became_approved
        and previous.subject.approval.state != current.subject.approval.state
    ):
        return "Approved the subject"
    return None


def _summarize_motion_change(
    previous: AnimationProjectManifest, current: AnimationProjectManifest
) -> str | None:
    previous_by_id = {motion.motion_id: motion for motion in previous.motions}
    for motion in current.motions:
        before = previous_by_id.get(motion.motion_id)
        summary = (
            f"Added motion {motion.specification.name}"
            if before is None
            else _summarize_existing_motion_change(before, motion)
        )
        if summary is not None:
            return summary
    return None


def _summarize_existing_motion_change(
    before: MotionState, motion: MotionState
) -> str | None:
    name = motion.specification.name
    if len(motion.candidates) > len(before.candidates):
        newest = motion.candidates[-1]
        return (
            f"Saved a frame revision for {name}"
            if newest.parent_revision_ids
            else f"Generated a candidate for {name}"
        )
    if motion.selected_candidate_id != before.selected_candidate_id:
        return f"Selected a candidate for {name}"
    return None


def _summarize_set_change(
    previous: AnimationProjectManifest, current: AnimationProjectManifest
) -> str | None:
    became_approved = current.set_approval.state in (
        ApprovalState.APPROVED,
        ApprovalState.WAIVED,
    )
    if became_approved and previous.set_approval.state != current.set_approval.state:
        return "Approved the animation set"
    if len(current.releases) > len(previous.releases):
        return f"Created release {current.releases[-1].name}"
    return None


def _require_current_subject_approval(manifest: AnimationProjectManifest) -> None:
    current = manifest.subject.current_revision
    if current is None:
        raise ValueError("subject must be approved before set approval")
    approval = manifest.subject.approval
    if approval.state not in {ApprovalState.APPROVED, ApprovalState.WAIVED}:
        raise ValueError("subject must be approved before set approval")
    if approval.artifact_sha256 != current.sha256:
        raise ValueError(
            "subject approval is not bound to the current subject revision"
        )
    if not all((approval.assessment_id, approval.approver_id, approval.policy_version)):
        raise ValueError("subject approval is missing required metadata")

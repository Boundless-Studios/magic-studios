import pytest
from pydantic import ValidationError

from magic_video_editor.studio.manifest import (
    add_derived_motion_candidate,
    add_motion_candidate,
    approve_set,
    approve_subject,
    create_project_manifest,
    manifest_digest,
    recompile_preset_motion_prompts,
    select_motion_candidate,
    set_digest,
    set_external_subject,
    summarize_manifest_change,
)
from magic_video_editor.studio.models import (
    AnimationProjectManifest,
    Approval,
    ApprovalState,
    ExportPackingEstimate,
    ExternalSubject,
    FindingSeverity,
    FrameSlot,
    MediaKind,
    MotionCandidate,
    MotionFrameMetadata,
    MotionGenerationSettings,
    MotionKind,
    MotionPreset,
    MotionSpecification,
    MotionState,
    PromptSource,
    QualityAssessment,
    QualityFinding,
    SubjectRevision,
)


def test_motion_candidate_preserves_bounded_frame_edits_and_generation_settings() -> None:
    metadata = MotionFrameMetadata(
        decodable=True,
        media_kind=MediaKind.SHEET,
        frame_count=4,
        width=256,
        height=64,
        constant_geometry=True,
        has_alpha=True,
        packing_estimate=ExportPackingEstimate(width=256, height=64),
    )
    settings = MotionGenerationSettings(
        preset="standard",
        steps=20,
        seed=42,
        width=512,
        height=512,
        duration_seconds=4,
        provider_parameters={"motion_strength": 0.75},
    )

    candidate = MotionCandidate(
        candidate_id="candidate-1",
        artifact_key="motions/candidate-1.png",
        sha256="a" * 64,
        metadata=metadata,
        ordered_frames=(3, 1),
        frame_duration_ms=80,
        loop_policy="loop",
        generation_settings=settings,
    )

    assert candidate.effective_ordered_frames == (
        FrameSlot(index=3, hold=1), FrameSlot(index=1, hold=1),
    )
    assert candidate.generation_settings == settings

    with pytest.raises(ValidationError, match="frame indexes"):
        MotionCandidate(
            candidate_id="candidate-2",
            artifact_key="motions/candidate-2.png",
            sha256="b" * 64,
            metadata=metadata,
            ordered_frames=(0, 4),
        )


def test_legacy_candidate_uses_measured_frame_order() -> None:
    candidate = MotionCandidate(
        candidate_id="candidate-1",
        artifact_key="motions/candidate-1.png",
        sha256="a" * 64,
        metadata=MotionFrameMetadata(
            decodable=True,
            media_kind=MediaKind.SHEET,
            frame_count=3,
            width=192,
            height=64,
            constant_geometry=True,
            has_alpha=True,
            packing_estimate=ExportPackingEstimate(width=192, height=64),
        ),
    )

    assert candidate.effective_ordered_frames == (
        FrameSlot(index=0), FrameSlot(index=1), FrameSlot(index=2),
    )


def test_ordered_frames_accepts_plain_ints_or_frame_slot_objects() -> None:
    metadata = MotionFrameMetadata(
        decodable=True,
        media_kind=MediaKind.SHEET,
        frame_count=4,
        width=256,
        height=64,
        constant_geometry=True,
        has_alpha=True,
        packing_estimate=ExportPackingEstimate(width=256, height=64),
    )

    from_ints = MotionCandidate(
        candidate_id="candidate-1",
        artifact_key="motions/candidate-1.png",
        sha256="a" * 64,
        metadata=metadata,
        ordered_frames=(2, 0, 1),
    )
    assert from_ints.ordered_frames == (
        FrameSlot(index=2, hold=1), FrameSlot(index=0, hold=1), FrameSlot(index=1, hold=1),
    )

    from_objects = MotionCandidate(
        candidate_id="candidate-2",
        artifact_key="motions/candidate-2.png",
        sha256="b" * 64,
        metadata=metadata,
        ordered_frames=({"index": 2, "hold": 3}, {"index": 0}),
    )
    assert from_objects.ordered_frames == (
        FrameSlot(index=2, hold=3), FrameSlot(index=0, hold=1),
    )
    assert sum(slot.hold for slot in from_objects.effective_ordered_frames) == 4

    with pytest.raises(ValidationError, match="less than or equal to 8"):
        MotionCandidate(
            candidate_id="candidate-3",
            artifact_key="motions/candidate-3.png",
            sha256="c" * 64,
            metadata=metadata,
            ordered_frames=({"index": 0, "hold": 9},),
        )


def test_subject_approval_is_bound_to_the_exact_revision_digest() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/project-1/source.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )

    approved = approve_subject(
        manifest.with_subject(subject),
        revision_id=subject.revision_id,
        assessment_id="assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )

    assert approved.subject.approval.state is ApprovalState.APPROVED
    assert approved.subject.approval.artifact_sha256 == "a" * 64
    assert manifest_digest(approved) != manifest_digest(manifest)


def test_replacing_subject_invalidates_subject_and_set_approval() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    first = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    second = SubjectRevision(
        revision_id="subject-2",
        artifact_key="subjects/two.png",
        sha256="b" * 64,
        width=1024,
        height=1024,
    )
    approved = approve_subject(
        manifest.with_subject(first),
        revision_id="subject-1",
        assessment_id="assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    with_candidate = approved.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            ),
            candidates=(
                MotionCandidate(
                    candidate_id="idle-1",
                    artifact_key="motions/idle-1.webm",
                    sha256="c" * 64,
                ),
            ),
        )
    )
    selected = select_motion_candidate(
        with_candidate,
        motion_id="idle",
        candidate_id="idle-1",
    )
    assessed = selected.with_set_assessment(
        "set-assessment-1",
        manifest_digest(selected),
        QualityAssessment(policy_version="set-policy-v1"),
    )
    with pytest.raises(ValueError, match="policy"):
        approve_set(
            assessed,
            assessment_id="set-assessment-1",
            approver_id="user-1",
            policy_version="old-policy",
        )
    set_approved = approve_set(
        assessed,
        assessment_id="set-assessment-1",
        approver_id="user-1",
        policy_version="set-policy-v1",
    )
    assert set_approved.set_approval.state is ApprovalState.APPROVED

    changed = set_approved.with_subject(second)

    assert changed.subject.approval.state is ApprovalState.NOT_READY
    assert changed.set_approval.state is ApprovalState.NOT_READY
    assert changed.motions[0].candidates == with_candidate.motions[0].candidates
    assert changed.motions[0].selected_candidate_id == "idle-1"


def test_with_subject_keeps_every_motions_candidates_and_selection() -> None:
    """A user correcting their subject must not lose their motion takes:
    `with_subject` only resets subject-scoped and set-scoped approval state
    -- candidates and the selection stay exactly as they were. A selection
    made for the earlier subject becomes stale (caught by `assess_set` and
    the release guard) rather than being silently dropped."""
    manifest = create_project_manifest("project-1", "Ember Knight")
    first = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    manifest = manifest.with_subject(first)
    candidate = MotionCandidate(
        candidate_id="idle-1",
        artifact_key="motions/idle-1.webm",
        sha256="c" * 64,
        subject_revision_id="subject-1",
    )
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            ),
            candidates=(candidate,),
            selected_candidate_id="idle-1",
        )
    )

    second = SubjectRevision(
        revision_id="subject-2",
        artifact_key="subjects/two.png",
        sha256="b" * 64,
        width=1024,
        height=1024,
    )
    changed = manifest.with_subject(second)

    assert changed.motions[0].candidates == (candidate,)
    assert changed.motions[0].selected_candidate_id == "idle-1"
    assert changed.motions[0].selected_candidate is not None
    assert (
        changed.motions[0].selected_candidate.subject_revision_id
        != changed.subject.current_revision.revision_id
    )


def test_set_waiver_requires_valid_codes_and_uses_waived_state() -> None:
    """Quality findings are warnings, never blockers (operator decision, 2026-09-05):
    a waiver no longer requires a reason -- `waived_finding_ids` alone is enough
    to record which findings were seen; an unknown finding id is still rejected."""
    manifest = _selected_approved_manifest()
    digest = manifest_digest(manifest)
    assessed = manifest.with_set_assessment(
        "set-assessment-1",
        digest,
        QualityAssessment(
            policy_version="set-policy-v1",
            findings=(
                QualityFinding(
                    code="motion_warning",
                    severity=FindingSeverity.WARNING,
                    message="Loop needs review",
                ),
            ),
        ),
    )
    reasonless = approve_set(
        assessed,
        assessment_id="set-assessment-1",
        approver_id="user-1",
        policy_version="set-policy-v1",
        waived_finding_ids=("motion_warning",),
    )
    assert reasonless.set_approval.state is ApprovalState.WAIVED
    assert reasonless.set_approval.waiver_reason is None
    with pytest.raises(ValueError, match="do not match"):
        approve_set(
            assessed,
            assessment_id="set-assessment-1",
            approver_id="user-1",
            policy_version="set-policy-v1",
            waived_finding_ids=("made_up",),
            waiver_reason="Reviewed manually",
        )
    waived = approve_set(
        assessed,
        assessment_id="set-assessment-1",
        approver_id="user-1",
        policy_version="set-policy-v1",
        waived_finding_ids=("motion_warning",),
        waiver_reason="Reviewed loop manually",
    )
    assert waived.set_approval.state is ApprovalState.WAIVED
    assert waived.set_approval.waiver_reason == "Reviewed loop manually"


def _selected_approved_manifest() -> AnimationProjectManifest:
    manifest = create_project_manifest("waiver-project", "Ember")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    manifest = approve_subject(
        manifest.with_subject(subject),
        revision_id="subject-1",
        assessment_id="subject-assessment",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            ),
            candidates=(
                MotionCandidate(
                    candidate_id="idle-1",
                    artifact_key="motions/idle.png",
                    sha256="b" * 64,
                ),
            ),
        )
    )
    return select_motion_candidate(manifest, motion_id="idle", candidate_id="idle-1")


def test_creative_mutations_clear_current_set_quality_evidence() -> None:
    manifest = _selected_approved_manifest()
    assessed = manifest.with_set_assessment(
        "set-assessment-1",
        manifest_digest(manifest),
        QualityAssessment(policy_version="set-policy-v1"),
    )

    changed = assessed.with_motion(
        MotionSpecification(
            motion_id="walk", name="Walk", kind=MotionKind.CUSTOM, prompt="walk"
        )
    )

    assert changed.set_assessment is None
    assert changed.set_assessment_id is None
    assert changed.set_assessment_digest is None


def test_reapproving_current_subject_invalidates_existing_set_approval() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    approved = approve_subject(
        manifest.with_subject(subject),
        revision_id="subject-1",
        assessment_id="subject-assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    selected = select_motion_candidate(
        approved.with_motion(
            MotionState(
                specification=MotionSpecification(
                    motion_id="idle", name="Idle", preset=MotionPreset.IDLE
                ),
                candidates=(
                    MotionCandidate(
                        candidate_id="idle-1",
                        artifact_key="motions/idle-1.webm",
                        sha256="b" * 64,
                    ),
                ),
            )
        ),
        motion_id="idle",
        candidate_id="idle-1",
    )
    set_approved = approve_set(
        selected.with_set_assessment(
            "set-assessment-1",
            manifest_digest(selected),
            QualityAssessment(policy_version="set-policy-v1"),
        ),
        assessment_id="set-assessment-1",
        approver_id="user-1",
        policy_version="set-policy-v1",
    )
    assert set_approved.set_approval.state is ApprovalState.APPROVED

    reapproved = approve_subject(
        set_approved,
        revision_id="subject-1",
        assessment_id="subject-assessment-2",
        approver_id="user-2",
        policy_version="subject-v2",
    )

    assert reapproved.subject.approval.state is ApprovalState.APPROVED
    assert reapproved.set_approval.state is ApprovalState.NOT_READY


def test_subject_approval_rejects_a_stale_revision() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )

    with pytest.raises(ValueError, match="current subject revision"):
        approve_subject(
            manifest.with_subject(subject),
            revision_id="other-subject",
            assessment_id="assessment-1",
            approver_id="user-1",
            policy_version="subject-v1",
        )


def test_set_approval_requires_approved_subject() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    with pytest.raises(ValueError, match="subject must be approved"):
        approve_set(
            manifest,
            assessment_id="set-assessment-1",
            approver_id="user-1",
            policy_version="set-policy-v1",
        )


def test_set_approval_requires_selected_candidate_for_every_required_motion() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    approved = approve_subject(
        manifest.with_subject(subject),
        revision_id="subject-1",
        assessment_id="subject-assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    ).with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            )
        )
    )

    with pytest.raises(ValueError, match="every required motion"):
        approve_set(
            approved,
            assessment_id="set-assessment-1",
            approver_id="user-1",
            policy_version="set-policy-v1",
        )


def test_select_motion_candidate_requires_a_known_candidate() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    manifest = manifest.with_subject(subject)
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle",
                name="Idle",
                kind=MotionKind.PRESET,
                preset=MotionPreset.IDLE,
            )
        )
    )
    with pytest.raises(ValueError, match="candidate"):
        select_motion_candidate(
            manifest,
            motion_id="idle",
            candidate_id="missing",
        )


def test_set_approval_requires_assessment_digest_for_current_set() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    approved = approve_subject(
        manifest.with_subject(subject),
        revision_id="subject-1",
        assessment_id="subject-assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    with_candidate = approved.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle",
                name="Idle",
                kind=MotionKind.PRESET,
                preset=MotionPreset.IDLE,
            ),
            candidates=(
                MotionCandidate(
                    candidate_id="idle-1",
                    artifact_key="motions/idle-1.webm",
                    sha256="b" * 64,
                ),
            ),
        )
    )
    selected = select_motion_candidate(
        with_candidate,
        motion_id="idle",
        candidate_id="idle-1",
    )
    with pytest.raises(ValueError, match="assessment digest"):
        approve_set(
            selected,
            assessment_id="set-assessment-1",
            approver_id="user-1",
            policy_version="set-policy-v1",
        )


def test_set_approval_succeeds_with_matching_assessment_digest() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    approved = approve_subject(
        manifest.with_subject(subject),
        revision_id="subject-1",
        assessment_id="subject-assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    with_candidate = approved.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE
            ),
            candidates=(
                MotionCandidate(
                    candidate_id="idle-1",
                    artifact_key="motions/idle-1.webm",
                    sha256="b" * 64,
                ),
            ),
        )
    )
    selected = select_motion_candidate(
        with_candidate,
        motion_id="idle",
        candidate_id="idle-1",
    )
    set_digest = manifest_digest(selected)
    approved_set = approve_set(
        selected.with_set_assessment(
            "set-assessment-1",
            set_digest,
            QualityAssessment(policy_version="set-policy-v1"),
        ),
        assessment_id="set-assessment-1",
        approver_id="user-1",
        policy_version="set-policy-v1",
    )

    assert approved_set.set_approval.state is ApprovalState.APPROVED
    assert approved_set.set_approval.manifest_digest == set_digest


def test_approve_set_rejects_subject_approval_not_bound_to_current_revision() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    selected = select_motion_candidate(
        manifest.with_subject(subject).with_motion(
            MotionState(
                specification=MotionSpecification(
                    motion_id="idle", name="Idle", preset=MotionPreset.IDLE
                ),
                candidates=(
                    MotionCandidate(
                        candidate_id="idle-1",
                        artifact_key="motions/idle-1.webm",
                        sha256="b" * 64,
                    ),
                ),
            )
        ),
        motion_id="idle",
        candidate_id="idle-1",
    )
    assessed = selected.with_set_assessment(
        "set-assessment-1",
        manifest_digest(selected),
        QualityAssessment(policy_version="set-policy-v1"),
    )
    unbound = assessed.model_copy(
        update={
            "subject": selected.subject.model_copy(
                update={
                    "approval": Approval(
                        state=ApprovalState.APPROVED,
                        assessment_id="subject-assessment-1",
                        approver_id="user-1",
                        policy_version="subject-v1",
                        artifact_sha256="stale" * 16,
                    )
                }
            )
        }
    )

    with pytest.raises(ValueError, match="bound to the current subject"):
        approve_set(
            unbound,
            assessment_id="set-assessment-1",
            approver_id="user-1",
            policy_version="set-policy-v1",
        )


def test_motion_specification_does_not_store_candidates() -> None:
    with pytest.raises(ValidationError, match="candidates"):
        MotionSpecification(
            motion_id="idle",
            name="Idle",
            preset=MotionPreset.IDLE,
            candidates=(),
        )


def test_manifest_digest_survives_canonical_json_round_trip() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight")

    restored = AnimationProjectManifest.model_validate_json(
        manifest.model_dump_json(exclude_none=True)
    )

    assert manifest_digest(restored) == manifest_digest(manifest)


def test_summarize_manifest_change_describes_each_transition_for_history() -> None:
    created = create_project_manifest("project-1", "Ember Knight")
    assert summarize_manifest_change(None, created) == "Created the project"

    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="subjects/one.png",
        sha256="a" * 64,
        width=1024,
        height=1024,
    )
    with_subject = created.with_subject(subject)
    assert summarize_manifest_change(created, with_subject) == "Generated a new subject"

    approved_subject = approve_subject(
        with_subject,
        revision_id="subject-1",
        assessment_id="assessment-1",
        approver_id="user-1",
        policy_version="subject-v1",
    )
    assert summarize_manifest_change(with_subject, approved_subject) == "Approved the subject"

    with_motion = approved_subject.with_motion(
        MotionState(specification=MotionSpecification(motion_id="idle", name="Idle", preset=MotionPreset.IDLE))
    )
    assert summarize_manifest_change(approved_subject, with_motion) == "Added motion Idle"

    candidate = MotionCandidate(candidate_id="idle-1", artifact_key="motions/idle-1.webm", sha256="c" * 64)
    with_candidate = add_motion_candidate(with_motion, motion_id="idle", candidate=candidate)
    assert summarize_manifest_change(with_motion, with_candidate) == "Generated a candidate for Idle"

    selected = select_motion_candidate(with_candidate, motion_id="idle", candidate_id="idle-1")
    assert summarize_manifest_change(with_candidate, selected) == "Selected a candidate for Idle"

    revised = add_derived_motion_candidate(
        selected,
        motion_id="idle",
        candidate=MotionCandidate(
            candidate_id="idle-2",
            artifact_key="motions/idle-2.webm",
            sha256="d" * 64,
            parent_revision_ids=("idle-1",),
        ),
    )
    assert summarize_manifest_change(selected, revised) == "Saved a frame revision for Idle"

    assessed = revised.with_set_assessment(
        "set-assessment-1", manifest_digest(revised), QualityAssessment(policy_version="set-policy-v1")
    )
    set_approved = approve_set(
        assessed, assessment_id="set-assessment-1", approver_id="user-1", policy_version="set-policy-v1"
    )
    assert summarize_manifest_change(assessed, set_approved) == "Approved the animation set"

    assert summarize_manifest_change(set_approved, set_approved) == "Updated the project"


def test_set_external_subject_is_a_pure_retag_that_survives_approvals() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight", owner_id="user-1")
    manifest = manifest.with_subject(
        SubjectRevision(
            revision_id="subject-1", artifact_key="sources/subject.png",
            sha256="a" * 64, width=64, height=64,
        )
    )
    approved = approve_subject(
        manifest, revision_id="subject-1", assessment_id="assessment-1",
        approver_id="user-1", policy_version="subject-policy-v1",
    )
    subject = ExternalSubject(
        system="acme-engine", entity_kind="character",
        entity_id="char-42", display_name="Ser Ember",
    )

    bound = set_external_subject(approved, external_subject=subject)

    assert bound.external_subject == subject
    assert bound.subject.approval.state is ApprovalState.APPROVED
    assert bound.subject.current_revision == approved.subject.current_revision

    cleared = set_external_subject(bound, external_subject=None)
    assert cleared.external_subject is None
    assert cleared.subject.approval.state is ApprovalState.APPROVED

    # It also does not knock a signed-off set out of "fresh" (`approve_set` binds
    # `set_assessment_digest`/`manifest_digest` to the pre-tag set digest).
    assert set_digest(approved) == set_digest(bound) == set_digest(cleared)


def test_recompile_preset_motion_prompts_updates_preset_prompts_only() -> None:
    """Refresh legacy preset prompts anonymously while preserving custom descriptions."""
    manifest = create_project_manifest("project-1", "Ember Knight", owner_id="user-1")
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="walk", name="Walk", preset=MotionPreset.WALK,
                prompt="Ember Knight walks in place — a clean, seamless walk cycle.",
                prompt_source=PromptSource.PRESET,
            )
        )
    )
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="attack", name="Attack", preset=MotionPreset.ATTACK,
                prompt="Their own hand-authored swing description.",
                prompt_source=PromptSource.CUSTOM,
            )
        )
    )
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="taunt", name="Taunt", kind=MotionKind.CUSTOM,
                prompt="A freeform taunt animation.",
            )
        )
    )

    recompiled = recompile_preset_motion_prompts(manifest, subject_name="Ser Ember")

    walk = next(m for m in recompiled.motions if m.motion_id == "walk")
    attack = next(m for m in recompiled.motions if m.motion_id == "attack")
    taunt = next(m for m in recompiled.motions if m.motion_id == "taunt")
    assert walk.specification.prompt == (
        "The character walks in place — a clean, seamless walk cycle."
    )
    assert attack.specification.prompt == "Their own hand-authored swing description."
    assert taunt.specification.prompt == "A freeform taunt animation."


def test_recompile_preset_motion_prompts_is_a_no_op_when_nothing_changes() -> None:
    manifest = create_project_manifest("project-1", "Ember Knight", owner_id="user-1")
    manifest = manifest.with_motion(
        MotionState(
            specification=MotionSpecification(
                motion_id="idle", name="Idle", preset=MotionPreset.IDLE,
                prompt="The character breathes and shifts weight — a subtle idle loop with minimal drift.",
                prompt_source=PromptSource.PRESET,
            )
        )
    )

    unchanged = recompile_preset_motion_prompts(manifest, subject_name="Ember Knight")

    assert unchanged is manifest

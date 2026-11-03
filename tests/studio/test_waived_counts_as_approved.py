"""A subject approved with acknowledged findings (state `waived`) unlocks motion work."""

from magic_video_editor.studio.manifest import (
    approve_subject,
    create_project_manifest,
    _require_current_subject_approval,
)
from magic_video_editor.studio.models import ApprovalState, SubjectRevision


def _manifest_with_waived_subject():
    manifest = create_project_manifest("project-1", "Ember", owner_id="user-1")
    subject = SubjectRevision(
        revision_id="subject-1",
        artifact_key="sources/subject.png",
        sha256="a" * 64,
        width=64,
        height=64,
    )
    manifest = manifest.with_subject(subject)
    return approve_subject(
        manifest,
        revision_id="subject-1",
        assessment_id="subject-assessment",
        approver_id="user-1",
        policy_version="subject-policy-v1",
        waived_finding_ids=("finding-1",),
        waiver_reason="acknowledged",
    )


def test_waived_subject_approval_counts_as_approved_for_set_work() -> None:
    manifest = _manifest_with_waived_subject()
    assert manifest.subject.approval.state is ApprovalState.WAIVED
    # Must not raise: waived is an approval with acknowledged findings.
    _require_current_subject_approval(manifest)

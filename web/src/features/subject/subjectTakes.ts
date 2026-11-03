import type { ApprovalState, Motion, SubjectAssessment, SubjectRevision, SubjectRevisionSource } from '../../api/types'

/**
 * Take numbers count from the oldest revision = 1 (studio-ux-flow.md section 3), even though the
 * API and the gallery both order revisions newest-first. Returns 0 for a revision id that is not
 * in the list (defensive -- the UI should never call this with a stale id).
 */
export function takeNumber(revisions: SubjectRevision[], revisionId: string): number {
  const index = revisions.findIndex((revision) => revision.revision_id === revisionId)
  if (index === -1) return 0
  return revisions.length - index
}

/** The gallery card's source tag: Generated / Uploaded / Edited. */
export function sourceLabel(source: SubjectRevisionSource): string {
  if (source === 'uploaded') return 'Uploaded'
  if (source === 'edited') return 'Edited'
  return 'Generated'
}

/** Motions bound to a subject revision: any motion with at least one candidate whose
 * `subject_revision_id` matches it (studio-ux-flow.md section 3). */
export function motionsBoundCount(motions: Motion[], revisionId: string): number {
  return motions.filter((motion) => motion.candidates.some((candidate) => candidate.subject_revision_id === revisionId)).length
}

/** The Chosen card's "Animating this subject" line, or null when there is nothing to say --
 * "no motions yet" told the operator only what they already knew (feedback 2026-09-06). */
export function motionsBoundLabel(motions: Motion[], revisionId: string): string | null {
  const count = motionsBoundCount(motions, revisionId)
  if (count === 0) return null
  return `Animating this subject · ${count} motion${count === 1 ? '' : 's'}`
}

export type SubjectApprovalDecision = 'already_approved' | 'auto_approve' | 'no_assessment'

/**
 * Decision 1 (studio-ux-flow.md section 9), revised 2026-09-05: quality findings are warnings,
 * never blockers, so choosing a subject take always auto-approves it once it has a settled
 * assessment -- there is no "needs review" stop any more, since approval itself never refuses on
 * findings; it just records whatever findings are present as acknowledged.
 */
export function decideSubjectApproval(assessment: SubjectAssessment | null | undefined, approvalState: ApprovalState): SubjectApprovalDecision {
  if (approvalState === 'approved' || approvalState === 'waived') return 'already_approved'
  if (!assessment) return 'no_assessment'
  return 'auto_approve'
}

import { describe, expect, it } from 'vitest'
import type { Motion, SubjectAssessment, SubjectRevision } from '../../api/types'
import { decideSubjectApproval, motionsBoundCount, motionsBoundLabel, sourceLabel, takeNumber } from './subjectTakes'

function revision(overrides: Partial<SubjectRevision> = {}): SubjectRevision {
  return { revision_id: 'r1', artifact_key: 'k', sha256: 'a'.repeat(64), width: 512, height: 512, lifecycle: 'ready', parent_revision_ids: [], source: 'generated', direction: null, created_at: null, ...overrides }
}

describe('takeNumber', () => {
  it('numbers the oldest revision 1, even though the list is newest-first', () => {
    const revisions = [revision({ revision_id: 'r3' }), revision({ revision_id: 'r2' }), revision({ revision_id: 'r1' })]
    expect(takeNumber(revisions, 'r1')).toBe(1)
    expect(takeNumber(revisions, 'r2')).toBe(2)
    expect(takeNumber(revisions, 'r3')).toBe(3)
  })

  it('returns 0 for an id that is not in the list', () => {
    expect(takeNumber([revision({ revision_id: 'r1' })], 'missing')).toBe(0)
  })
})

describe('sourceLabel', () => {
  it('maps each source to its display label', () => {
    expect(sourceLabel('generated')).toBe('Generated')
    expect(sourceLabel('uploaded')).toBe('Uploaded')
    expect(sourceLabel('edited')).toBe('Edited')
  })
})

function motion(candidates: { subject_revision_id?: string | null }[]): Motion {
  return {
    motion_id: 'm', name: 'Idle', kind: 'preset', semantic_slot: null, loop_policy: 'loop', required: true, selected_candidate_id: null,
    candidates: candidates.map((c, index) => ({
      candidate_id: `c${index}`, artifact_key: 'k', sha256: 'a'.repeat(64), duration_ms: null, metadata: null, assessment: null,
      ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right',
      recipe: null, source: 'generated', subject_revision_id: c.subject_revision_id ?? null,
    })),
  }
}

describe('motionsBoundCount / motionsBoundLabel', () => {
  it('counts a motion once even when several of its candidates share the revision', () => {
    const motions = [motion([{ subject_revision_id: 'r1' }, { subject_revision_id: 'r1' }]), motion([{ subject_revision_id: 'r2' }])]
    expect(motionsBoundCount(motions, 'r1')).toBe(1)
  })

  it('ignores candidates made from a different or absent revision', () => {
    const motions = [motion([{ subject_revision_id: 'r2' }]), motion([{ subject_revision_id: null }])]
    expect(motionsBoundCount(motions, 'r1')).toBe(0)
  })

  it('reports the singular label for exactly one motion', () => {
    expect(motionsBoundLabel([motion([{ subject_revision_id: 'r1' }])], 'r1')).toBe('Animating this subject · 1 motion')
  })

  it('reports the plural label for several motions', () => {
    expect(motionsBoundLabel([motion([{ subject_revision_id: 'r1' }]), motion([{ subject_revision_id: 'r1' }])], 'r1')).toBe('Animating this subject · 2 motions')
  })

  it('says nothing at all when no motions are bound (operator, 2026-09-06)', () => {
    expect(motionsBoundLabel([], 'r1')).toBeNull()
  })
})

function assessment(findings: SubjectAssessment['findings']): SubjectAssessment {
  return { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: 'a'.repeat(64), width: 512, height: 512, occupancy_ratio: 0.7, findings }
}

describe('decideSubjectApproval', () => {
  it('treats an approved or waived revision as already approved regardless of findings', () => {
    expect(decideSubjectApproval(assessment([{ code: 'subject_missing_alpha', severity: 'blocking', title: 't', explanation: 'e', artifact_sha256: 'a'.repeat(64), action_id: null }]), 'approved')).toBe('already_approved')
    expect(decideSubjectApproval(null, 'waived')).toBe('already_approved')
  })

  it('reports no_assessment when there is nothing to judge yet', () => {
    expect(decideSubjectApproval(null, 'ready_for_signoff')).toBe('no_assessment')
    expect(decideSubjectApproval(undefined, 'ready_for_signoff')).toBe('no_assessment')
  })

  it('auto-approves when every finding is advisory', () => {
    const findings: SubjectAssessment['findings'] = [{ code: 'subject_occupancy_too_low', severity: 'warning', title: 't', explanation: 'e', artifact_sha256: 'a'.repeat(64), action_id: null }]
    expect(decideSubjectApproval(assessment(findings), 'ready_for_signoff')).toBe('auto_approve')
  })

  it('auto-approves when there are no findings at all', () => {
    expect(decideSubjectApproval(assessment([]), 'ready_for_signoff')).toBe('auto_approve')
  })

  it('auto-approves even when a finding carries the legacy blocking severity', () => {
    // Quality findings are warnings, never blockers (operator decision, 2026-09-05): there is no
    // "needs review" stop any more, whatever severity a finding carries.
    const findings: SubjectAssessment['findings'] = [
      { code: 'subject_occupancy_too_low', severity: 'warning', title: 't', explanation: 'e', artifact_sha256: 'a'.repeat(64), action_id: null },
      { code: 'subject_missing_alpha', severity: 'blocking', title: 't2', explanation: 'e2', artifact_sha256: 'a'.repeat(64), action_id: null },
    ]
    expect(decideSubjectApproval(assessment(findings), 'ready_for_signoff')).toBe('auto_approve')
  })
})

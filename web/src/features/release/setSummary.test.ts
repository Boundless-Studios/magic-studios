import { expect, it } from 'vitest'
import type { Motion, MotionCandidate, SubjectRevision } from '../../api/types'
import { buildSetItems, setSummaryLine } from './setSummary'

const subjectR1: SubjectRevision = { revision_id: 'subject-r1', artifact_key: 's1.png', sha256: 's1', width: 512, height: 512, lifecycle: 'ready', parent_revision_ids: [], source: 'generated', direction: null, created_at: null }
const subjectR2: SubjectRevision = { ...subjectR1, revision_id: 'subject-r2' }

function candidate(overrides: Partial<MotionCandidate> = {}): MotionCandidate {
  return {
    candidate_id: 'c1', artifact_key: 'c1.mp4', sha256: 'c1', duration_ms: 1000, metadata: null, assessment: null,
    ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right',
    recipe: null, source: 'generated', subject_revision_id: 'subject-r1', ...overrides,
  }
}

function motion(overrides: Partial<Motion> = {}): Motion {
  return { motion_id: 'idle', name: 'Idle', kind: 'preset', semantic_slot: 'idle', loop_policy: 'loop', required: true, candidates: [], selected_candidate_id: null, ...overrides }
}

it('marks a tile stale only when its candidate was made from a different subject revision', () => {
  const items = buildSetItems(
    [motion({ motion_id: 'idle', candidates: [candidate({ candidate_id: 'c1', subject_revision_id: 'subject-r1' })], selected_candidate_id: 'c1' })],
    subjectR2,
  )
  expect(items).toEqual([{ motionId: 'idle', name: 'Idle', candidate: expect.objectContaining({ candidate_id: 'c1' }), stale: true }])
})

it('never treats a candidate with no recorded subject_revision_id as stale', () => {
  const items = buildSetItems(
    [motion({ candidates: [candidate({ candidate_id: 'c1', subject_revision_id: null })], selected_candidate_id: 'c1' })],
    subjectR2,
  )
  expect(items[0].stale).toBe(false)
})

it('reports a motion with no selected take as a null-candidate tile', () => {
  const items = buildSetItems([motion({ motion_id: 'walk', name: 'Walk' })], subjectR1)
  expect(items).toEqual([{ motionId: 'walk', name: 'Walk', candidate: null, stale: false }])
})

it('summarizes a clean, fully selected set with a single clause', () => {
  const items = buildSetItems(
    [motion({ candidates: [candidate({ candidate_id: 'c1', subject_revision_id: 'subject-r1' })], selected_candidate_id: 'c1' })],
    subjectR1,
  )
  expect(setSummaryLine(items, 0)).toBe('1 of 1 motions selected')
})

it('appends stale and findings clauses only when they are non-zero', () => {
  const items = buildSetItems(
    [
      motion({ motion_id: 'idle', candidates: [candidate({ candidate_id: 'c1', subject_revision_id: 'subject-r1' })], selected_candidate_id: 'c1' }),
      motion({ motion_id: 'walk', name: 'Walk' }),
    ],
    subjectR2,
  )
  expect(setSummaryLine(items, 3)).toBe('1 of 2 motions selected · 1 stale · 3 findings')
})

it('uses the singular "finding" for exactly one', () => {
  expect(setSummaryLine(buildSetItems([], subjectR1), 1)).toBe('0 of 0 motions selected · 1 finding')
})

it('labels duplicate actions by declared view, leaving legacy names unchanged', () => {
  const items = buildSetItems([
    motion({ motion_id: 'n-idle', name: 'Idle', view_id: 'N' }),
    motion({ motion_id: 'sw-idle', name: 'Idle', view_id: 'SW' }),
    motion({ motion_id: 'legacy-idle', name: 'Idle' }),
  ], subjectR1, [{ view_id: 'N', name: 'North', reference: null }, { view_id: 'SW', name: 'SW', reference: null }])
  expect(items.map((item) => item.name)).toEqual(['North (N) · Idle', 'SW · Idle', 'Idle'])
})

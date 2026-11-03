import { expect, it } from 'vitest'
import type { ManifestSummary, Motion, MotionCandidate } from '../../api/types'
import { deriveSetFindings } from './setFindings'

function candidate(overrides: Partial<MotionCandidate> = {}): MotionCandidate {
  return {
    candidate_id: 'c1', artifact_key: 'c1.mp4', sha256: 'c1', duration_ms: 1000, metadata: null, assessment: null,
    ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right',
    recipe: null, source: 'generated', ...overrides,
  }
}
function motion(overrides: Partial<Motion> = {}): Motion {
  return { motion_id: 'idle', name: 'Idle', kind: 'preset', semantic_slot: 'idle', loop_policy: 'loop', required: true, candidates: [], selected_candidate_id: null, ...overrides }
}
function manifest(overrides: Partial<Pick<ManifestSummary, 'motions' | 'set_assessment'>> = {}): Pick<ManifestSummary, 'motions' | 'set_assessment'> {
  return { motions: [], set_assessment: null, ...overrides }
}

it('attributes a selected candidate finding to its motion and points it at Frames', () => {
  const findings = deriveSetFindings(manifest({
    motions: [motion({ candidates: [candidate({ candidate_id: 'c1', assessment: { policy_version: 'v1', findings: [{ code: 'motion_missing_alpha', severity: 'warning', message: 'Sprite output must preserve transparency.', action_id: 'derive_alpha' }] } })], selected_candidate_id: 'c1' })],
    set_assessment: { policy_version: 'set-v1', findings: [{ code: 'motion_missing_alpha', severity: 'warning', message: 'Sprite output must preserve transparency.', action_id: null }] },
  }))
  expect(findings).toEqual([{ id: 'idle-motion_missing_alpha', severity: 'warning', title: 'Idle: motion missing alpha', explanation: 'Sprite output must preserve transparency.', motionId: 'idle', motionName: 'Idle' }])
})

it('keeps a genuine set-level finding with no motion counterpart', () => {
  // Quality findings are warnings, never blockers (operator decision, 2026-09-05): there is no
  // "waivable" distinction any more -- every finding is informational.
  const findings = deriveSetFindings(manifest({
    set_assessment: { policy_version: 'set-v1', findings: [{ code: 'set_canvas_mismatch', severity: 'warning', message: 'Selected motions must share a frame canvas.', action_id: null }] },
  }))
  expect(findings).toEqual([{ id: 'set_canvas_mismatch', severity: 'warning', title: 'set canvas mismatch', explanation: 'Selected motions must share a frame canvas.', motionId: null, motionName: null }])
})

it('still surfaces a finding carrying the legacy blocking severity', () => {
  const findings = deriveSetFindings(manifest({
    set_assessment: { policy_version: 'set-v1', findings: [{ code: 'set_canvas_mismatch', severity: 'blocking', message: 'Selected motions must share a frame canvas.', action_id: null }] },
  }))
  expect(findings).toHaveLength(1)
  expect(findings[0].severity).toBe('blocking')
})

it('returns nothing when there is no assessment yet', () => {
  expect(deriveSetFindings(manifest())).toEqual([])
})

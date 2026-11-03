import { expect, it } from 'vitest'
import type { Motion, Operation } from '../../api/types'
import { motionChipInfo } from './motionChip'

const base: Motion = { motion_id: 'idle', name: 'Idle', kind: 'preset', semantic_slot: 'idle', loop_policy: 'loop', required: true, candidates: [], selected_candidate_id: null }
const candidate = { candidate_id: 'c1', artifact_key: 'c1.png', sha256: 'x', metadata: null, assessment: null, duration_ms: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right' as const, recipe: null, source: 'generated' as const }
const op = (over: Partial<Operation>): Operation => ({ job_id: 'job', status: 'queued', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false, ...over })

it('labels an untouched motion "To generate"', () => {
  expect(motionChipInfo(base, [])).toEqual({ label: 'To generate', tone: 'neutral', progressPercent: null, spinner: false })
})

it('labels a queued operation "Queued"', () => {
  expect(motionChipInfo(base, [op({ status: 'queued' })])).toEqual({ label: 'Queued', tone: 'accent', progressPercent: null, spinner: true })
})

it('labels a processing operation "Generating" with a known percentage', () => {
  expect(motionChipInfo(base, [op({ status: 'processing', progress_percent: 43 })])).toEqual({ label: 'Generating 43%', tone: 'accent', progressPercent: 43, spinner: true })
})

it('labels a processing operation "Generating" without a known percentage', () => {
  expect(motionChipInfo(base, [op({ status: 'processing', progress_percent: null })])).toEqual({ label: 'Generating…', tone: 'accent', progressPercent: null, spinner: true })
})

it('labels a review-ready motion "Review" (amber)', () => {
  expect(motionChipInfo({ ...base, candidates: [candidate] }, [])).toEqual({ label: 'Review', tone: 'warn', progressPercent: null, spinner: false })
})

it('labels a selected motion "Selected" (success), even mid-regenerate', () => {
  expect(motionChipInfo({ ...base, candidates: [candidate], selected_candidate_id: 'c1' }, [])).toEqual({ label: 'Selected', tone: 'success', progressPercent: null, spinner: false })
})

it('labels an unrecovered failure "Failed" (danger)', () => {
  expect(motionChipInfo(base, [op({ status: 'failed' })])).toEqual({ label: 'Failed', tone: 'danger', progressPercent: null, spinner: false })
})

it('labels an unrecovered cancellation "Cancelled" (grey)', () => {
  expect(motionChipInfo(base, [op({ status: 'cancelled' })])).toEqual({ label: 'Cancelled', tone: 'grey', progressPercent: null, spinner: false })
})

it('labels a completed but unsettled operation "Finishing…", never "Queued"', () => {
  expect(motionChipInfo(base, [op({ status: 'completed', stage: 'awaiting_settlement' })])).toEqual({ label: 'Finishing…', tone: 'accent', progressPercent: null, spinner: true })
})

it('labels a completed operation whose settlement is blocked "Needs attention"', () => {
  expect(motionChipInfo(base, [op({ status: 'completed', stage: 'awaiting_settlement', settlement_blocked: 'completed motion artifact is missing' })])).toEqual({ label: 'Needs attention', tone: 'warn', progressPercent: null, spinner: false })
})

it('labels a selected take made from a superseded subject revision "Stale" (grey)', () => {
  const staleCandidate = { ...candidate, subject_revision_id: 'r1' }
  const currentRevision = { revision_id: 'r2', artifact_key: 'k', sha256: 's', width: 1, height: 1, lifecycle: 'ready' as never, parent_revision_ids: [], source: 'generated' as never, direction: null, created_at: null }
  expect(motionChipInfo({ ...base, candidates: [staleCandidate], selected_candidate_id: 'c1' }, [], currentRevision)).toEqual({ label: 'Stale', tone: 'grey', progressPercent: null, spinner: false })
})

import { describe, expect, it } from 'vitest'
import type { Motion, Operation } from '../../api/types'
import { isOperationActive, needsGeneration, operationForMotion } from './operationSelection'

function op(overrides: Partial<Operation>): Operation {
  return {
    job_id: 'job-1', status: 'queued', motion_id: 'idle', output_key: 'out',
    progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null,
    retryable: false, ...overrides,
  }
}

function motion(overrides: Partial<Motion>): Motion {
  return {
    motion_id: 'idle', name: 'Idle', kind: 'preset', semantic_slot: 'idle', loop_policy: 'loop',
    required: true, candidates: [], selected_candidate_id: null, ...overrides,
  }
}

describe('isOperationActive', () => {
  it('is active while queued or processing', () => {
    expect(isOperationActive(op({ status: 'queued', stage: 'queued' }))).toBe(true)
    expect(isOperationActive(op({ status: 'processing', stage: 'generating' }))).toBe(true)
  })
  it('is active when completed but not yet settled', () => {
    expect(isOperationActive(op({ status: 'completed', stage: 'awaiting_settlement' }))).toBe(true)
  })
  it('is inactive once a completed job has settled -- it is history, not live work', () => {
    expect(isOperationActive(op({ status: 'completed', stage: 'settled' }))).toBe(false)
  })
  it('is inactive when failed or cancelled', () => {
    expect(isOperationActive(op({ status: 'failed', stage: 'failed' }))).toBe(false)
    expect(isOperationActive(op({ status: 'cancelled', stage: 'cancelled' }))).toBe(false)
  })
})

describe('operationForMotion', () => {
  it('ignores a settled completed job for a motion that already has a candidate', () => {
    const settled = op({ job_id: 'old', status: 'completed', stage: 'settled' })
    expect(operationForMotion(motion({ candidates: [{ candidate_id: 'c1' } as never] }), [settled])).toBeUndefined()
  })
  it('still surfaces a live job over settled history', () => {
    const settled = op({ job_id: 'old', status: 'completed', stage: 'settled' })
    const live = op({ job_id: 'new', status: 'processing', stage: 'generating' })
    expect(operationForMotion(motion({}), [settled, live])?.job_id).toBe('new')
  })
})

describe('needsGeneration', () => {
  it('is false while a settled job is the only history but a candidate now exists', () => {
    const settled = op({ status: 'completed', stage: 'settled' })
    expect(needsGeneration(motion({ candidates: [{ candidate_id: 'c1' } as never] }), [settled])).toBe(false)
  })
  it('is true for a candidate-less motion whose only operation has already settled (settlement produced no candidate, e.g. a failure recorded oddly)', () => {
    const settled = op({ status: 'completed', stage: 'settled' })
    expect(needsGeneration(motion({}), [settled])).toBe(true)
  })
})

import { describe, expect, it } from 'vitest'
import type { Motion, Operation, SubjectRevision } from '../../api/types'
import { batchButtonLabel, defaultChecked, generationSummary, hasTakesFromOlderSpec, inFlightCheckedCount, isCandidateStale, readinessSummary, settledSinceDispatch } from './motionReadiness'

function motion(overrides: Partial<Motion>): Motion {
  return {
    motion_id: 'idle', name: 'Idle', kind: 'preset', semantic_slot: 'idle', loop_policy: 'loop',
    required: true, candidates: [], selected_candidate_id: null, ...overrides,
  }
}
function op(overrides: Partial<Operation>): Operation {
  return {
    job_id: 'job-1', status: 'queued', motion_id: 'idle', output_key: 'out', progress_percent: null,
    stage: 'queued', failure_code: null, failure_explanation: null, retryable: false, ...overrides,
  }
}
function revision(id: string): SubjectRevision {
  return { revision_id: id, artifact_key: 'k', sha256: 'sha', width: 1, height: 1, lifecycle: 'ready' as never, parent_revision_ids: [], source: 'generated' as never, direction: null, created_at: null }
}

describe('isCandidateStale', () => {
  it('is not stale with no recorded lineage', () => {
    expect(isCandidateStale({ subject_revision_id: null } as never, revision('r2'))).toBe(false)
  })
  it('is stale once the subject has moved past it', () => {
    expect(isCandidateStale({ subject_revision_id: 'r1' } as never, revision('r2'))).toBe(true)
  })
  it('is not stale when it matches the current subject revision', () => {
    expect(isCandidateStale({ subject_revision_id: 'r2' } as never, revision('r2'))).toBe(false)
  })
})

describe('defaultChecked', () => {
  it('is checked with no selection and nothing in flight', () => {
    expect(defaultChecked(motion({}), [])).toBe(true)
  })
  it('is unchecked once a candidate is selected', () => {
    expect(defaultChecked(motion({ selected_candidate_id: 'c1' }), [])).toBe(false)
  })
  it('is unchecked while a job is already running', () => {
    expect(defaultChecked(motion({}), [op({ status: 'processing', stage: 'generating' })])).toBe(false)
  })
})

describe('readinessSummary', () => {
  it('reports selected count and pending review', () => {
    const motions = [
      motion({ motion_id: 'idle', selected_candidate_id: 'c1', candidates: [{ candidate_id: 'c1' } as never] }),
      motion({ motion_id: 'walk', candidates: [{ candidate_id: 'c2' } as never] }),
      motion({ motion_id: 'run' }),
    ]
    expect(readinessSummary(motions, [])).toBe('1 of 3 selected · 1 to review')
  })
  it('omits the review clause when nothing needs review', () => {
    const motions = [motion({ motion_id: 'idle', selected_candidate_id: 'c1', candidates: [{ candidate_id: 'c1' } as never] })]
    expect(readinessSummary(motions, [])).toBe('1 of 1 selected')
  })
  it('excludes selected takes from an older view reference and keeps fresh takes to review', () => {
    const motions = [
      motion({ view_id: 'N', spec_digest: 'current', selected_candidate_id: 'old', candidates: [{ candidate_id: 'old', motion_spec_digest: 'previous' } as never] }),
      motion({ motion_id: 'attack', view_id: 'N', spec_digest: 'current', candidates: [{ candidate_id: 'fresh', motion_spec_digest: 'current' } as never] }),
    ]
    expect(readinessSummary(motions, [])).toBe('0 of 2 selected · 1 stale · 1 to review')
  })
  it('uses subject lineage for legacy selections without treating legacy spec edits as stale', () => {
    const motions = [
      motion({ selected_candidate_id: 'old', candidates: [{ candidate_id: 'old', subject_revision_id: 'r1' } as never] }),
      motion({ motion_id: 'walk', spec_digest: 'new-description', selected_candidate_id: 'legacy', candidates: [{ candidate_id: 'legacy', motion_spec_digest: 'old-description' } as never] }),
    ]
    expect(readinessSummary(motions, [], revision('r2'))).toBe('1 of 2 selected · 1 stale')
  })
  it('does not offer only-stale takes for review, but counts a fresh replacement for a stale selection', () => {
    const old = { candidate_id: 'old', motion_spec_digest: 'previous' } as never
    const fresh = { candidate_id: 'fresh', motion_spec_digest: 'current' } as never
    const motions = [motion({ view_id: 'N', spec_digest: 'current', candidates: [old] })]
    expect(readinessSummary(motions, [])).toBe('0 of 1 selected · 1 stale')
    expect(readinessSummary([{ ...motions[0], selected_candidate_id: 'old', candidates: [old, fresh] }], [])).toBe('0 of 1 selected · 1 stale · 1 to review')
  })
})

describe('generationSummary', () => {
  it('is absent while nothing is in flight', () => {
    expect(generationSummary([motion({ motion_id: 'idle' })], [])).toBeNull()
  })
  it('counts the batch and the motions already done', () => {
    const motions = [
      motion({ motion_id: 'idle' }),
      motion({ motion_id: 'walk' }),
      motion({ motion_id: 'run', candidates: [{ candidate_id: 'c1' } as never] }),
    ]
    const operations = [op({ job_id: 'a', motion_id: 'idle' }), op({ job_id: 'b', motion_id: 'walk', status: 'processing', stage: 'generating' })]
    expect(generationSummary(motions, operations)).toBe('Generating 3 motions · 1 done')
  })
  it('omits the done clause when nothing has landed yet', () => {
    expect(generationSummary([motion({ motion_id: 'idle' })], [op({ motion_id: 'idle' })])).toBe('Generating 1 motion')
  })
})

describe('batchButtonLabel', () => {
  const take = { candidate_id: 'c1' } as never
  it('offers to generate the ticked motions that have nothing yet', () => {
    const motions = [motion({ motion_id: 'idle' }), motion({ motion_id: 'walk' })]
    expect(batchButtonLabel(motions, [], new Set(['idle', 'walk']))).toBe('Generate 2 motions')
    expect(batchButtonLabel(motions, [], new Set(['idle']))).toBe('Generate 1 motion')
  })
  it('offers to regenerate once every ticked motion already has a take', () => {
    const motions = [motion({ motion_id: 'idle', candidates: [take] }), motion({ motion_id: 'walk', candidates: [take] })]
    expect(batchButtonLabel(motions, [], new Set(['idle', 'walk']))).toBe('Regenerate 2 motions')
    // A mixed tick is still a generate -- something in it has never rendered.
    expect(batchButtonLabel([...motions, motion({ motion_id: 'run' })], [], new Set(['idle', 'run']))).toBe('Generate 2 motions')
  })
  it('counts what is actually in flight, not what is ticked', () => {
    const motions = [motion({ motion_id: 'idle' }), motion({ motion_id: 'walk' })]
    const operations = [op({ motion_id: 'idle', status: 'processing', stage: 'generating' })]
    expect(batchButtonLabel(motions, operations, new Set(['idle', 'walk']))).toBe('Generating 1…')
    expect(inFlightCheckedCount(motions, operations, new Set(['idle', 'walk']))).toBe(1)
  })
})

describe('settledSinceDispatch', () => {
  const take = { candidate_id: 'c1' } as never
  it('reports only dispatched motions whose new take has landed', () => {
    const motions = [
      motion({ motion_id: 'idle', candidates: [take] }),
      motion({ motion_id: 'walk' }),
      motion({ motion_id: 'run', candidates: [take] }),
    ]
    expect(settledSinceDispatch(motions, [], { idle: 0, walk: 0 })).toEqual(['idle'])
    // A regeneration only settles once its take count grows past what it started with.
    expect(settledSinceDispatch(motions, [], { idle: 1 })).toEqual([])
  })
  it('waits for a still-running job', () => {
    const motions = [motion({ motion_id: 'idle', candidates: [take] })]
    expect(settledSinceDispatch(motions, [op({ status: 'processing', stage: 'generating' })], { idle: 0 })).toEqual([])
  })
})

describe('hasTakesFromOlderSpec', () => {
  it('flags takes rendered before the description changed, and nothing else', () => {
    const older = { candidate_id: 'c1', motion_spec_digest: 'old' } as never
    const current = { candidate_id: 'c2', motion_spec_digest: 'new' } as never
    expect(hasTakesFromOlderSpec(motion({ spec_digest: 'new', candidates: [older] }))).toBe(true)
    expect(hasTakesFromOlderSpec(motion({ spec_digest: 'new', candidates: [older, current] }))).toBe(false)
    expect(hasTakesFromOlderSpec(motion({ spec_digest: 'new', candidates: [] }))).toBe(false)
    expect(hasTakesFromOlderSpec(motion({ candidates: [older] }))).toBe(false)
  })
})

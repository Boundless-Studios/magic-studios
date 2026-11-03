import { describe, expect, it } from 'vitest'
import type { MotionCandidate } from '../../api/types'
import { groupCandidatesIntoTakes } from './takeGrouping'

function candidate(overrides: Partial<MotionCandidate>): MotionCandidate {
  return {
    candidate_id: 'c', artifact_key: 'k', sha256: 'sha', duration_ms: 1000, metadata: null,
    assessment: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null,
    generation_settings: null, facing: 'right', recipe: null, source: 'generated', ...overrides,
  }
}

describe('groupCandidatesIntoTakes', () => {
  it('groups a clip with its keyed alpha sibling into one take', () => {
    const source = candidate({ candidate_id: 'a', duration_ms: 900, metadata: { has_alpha: false } as never })
    const keyed = candidate({ candidate_id: 'b', duration_ms: 900, metadata: { has_alpha: true } as never, recipe: { op: 'derive_alpha', key: '0x00B140', similarity: null, blend: null, target_frame_count: null, cell_size: null } })
    const groups = groupCandidatesIntoTakes([source, keyed])
    expect(groups).toHaveLength(1)
    expect(groups[0]).toMatchObject({ index: 1, primary: source, keyed })
  })

  it('keeps unrelated candidates as separate takes', () => {
    const a = candidate({ candidate_id: 'a' })
    const b = candidate({ candidate_id: 'b' })
    const groups = groupCandidatesIntoTakes([a, b])
    expect(groups).toEqual([{ index: 1, primary: a, keyed: null }, { index: 2, primary: b, keyed: null }])
  })

  it('does not group a derive_alpha candidate whose duration changed', () => {
    const source = candidate({ candidate_id: 'a', duration_ms: 900, metadata: { has_alpha: false } as never })
    const other = candidate({ candidate_id: 'b', duration_ms: 1200, metadata: { has_alpha: true } as never, recipe: { op: 'derive_alpha', key: '0x00B140', similarity: null, blend: null, target_frame_count: null, cell_size: null } })
    const groups = groupCandidatesIntoTakes([source, other])
    expect(groups).toHaveLength(2)
  })

  it('numbers takes counting grouped pairs once', () => {
    const a = candidate({ candidate_id: 'a', duration_ms: 900, metadata: { has_alpha: false } as never })
    const keyed = candidate({ candidate_id: 'b', duration_ms: 900, metadata: { has_alpha: true } as never, recipe: { op: 'derive_alpha', key: '0x00B140', similarity: null, blend: null, target_frame_count: null, cell_size: null } })
    const c = candidate({ candidate_id: 'c' })
    const groups = groupCandidatesIntoTakes([a, keyed, c])
    expect(groups.map((group) => group.index)).toEqual([1, 2])
  })
})

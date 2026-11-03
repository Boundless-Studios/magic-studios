import { beforeEach, describe, expect, it } from 'vitest'
import type { MotionCandidate } from '../../api/types'
import {
  clampFps,
  createFrameDraft,
  draftDurationMs,
  frameDraftStorageKey,
  frameDurationMsFromFps,
  frameDurationMsFromTotalDuration,
  fpsFromFrameDurationMs,
  holdForFrame,
  isFrameDraftDirty,
  loadFrameDraft,
  moveFrameTo,
  saveFrameDraft,
  setFrameHold,
  toggleFrame,
  totalDurationMsFor,
  applyCandidateTiming,
  validateFrameDraft,
} from './frameDraft'

const candidate: MotionCandidate = {
  candidate_id: 'candidate-1', artifact_key: 'motion.png', sha256: 'a',
  metadata: { decodable: true, media_kind: 'sheet', frame_count: 4, width: 256, height: 64, constant_geometry: true, has_alpha: true, duplicate_terminal_frame: false, baseline_y: null, packing_estimate: { profile: 'horizontal-strip-v1', width: 256, height: 64 } },
  assessment: null, duration_ms: 400, ordered_frames: null, frame_duration_ms: null, loop_policy: null,
  generation_settings: null, facing: 'right', recipe: null, source: 'generated',
}

beforeEach(() => sessionStorage.clear())

describe('frame drafts', () => {
  it('starts from measured legacy metadata and retains playback order while editing', () => {
    let draft = createFrameDraft(candidate, 'loop')
    draft = toggleFrame(draft, 1)
    draft = moveFrameTo(draft, 3, 0)

    expect(draft.orderedFrames).toEqual([3, 0, 2])
    expect(validateFrameDraft(draft, 4)).toEqual([])
    expect(isFrameDraftDirty(draft, candidate, 'loop')).toBe(true)
  })

  it('moves a frame later as well as earlier, by absolute playback position', () => {
    const draft = createFrameDraft(candidate, 'loop')
    expect(draft.orderedFrames).toEqual([0, 1, 2, 3])

    expect(moveFrameTo(draft, 0, 2).orderedFrames).toEqual([1, 2, 0, 3])
    expect(moveFrameTo(draft, 3, 0).orderedFrames).toEqual([3, 0, 1, 2])
  })

  it('bounds a move to the ends of the playback order and ignores an unincluded frame', () => {
    const draft = createFrameDraft(candidate, 'loop')

    expect(moveFrameTo(draft, 0, 99).orderedFrames).toEqual([1, 2, 3, 0])
    expect(moveFrameTo(draft, 3, -5).orderedFrames).toEqual([3, 0, 1, 2])
    expect(moveFrameTo(draft, 9, 0)).toEqual(draft)
  })

  it('round-trips one motion draft through session storage', () => {
    const key = frameDraftStorageKey('project-1', 'idle', candidate.candidate_id)
    const draft = toggleFrame(createFrameDraft(candidate, 'loop'), 2)

    saveFrameDraft(key, draft)

    expect(loadFrameDraft(key, candidate, 'loop')).toEqual(draft)
  })

  it('drops stale or corrupt drafts instead of applying them to another candidate', () => {
    const key = frameDraftStorageKey('project-1', 'idle', candidate.candidate_id)
    sessionStorage.setItem(key, JSON.stringify({ ...createFrameDraft(candidate, 'loop'), sourceCandidateId: 'older' }))
    expect(loadFrameDraft(key, candidate, 'loop')).toEqual(createFrameDraft(candidate, 'loop'))
    sessionStorage.setItem(key, '{')
    expect(loadFrameDraft(key, candidate, 'loop')).toEqual(createFrameDraft(candidate, 'loop'))
  })

  it('defaults flipHorizontal to false and toggles independently of frame order', () => {
    const draft = createFrameDraft(candidate, 'loop')
    expect(draft.flipHorizontal).toBe(false)
    expect({ ...draft, flipHorizontal: true }.orderedFrames).toEqual(draft.orderedFrames)
  })

  it('cross-derives total duration and FPS through the ported timing math', () => {
    expect(frameDurationMsFromFps(10)).toBe(100)
    expect(fpsFromFrameDurationMs(100)).toBe(10)
    expect(totalDurationMsFor(4, 100)).toBe(400)
    expect(frameDurationMsFromTotalDuration(400, 4)).toBe(100)
    expect(frameDurationMsFromFps(0)).toBeNull()
    expect(fpsFromFrameDurationMs(0)).toBeNull()
    expect(totalDurationMsFor(0, 100)).toBeNull()
    expect(frameDurationMsFromTotalDuration(400, 0)).toBeNull()
  })

  it('clamps FPS to the 1-60 authoring range', () => {
    expect(clampFps(0)).toBe(1)
    expect(clampFps(120)).toBe(60)
    expect(clampFps(24)).toBe(24)
    expect(clampFps(Number.NaN)).toBeGreaterThanOrEqual(1)
  })

  it('resets duration and FPS to the source candidate measured timing, leaving frame order alone', () => {
    let draft = createFrameDraft(candidate, 'loop')
    draft = toggleFrame(draft, 1)
    draft = { ...draft, frameDurationMs: 999 }

    const reset = applyCandidateTiming(draft, candidate)

    expect(reset.frameDurationMs).toBe(100)
    expect(reset.orderedFrames).toEqual(draft.orderedFrames)
  })

  it('rejects an empty, duplicate, out-of-range, or invalidly timed draft', () => {
    const draft = createFrameDraft(candidate, 'loop')
    expect(validateFrameDraft({ ...draft, orderedFrames: [] }, 4)).toContain('Select at least one frame.')
    expect(validateFrameDraft({ ...draft, orderedFrames: [0, 0] }, 4)).toContain('Each frame can appear only once.')
    expect(validateFrameDraft({ ...draft, orderedFrames: [0, 4] }, 4)).toContain('A selected frame is outside this candidate.')
    expect(validateFrameDraft({ ...draft, frameDurationMs: 0 }, 4)).toContain('Frame duration must be at least 1 ms.')
  })

  it('defaults every frame to a hold of 1 and extends draft duration by the sum of holds', () => {
    const draft = createFrameDraft(candidate, 'loop')
    expect(holdForFrame(draft, 0)).toBe(1)
    expect(draftDurationMs(draft)).toBe(400)

    const held = setFrameHold(draft, 1, 3)
    expect(holdForFrame(held, 1)).toBe(3)
    // (1 + 3 + 1 + 1) frames held at 100ms each.
    expect(draftDurationMs(held)).toBe(600)
  })

  it('clamps a frame hold to 1-8 and forgets it once excluded', () => {
    const draft = createFrameDraft(candidate, 'loop')
    expect(holdForFrame(setFrameHold(draft, 0, 20), 0)).toBe(8)
    expect(holdForFrame(setFrameHold(draft, 0, 0), 0)).toBe(1)

    const held = setFrameHold(draft, 2, 5)
    const excluded = toggleFrame(held, 2)
    expect(holdForFrame(excluded, 2)).toBe(1)
  })

  it('rejects a frame hold outside 1-8 when loaded from a corrupt draft', () => {
    const draft = createFrameDraft(candidate, 'loop')
    expect(validateFrameDraft({ ...draft, frameHolds: { 0: 9 } }, 4)).toContain('Frame hold must be between 1 and 8.')
    expect(validateFrameDraft({ ...draft, frameHolds: { 0: 0 } }, 4)).toContain('Frame hold must be between 1 and 8.')
  })
})

it('reinserts a toggled frame in source order instead of appending it to playback', () => {
  const draft = createFrameDraft(candidate, 'loop')
  expect(toggleFrame(toggleFrame(draft, 1), 1).orderedFrames).toEqual([0, 1, 2, 3])
})

it('restores a frame after its nearest source predecessor in a reordered draft', () => {
  const draft = { ...createFrameDraft(candidate, 'loop'), orderedFrames: [3, 0, 2] }
  expect(toggleFrame(draft, 1).orderedFrames).toEqual([3, 0, 1, 2])
})

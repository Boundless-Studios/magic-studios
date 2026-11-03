import type { LoopPolicy, MotionCandidate, NormalizationSpec } from '../../api/types'

const FRAME_DRAFT_SCHEMA_VERSION = 4
const MIN_HOLD = 1
const MAX_HOLD = 8
export const TARGET_FRAME_COUNT_CHOICES = [4, 8, 12, 16, 32] as const
export const CELL_SIZE_CHOICES = [256, 512, 1024] as const

export interface FrameDraft {
  schemaVersion: number
  sourceCandidateId: string
  loopRange?: { start: number; end: number }
  orderedFrames: number[]
  // frameIndex -> hold (1-8); a frame missing here holds for 1 step. Keyed by frame index
  // (not playback position) so it survives reordering.
  frameHolds: Record<number, number>
  frameDurationMs: number
  loopPolicy: LoopPolicy
  flipHorizontal: boolean
  // T19: null means "keep the source frames as-is" (reorder/hold-only save). Both are set
  // together or neither is -- the pair drives a resample to a new packed PNG on save.
  targetFrameCount: number | null
  cellSize: number | null
  /** One fixed transform for every chosen attack frame; absent on existing drafts. */
  normalization?: NormalizationSpec | null
}

export function createFrameDraft(candidate: MotionCandidate, fallbackLoop: LoopPolicy): FrameDraft {
  const frameCount = candidate.metadata?.frame_count ?? 0
  const orderedFrames = candidate.ordered_frames ?? Array.from({ length: frameCount }, (_, index) => index)
  const measuredFrameDuration = candidate.duration_ms && frameCount ? Math.max(1, Math.round(candidate.duration_ms / frameCount)) : 100
  const frameHolds: Record<number, number> = {}
  if (candidate.frame_holds) {
    orderedFrames.forEach((frameIndex, position) => {
      const hold = candidate.frame_holds?.[position]
      if (hold && hold !== 1) frameHolds[frameIndex] = Math.max(MIN_HOLD, Math.min(MAX_HOLD, Math.round(hold)))
    })
  }
  return {
    schemaVersion: FRAME_DRAFT_SCHEMA_VERSION,
    sourceCandidateId: candidate.candidate_id,
    orderedFrames: [...orderedFrames],
    frameHolds,
    frameDurationMs: candidate.frame_duration_ms ?? measuredFrameDuration,
    loopPolicy: candidate.loop_policy ?? fallbackLoop,
    // The saved candidate's own facing is what it *is*; the draft flag is whether this edit
    // toggles it again, so a freshly opened draft always starts un-toggled.
    flipHorizontal: false,
    targetFrameCount: null,
    cellSize: null,
  }
}

export function holdForFrame(draft: FrameDraft, frameIndex: number): number {
  return draft.frameHolds[frameIndex] ?? MIN_HOLD
}

export function setFrameHold(draft: FrameDraft, frameIndex: number, hold: number): FrameDraft {
  const bounded = Math.max(MIN_HOLD, Math.min(MAX_HOLD, Math.round(hold)))
  const frameHolds = { ...draft.frameHolds }
  if (bounded === MIN_HOLD) delete frameHolds[frameIndex]
  else frameHolds[frameIndex] = bounded
  return { ...draft, frameHolds }
}

/** Total playback duration: each included frame held for its hold count, at frameDurationMs per
 * step -- or, once a resample is staged (T19), the resampled frame count at that same rate,
 * since a resample bakes reorder/holds into one frame per packed cell. */
export function draftDurationMs(draft: FrameDraft): number {
  if (draft.targetFrameCount) return draft.targetFrameCount * draft.frameDurationMs
  const totalHold = draft.orderedFrames.reduce((sum, frameIndex) => sum + holdForFrame(draft, frameIndex), 0)
  return totalHold * draft.frameDurationMs
}

/** Stage or clear a resample (T19); the pair is set or cleared together. */
export function setPacking(draft: FrameDraft, targetFrameCount: number | null, cellSize: number | null): FrameDraft {
  return { ...draft, targetFrameCount, cellSize }
}

// Ported from the reference game's frontend/src/sprites/spriteAnimationTiming.js: total duration and FPS
// are two views of the same per-frame duration, cross-derived rather than stored twice.
export const MIN_FPS = 1
export const MAX_FPS = 60

export function frameDurationMsFromFps(fps: number): number | null {
  const numericFps = Number(fps)
  return numericFps > 0 && Number.isFinite(numericFps) ? 1000 / numericFps : null
}

export function fpsFromFrameDurationMs(frameDurationMs: number): number | null {
  const duration = Number(frameDurationMs)
  return duration > 0 && Number.isFinite(duration) ? 1000 / duration : null
}

export function totalDurationMsFor(frameCount: number, frameDurationMs: number): number | null {
  const count = Number(frameCount)
  const duration = Number(frameDurationMs)
  return count > 0 && duration > 0 && Number.isFinite(count) && Number.isFinite(duration) ? count * duration : null
}

export function frameDurationMsFromTotalDuration(durationMs: number, frameCount: number): number | null {
  const duration = Number(durationMs)
  const count = Number(frameCount)
  return duration > 0 && count > 0 && Number.isFinite(duration) && Number.isFinite(count) ? duration / count : null
}

export function clampFps(fps: number): number {
  const numericFps = Number(fps)
  if (!Number.isFinite(numericFps)) return MIN_FPS
  return Math.min(MAX_FPS, Math.max(MIN_FPS, numericFps))
}

/** Reset only the timing fields to the source candidate's measured values; frame order and flip are untouched. */
export function applyCandidateTiming(draft: FrameDraft, candidate: MotionCandidate): FrameDraft {
  const frameCount = candidate.metadata?.frame_count ?? 0
  const measuredFrameDuration = candidate.duration_ms && frameCount ? Math.max(1, Math.round(candidate.duration_ms / frameCount)) : 100
  return { ...draft, frameDurationMs: candidate.frame_duration_ms ?? measuredFrameDuration }
}

export function toggleFrame(draft: FrameDraft, frameIndex: number): FrameDraft {
  const included = draft.orderedFrames.includes(frameIndex)
  // Use the nearest source neighbor, not the first greater entry in playback order.
  const predecessor = Math.max(-1, ...draft.orderedFrames.filter((index) => index < frameIndex))
  const successor = Math.min(Infinity, ...draft.orderedFrames.filter((index) => index > frameIndex))
  const insertionPosition = predecessor >= 0 ? draft.orderedFrames.indexOf(predecessor) + 1
    : Number.isFinite(successor) ? draft.orderedFrames.indexOf(successor) : draft.orderedFrames.length
  const frameHolds = { ...draft.frameHolds }
  if (included) delete frameHolds[frameIndex]
  return {
    ...draft,
    orderedFrames: included
      ? draft.orderedFrames.filter((index) => index !== frameIndex)
      : [...draft.orderedFrames.slice(0, insertionPosition), frameIndex, ...draft.orderedFrames.slice(insertionPosition)],
    frameHolds,
  }
}

/** Move an included frame to an absolute playback position (0-indexed), in either direction. */
export function moveFrameTo(draft: FrameDraft, frameIndex: number, position: number): FrameDraft {
  const currentPosition = draft.orderedFrames.indexOf(frameIndex)
  if (currentPosition === -1) return draft
  const orderedFrames = [...draft.orderedFrames]
  orderedFrames.splice(currentPosition, 1)
  const boundedTarget = Math.max(0, Math.min(position, orderedFrames.length))
  orderedFrames.splice(boundedTarget, 0, frameIndex)
  return { ...draft, orderedFrames }
}

export function validateFrameDraft(draft: FrameDraft, frameCount: number): string[] {
  const errors: string[] = []
  if (draft.loopRange && (!Number.isInteger(draft.loopRange.start) || !Number.isInteger(draft.loopRange.end) || draft.loopRange.start < 0 || draft.loopRange.end >= frameCount || draft.loopRange.end <= draft.loopRange.start)) errors.push('Loop boundaries are outside this candidate.')
  if (draft.orderedFrames.length === 0) errors.push('Select at least one frame.')
  if (new Set(draft.orderedFrames).size !== draft.orderedFrames.length) errors.push('Each frame can appear only once.')
  if (draft.orderedFrames.some((index) => !Number.isInteger(index) || index < 0 || index >= frameCount)) errors.push('A selected frame is outside this candidate.')
  if (!Number.isInteger(draft.frameDurationMs) || draft.frameDurationMs < 1) errors.push('Frame duration must be at least 1 ms.')
  if (Object.values(draft.frameHolds).some((hold) => !Number.isInteger(hold) || hold < MIN_HOLD || hold > MAX_HOLD)) errors.push('Frame hold must be between 1 and 8.')
  if ((draft.targetFrameCount === null) !== (draft.cellSize === null)) errors.push('Choose both a frame count and a cell size to resample, or neither.')
  if (draft.normalization) {
    const { scale, source_anchor_x, source_anchor_y, target_anchor_x, target_anchor_y } = draft.normalization
    if (![scale, source_anchor_x, source_anchor_y, target_anchor_x, target_anchor_y].every(Number.isFinite) || scale <= 0 || scale > 8 || [source_anchor_x, source_anchor_y, target_anchor_x, target_anchor_y].some((value) => value < 0)) errors.push('Body scale and origin must be valid positive values.')
    if (draft.normalization.padding !== undefined && (!Number.isInteger(draft.normalization.padding) || draft.normalization.padding < 0 || draft.normalization.padding > 256)) errors.push('Overhang padding must be a whole number from 0 to 256 pixels.')
    if (!draft.targetFrameCount || !draft.cellSize) errors.push('Choose a frame count and size for fixed framing.')
  }
  return errors
}

export function isFrameDraftDirty(draft: FrameDraft, candidate: MotionCandidate, fallbackLoop: LoopPolicy): boolean {
  return JSON.stringify(draft) !== JSON.stringify(createFrameDraft(candidate, fallbackLoop))
}

export function frameDraftStorageKey(projectId: string, motionId: string, sourceCandidateId: string): string {
  return `studio-frame-draft:${projectId}:${motionId}:${sourceCandidateId}`
}

export function saveFrameDraft(key: string, draft: FrameDraft): void {
  sessionStorage.setItem(key, JSON.stringify(draft))
}

export function loadFrameDraft(key: string, candidate: MotionCandidate, fallbackLoop: LoopPolicy): FrameDraft {
  const clean = createFrameDraft(candidate, fallbackLoop)
  const stored = sessionStorage.getItem(key)
  if (!stored) return clean
  try {
    const parsed = JSON.parse(stored) as FrameDraft
    if (
      parsed.schemaVersion !== FRAME_DRAFT_SCHEMA_VERSION
      || parsed.sourceCandidateId !== candidate.candidate_id
      || validateFrameDraft(parsed, candidate.metadata?.frame_count ?? 0).length > 0
    ) {
      sessionStorage.removeItem(key)
      return clean
    }
    return parsed
  } catch {
    sessionStorage.removeItem(key)
    return clean
  }
}

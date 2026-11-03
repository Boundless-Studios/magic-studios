import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { MotionCandidate } from '../../api/types'
import { studioApi } from '../../api/client'
import { FrameEditor } from './FrameEditor'
import { matchWalkBody } from './bodyCalibration'
import { useVideoThumbnails } from './useVideoThumbnails'
vi.mock('./useVideoThumbnails', () => ({ useVideoThumbnails: vi.fn() }))
vi.mock('./bodyCalibration', () => ({ matchWalkBody: vi.fn() }))
const candidate = {
  candidate_id: 'walk-source', artifact_key: 'walk.mp4', duration_ms: 2708,
  metadata: { media_kind: 'video', frame_count: 65, width: 512, height: 512 },
} as MotionCandidate
const empty = { url: '/walk.mp4', frames: [], poses: [], duration: 0, failed: false }
const loaded = {
  url: '/walk.mp4',
  frames: Array.from({ length: 65 }, (_, i) => `/frame-${i}.png`),
  poses: Array.from({ length: 65 }, (_, i) => [Math.cos(i * Math.PI / 12), Math.sin(i * Math.PI / 12)]),
  duration: 65 / 24, failed: false,
}
function editor(onSave = vi.fn()) {
  return <FrameEditor projectId="loop-test" motionId="walk" motionName="Walk" candidate={candidate} previewUrl="/walk.mp4" fallbackLoop="loop" onSave={onSave} />
}
beforeEach(() => { sessionStorage.clear(); vi.mocked(useVideoThumbnails).mockReturnValue(empty); vi.spyOn(studioApi, 'suggestCandidateLoop').mockResolvedValue({ analysis_version: 'pose-cycle-v2', mode: 'walk', candidate_id: 'walk-source', source_sha256: 'test', status: 'complete', start: 12, end: 36, ordered_frames: Array.from({ length: 16 }, (_, index) => 12 + Math.floor(index * 24 / 16)), frame_duration_ms: 63, loop_policy: 'loop', edge_clipped_frames: [], warnings: ['Visual review required.'], visual_review_required: true }) })
afterEach(() => { vi.restoreAllMocks() })
it('requests native walk analysis before thumbnails load and applies its proposed cycle', async () => {
  const onSave = vi.fn().mockResolvedValue(undefined)
  const view = render(editor(onSave))
  fireEvent.click(screen.getByRole('button', { name: 'Suggest walk loop' }))
  expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Preparing 65 source frames')
  expect(screen.getByRole('button', { name: 'Suggest walk loop' })).toBeDisabled()
  vi.mocked(useVideoThumbnails).mockReturnValue(loaded)
  view.rerender(editor(onSave))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Suggested frames'))
  const start = Number((screen.getByLabelText('Loop start frame') as HTMLInputElement).value)
  const end = Number((screen.getByLabelText('Loop matching end frame') as HTMLInputElement).value)
  expect(end - start).toBe(24)
  expect(studioApi.suggestCandidateLoop).toHaveBeenCalledWith('loop-test', 'walk', 'walk-source', 'walk', 16)
  expect(screen.getByRole('button', { name: 'Pause' })).toBeEnabled()
  fireEvent.change(screen.getByLabelText('Loop output frames'), { target: { value: '12' } })
  expect(screen.getAllByRole('img', { name: /^Source frame / })).toHaveLength(12)
  expect(screen.getByRole('button', { name: 'Selected frames (12)' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ targetFrameCount: 12, orderedFrames: Array.from({ length: 12 }, (_, i) => start - 1 + i * 2), frameDurationMs: 83 }))
})
it('shows a visible failure instead of silently keeping the full source range', async () => {
  vi.mocked(studioApi.suggestCandidateLoop).mockRejectedValueOnce(new Error('Native decode failed'))
  vi.mocked(useVideoThumbnails).mockReturnValue({ ...empty, failed: true })
  render(editor())
  fireEvent.click(screen.getByRole('button', { name: 'Suggest walk loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Walk analysis unavailable'))
  expect(screen.getByRole('button', { name: 'Suggest walk loop' })).toBeEnabled()
})

it('steps through selected poses and blocks saving stale boundary edits', async () => {
  vi.mocked(useVideoThumbnails).mockReturnValue(loaded)
  render(editor())
  fireEvent.click(screen.getByRole('button', { name: 'Suggest walk loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Suggested frames'))
  const start = Number((screen.getByLabelText('Loop start frame') as HTMLInputElement).value)
  fireEvent.change(screen.getByLabelText('Loop output frames'), { target: { value: '8' } })
  fireEvent.change(screen.getByLabelText('Current frame'), { target: { value: '0' } })
  expect(screen.getByRole('button', { name: 'Previous frame' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Next frame' }))
  expect(screen.getByRole('button', { name: `Preview frame ${start + 3}` })).toHaveAttribute('aria-current', 'true')
  fireEvent.click(screen.getByRole('button', { name: `Preview frame ${start + 3}` }))
  expect(screen.getAllByRole('img', { name: /^Source frame / })).toHaveLength(8)
  fireEvent.change(screen.getByLabelText('Loop start frame'), { target: { value: String(start + 1) } })
  expect(screen.getByRole('button', { name: 'Save revision' })).toBeDisabled()
  expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Boundaries changed')
  fireEvent.click(screen.getByRole('button', { name: 'Preview selected loop' }))
  expect(screen.getByRole('button', { name: 'Save revision' })).toBeEnabled()
})

it('keeps an unfinished attack as a one-shot and does not call it a complete loop', async () => {
  vi.spyOn(studioApi, 'suggestCandidateLoop').mockResolvedValueOnce({ analysis_version: 'pose-cycle-v2', mode: 'attack', candidate_id: 'attack-source', source_sha256: 'test', status: 'incomplete', start: null, end: null, ordered_frames: [], frame_duration_ms: null, loop_policy: 'loop', edge_clipped_frames: [], warnings: ['No complete pose cycle found.'], visual_review_required: true })
  const attackCandidate = { ...candidate, candidate_id: 'attack-source', artifact_key: 'attack.mp4', metadata: { ...candidate.metadata!, frame_count: 65 } } as MotionCandidate
  const onSave = vi.fn().mockResolvedValue(undefined)
  const windup = [
    ...Array(15).fill(0),
    ...Array.from({ length: 12 }, (_, index) => index / 11 * 0.15),
    ...Array(35).fill(0.15),
    0.13, 0.11, 0.09,
  ]
  vi.mocked(useVideoThumbnails).mockReturnValue({ url: '/attack.mp4', frames: Array.from({ length: 65 }, (_, index) => `/attack-${index}.png`), poses: windup.map((value) => [value, value]), duration: 65 / 24, failed: false })
  render(<FrameEditor projectId="loop-test" motionId="attack" motionName="Attack" motionPreset="attack" candidate={attackCandidate} previewUrl="/attack.mp4" fallbackLoop="once" onSave={onSave} />)
  expect(screen.queryByRole('button', { name: 'Suggest walk loop' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Suggest attack loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('No complete attack and recovery found'))
  fireEvent.click(screen.getByRole('button', { name: 'Preview selected attack' }))
  expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('one-shot')
  fireEvent.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ loopPolicy: 'once', targetFrameCount: 16 }))
})

it('saves the native complete attack as a one-shot with its exact source indices and timing', async () => {
  const attackCandidate = { ...candidate, candidate_id: 'attack-complete', artifact_key: 'attack.mp4', metadata: { ...candidate.metadata!, frame_count: 65 } } as MotionCandidate
  const ordered = Array.from({ length: 16 }, (_, index) => 12 + Math.floor(index * 38 / 16))
  vi.spyOn(studioApi, 'suggestCandidateLoop').mockResolvedValueOnce({ analysis_version: 'pose-cycle-v2', mode: 'attack', candidate_id: 'attack-complete', source_sha256: 'test', status: 'complete', start: 12, end: 50, ordered_frames: ordered, frame_duration_ms: 99, loop_policy: 'once', edge_clipped_frames: [], warnings: ['Visual review required.'], visual_review_required: true })
  vi.mocked(useVideoThumbnails).mockReturnValue({ url: '/attack.mp4', frames: Array.from({ length: 65 }, (_, index) => `/attack-${index}.png`), poses: Array.from({ length: 65 }, () => [0, 0]), duration: 65 / 24, failed: false })
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="loop-test" motionId="attack" motionName="Attack" motionPreset="attack" candidate={attackCandidate} previewUrl="/attack.mp4" fallbackLoop="once" onSave={onSave} />)
  fireEvent.click(screen.getByRole('button', { name: 'Suggest attack loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Suggested attack frames 13–51'))
  expect(studioApi.suggestCandidateLoop).toHaveBeenCalledWith('loop-test', 'attack', 'attack-complete', 'attack', 16)
  fireEvent.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ orderedFrames: ordered, frameDurationMs: 99, loopPolicy: 'once' }))
})

it.each(['once', 'loop', 'ping_pong'] as const)('preserves the selected %s policy through attack resampling, preview, and saving', async (loopPolicy) => {
  vi.mocked(studioApi.suggestCandidateLoop).mockResolvedValueOnce({ analysis_version: 'native-attack-v1', mode: 'attack', candidate_id: 'attack-choice', source_sha256: 'test', status: 'complete', start: 12, end: 50, ordered_frames: Array.from({ length: 16 }, (_, index) => 12 + index * 2), frame_duration_ms: 99, loop_policy: 'once', edge_clipped_frames: [], warnings: [], visual_review_required: true })
  vi.mocked(useVideoThumbnails).mockReturnValue(loaded)
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="loop-test" motionId="attack" motionName="Attack" motionPreset="attack" candidate={{ ...candidate, candidate_id: 'attack-choice' }} previewUrl="/attack.mp4" fallbackLoop="once" onSave={onSave} />)
  fireEvent.click(screen.getByRole('button', { name: 'Suggest attack loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Suggested attack frames'))
  fireEvent.click(screen.getByText('More frame controls'))
  expect(screen.getByLabelText('Loop mode')).toHaveValue('once')
  fireEvent.change(screen.getByLabelText('Loop mode'), { target: { value: loopPolicy } })
  fireEvent.change(screen.getByLabelText('Attack output frames'), { target: { value: '12' } })
  expect(screen.getByLabelText('Loop mode')).toHaveValue(loopPolicy)
  fireEvent.click(screen.getByRole('button', { name: 'Preview selected attack' }))
  expect(screen.getByLabelText('Loop mode')).toHaveValue(loopPolicy)
  fireEvent.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ loopPolicy, targetFrameCount: 12 }))
})

it('anchors attack scale to its selected ready pose after the opening frames settle', async () => {
  const alphaCandidate = { ...candidate, candidate_id: 'attack-alpha', artifact_key: 'attack.webm', metadata: { ...candidate.metadata!, frame_count: 65, has_alpha: true } } as MotionCandidate
  const walkReference = { ...candidate, candidate_id: 'selected-walk', artifact_key: 'walk.png', metadata: { ...candidate.metadata!, media_kind: 'sheet', frame_count: 12 } } as MotionCandidate
  const ordered = Array.from({ length: 16 }, (_, index) => 12 + Math.floor(index * 38 / 16))
  vi.spyOn(studioApi, 'suggestCandidateLoop').mockResolvedValueOnce({ analysis_version: 'pose-cycle-v2', mode: 'attack', candidate_id: 'attack-alpha', source_sha256: 'test', status: 'complete', start: 12, end: 50, ordered_frames: ordered, frame_duration_ms: 99, loop_policy: 'loop', edge_clipped_frames: [], warnings: [], visual_review_required: true })
  vi.mocked(useVideoThumbnails).mockReturnValue({ url: '/attack.webm', frames: Array.from({ length: 65 }, (_, index) => `/attack-${index}.png`), poses: Array.from({ length: 65 }, () => [0, 0]), duration: 65 / 24, failed: false })
  vi.mocked(matchWalkBody).mockImplementation(async (frameUrl) => {
    await new Promise((resolve) => setTimeout(resolve, 25))
    return { scale: frameUrl.includes('-12.') ? 0.72 : 0.5, source_anchor_x: 256, source_anchor_y: frameUrl.includes('-12.') ? 436 : 402, target_anchor_x: 128, target_anchor_y: 224 }
  })
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="loop-test" motionId="attack" motionName="Attack" motionPreset="attack" candidate={alphaCandidate} previewUrl="/attack.webm" walkReference={walkReference} walkReferenceUrl="/walk.png" fallbackLoop="once" onSave={onSave} />)
  fireEvent.click(screen.getByRole('button', { name: 'Suggest attack loop' }))
  await waitFor(() => expect(matchWalkBody).toHaveBeenCalledWith('/attack-12.png', '/walk.png', 12))
  await waitFor(() => expect(screen.getByLabelText('Attack body scale')).toHaveValue(0.72))
  fireEvent.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ normalization: expect.objectContaining({ scale: 0.72, source_anchor_y: 436 }) }))
})

it('preserves the native walk proposal indices and measured timing without a client pose pass', async () => {
  const ordered = [3, 4, 6, 7, 8, 10, 12, 14, 15, 17, 18, 19, 21, 22, 23, 25]
  vi.mocked(studioApi.suggestCandidateLoop).mockResolvedValueOnce({ analysis_version: 'native-walk-v3', mode: 'walk', candidate_id: 'walk-source', source_sha256: 'test', status: 'complete', start: 3, end: 27, ordered_frames: ordered, frame_duration_ms: 71, loop_policy: 'loop', edge_clipped_frames: [], warnings: ['Review cadence.'], visual_review_required: true })
  vi.mocked(useVideoThumbnails).mockReturnValue({ ...loaded, poses: [] })
  const save = vi.fn().mockResolvedValue(undefined)
  render(editor(save))
  fireEvent.click(screen.getByRole('button', { name: 'Suggest walk loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('native source analysis'))
  expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('Review cadence')
  fireEvent.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ orderedFrames: ordered, frameDurationMs: 71, loopPolicy: 'loop' }))
})

it('keeps manual walk boundaries available when native analysis finds no complete cycle', async () => {
  vi.mocked(studioApi.suggestCandidateLoop).mockResolvedValueOnce({ analysis_version: 'native-walk-v3', mode: 'walk', candidate_id: 'walk-source', source_sha256: 'test', status: 'incomplete', start: null, end: null, ordered_frames: [], frame_duration_ms: null, loop_policy: 'loop', edge_clipped_frames: [], warnings: ['No reciprocal stride.'], visual_review_required: true })
  vi.mocked(useVideoThumbnails).mockReturnValue(loaded)
  render(editor())
  fireEvent.click(screen.getByRole('button', { name: 'Suggest walk loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('No complete walk cycle found. No reciprocal stride.'))
  expect(screen.getByLabelText('Loop start frame')).toBeEnabled()
  expect(screen.getByLabelText('Loop matching end frame')).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Preview selected loop' })).toBeEnabled()
})

it('uses native idle breathing analysis and preserves its exact frames and timing', async () => {
  const ordered = [8, 9, 11, 12, 14, 15, 17, 18, 20, 21, 23, 24, 26, 27, 29, 30]
  vi.mocked(studioApi.suggestCandidateLoop).mockResolvedValueOnce({ analysis_version: 'native-idle-v1', mode: 'idle', candidate_id: 'idle-source', source_sha256: 'test', status: 'complete', start: 8, end: 32, ordered_frames: ordered, frame_duration_ms: 70, loop_policy: 'loop', edge_clipped_frames: [], warnings: ['Review breathing cadence.'], visual_review_required: true })
  vi.mocked(useVideoThumbnails).mockReturnValue(loaded)
  const save = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="loop-test" motionId="idle" motionName="Idle" motionPreset="idle" candidate={{ ...candidate, candidate_id: 'idle-source' }} previewUrl="/idle.mp4" fallbackLoop="loop" onSave={save} />)
  expect(screen.queryByRole('button', { name: 'Suggest walk loop' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Suggest idle loop' }))
  await waitFor(() => expect(screen.getByRole('status', { name: 'Loop analysis status' })).toHaveTextContent('subtle breathing'))
  expect(studioApi.suggestCandidateLoop).toHaveBeenCalledWith('loop-test', 'idle', 'idle-source', 'idle', 16)
  fireEvent.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ orderedFrames: ordered, frameDurationMs: 70, loopPolicy: 'loop' }))
})

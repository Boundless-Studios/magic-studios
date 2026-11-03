import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import type { MotionCandidate } from '../../api/types'
import { FrameEditor } from './FrameEditor'

const candidate: MotionCandidate = {
  candidate_id: 'candidate-1', artifact_key: 'idle.png', sha256: 'a', duration_ms: 400,
  metadata: { decodable: true, media_kind: 'sheet', frame_count: 4, width: 256, height: 64, constant_geometry: true, has_alpha: true, duplicate_terminal_frame: false, baseline_y: null, packing_estimate: { profile: 'horizontal-strip-v1', width: 256, height: 64 } },
  assessment: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null,
  generation_settings: null, facing: 'right', recipe: null, source: 'generated',
}

beforeEach(() => { vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {}); sessionStorage.clear(); vi.stubGlobal('PointerEvent', MouseEvent) })

it('inspects, excludes, reorders, and persists a local draft without saving', async () => {
  const user = userEvent.setup()
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)

  fireEvent.change(screen.getByLabelText('Current frame'), { target: { value: '3' } })
  expect(screen.getByRole('status', { name: 'Playback position' })).toHaveTextContent('Frame 4 of 4')
  await user.click(screen.getByRole('button', { name: 'Exclude frame 2' }))
  fireEvent.change(screen.getByLabelText('Current frame'), { target: { value: '3' } })
  await user.click(screen.getByText('More frame controls'))
  await user.click(screen.getByRole('button', { name: 'Move frame 4 earlier' }))

  expect(screen.getByText('Draft · 3 of 4 frames')).toBeInTheDocument()
  expect(sessionStorage.getItem('studio-frame-draft:project-1:idle:candidate-1')).toContain('"orderedFrames":[0,3,2]')
  expect(screen.getByRole('button', { name: 'Save revision' })).toBeEnabled()
})

it('saves explicitly and discards back to persisted candidate state', async () => {
  const user = userEvent.setup()
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={onSave} />)

  await user.click(screen.getByRole('button', { name: 'Exclude frame 2' }))
  await user.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ orderedFrames: [0, 2, 3] }))

  await user.click(screen.getByRole('button', { name: 'Exclude frame 3' }))
  await user.click(screen.getByRole('button', { name: 'Discard draft' }))
  expect(screen.getByText('Saved · 4 frames')).toBeInTheDocument()
})

it('keeps an invalid draft available for correction', async () => {
  const user = userEvent.setup()
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, metadata: { ...candidate.metadata!, frame_count: 1, width: 64 } }} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)

  await user.click(screen.getByRole('button', { name: 'Exclude frame 1' }))

  expect(screen.getByRole('alert')).toHaveTextContent('Select at least one frame.')
  expect(screen.getByRole('button', { name: 'Save revision' })).toBeDisabled()
})

it('uses a seekable video surface for candidates measured as video', () => {
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, artifact_key: 'idle.mp4', metadata: { ...candidate.metadata!, media_kind: 'video' } }} previewUrl="https://assets.invalid/idle.mp4" fallbackLoop="loop" onSave={vi.fn()} />)

  expect(screen.getByLabelText('Idle frame preview')).toHaveAttribute('src', 'https://assets.invalid/idle.mp4')
})

it('renders the filmstrip for a sheet even when its stored key ends in .mp4', () => {
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, artifact_key: 'idle.mp4', ordered_frames: [0, 3, 2] }} previewUrl="/local-assets/idle.mp4" fallbackLoop="loop" onSave={vi.fn()} />)

  expect(screen.getByText('Saved · 3 frames')).toBeInTheDocument()
  expect(screen.queryByLabelText('Idle frame preview')).not.toBeInTheDocument()
  expect(screen.getByRole('img', { name: 'Idle, frame 1' })).toBeInTheDocument()
})

it('holds a frame for extra steps, extending draft duration, and saves the hold', async () => {
  const user = userEvent.setup()
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={onSave} />)

  await user.click(screen.getByText('More frame controls'))
  fireEvent.change(screen.getByLabelText('Current frame'), { target: { value: '1' } })
  expect(screen.getByText('0.40 s')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Hold for frame 2'), { target: { value: '3' } })
  // (1 + 3 + 1 + 1) frames at 100ms each.
  expect(screen.getByText('0.60 s')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ frameHolds: { 1: 3 } }))
})

it('honors a frame hold during playback, showing each held frame for hold x frameDurationMs (T7)', () => {
  vi.useFakeTimers()
  try {
    render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)

    // Frame 2 (index 1) holds for 3 steps of 100ms: it must stay on screen for 300ms before
    // advancing, not the single frameDurationMs the un-expanded playback sequence used to give it.
    fireEvent.click(screen.getByText('More frame controls'))
    fireEvent.change(screen.getByLabelText('Current frame'), { target: { value: '1' } })
    fireEvent.change(screen.getByLabelText('Hold for frame 2'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Play' }))
    expect(screen.getByRole('status', { name: 'Playback position' })).toHaveTextContent('Frame 2 of 4')

    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('status', { name: 'Playback position' })).toHaveTextContent('Frame 2 of 4')
    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('status', { name: 'Playback position' })).toHaveTextContent('Frame 2 of 4')
    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('status', { name: 'Playback position' })).toHaveTextContent('Frame 3 of 4')
  } finally {
    vi.useRealTimers()
  }
})

it('cross-derives total duration and FPS, with a reset to candidate timing', async () => {
  const user = userEvent.setup()
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)

  expect(screen.getByLabelText('Total duration (ms)')).toHaveValue(400)
  expect(screen.getByLabelText('Frames per second')).toHaveValue(10)

  fireEvent.change(screen.getByLabelText('Frames per second'), { target: { value: '20' } })
  expect(screen.getByLabelText('Total duration (ms)')).toHaveValue(200)

  fireEvent.change(screen.getByLabelText('Total duration (ms)'), { target: { value: '800' } })
  expect(screen.getByLabelText('Frames per second')).toHaveValue(5)

  await user.click(screen.getByText('More frame controls'))
  await user.click(screen.getByRole('button', { name: 'Use candidate timing' }))
  expect(screen.getByLabelText('Total duration (ms)')).toHaveValue(400)
  expect(screen.getByLabelText('Frames per second')).toHaveValue(10)
})

it('mirrors the preview stage and saves flip_horizontal via the draft', async () => {
  const user = userEvent.setup()
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={onSave} />)

  const stage = screen.getByRole('img', { name: 'Idle, frame 1' })
  expect(stage).not.toHaveStyle({ transform: 'scaleX(-1)' })

  await user.click(screen.getByLabelText('Flip horizontally'))
  expect(stage).toHaveStyle({ transform: 'scaleX(-1)' })

  await user.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ flipHorizontal: true }))
})

it('mirrors the video preview too, for candidates measured as video (T8)', async () => {
  const user = userEvent.setup()
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, artifact_key: 'idle.mp4', metadata: { ...candidate.metadata!, media_kind: 'video' } }} previewUrl="https://assets.invalid/idle.mp4" fallbackLoop="loop" onSave={vi.fn()} />)

  const video = screen.getByLabelText('Idle frame preview')
  expect(video).not.toHaveStyle({ transform: 'scaleX(-1)' })

  await user.click(screen.getByLabelText('Flip horizontally'))
  expect(video).toHaveStyle({ transform: 'scaleX(-1)' })
})

it('moves the focused frame with Alt+ArrowLeft and Alt+ArrowRight', async () => {
  const user = userEvent.setup()
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)

  const inspectFrame3 = screen.getByRole('button', { name: 'Exclude frame 3' })
  inspectFrame3.focus()
  await user.keyboard('{Alt>}{ArrowLeft}{/Alt}')

  expect(sessionStorage.getItem('studio-frame-draft:project-1:idle:candidate-1')).toContain('"orderedFrames":[0,2,1,3]')
})

it('resets the editor to a newly saved candidate revision', async () => {
  const user = userEvent.setup()
  const { rerender } = render(<FrameEditor key={candidate.candidate_id} projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="/local-assets/idle.mp4?media=image" fallbackLoop="loop" onSave={vi.fn()} />)
  await user.click(screen.getByRole('button', { name: 'Exclude frame 2' }))
  expect(screen.getByText('Draft · 3 of 4 frames')).toBeInTheDocument()

  rerender(<FrameEditor key="candidate-2" projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, candidate_id: 'candidate-2', ordered_frames: [0, 2, 3] }} previewUrl="/local-assets/idle.mp4?media=image" fallbackLoop="loop" onSave={vi.fn()} />)

  expect(await screen.findByText('Saved · 3 frames')).toBeInTheDocument()
})

it('stages a resample and saves target_frame_count/cell_size, reflected in draft duration (T19)', async () => {
  const user = userEvent.setup()
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="https://assets.invalid/idle.png" fallbackLoop="loop" onSave={onSave} />)

  await user.click(screen.getByText('More frame controls'))
  await user.selectOptions(screen.getByRole('combobox', { name: 'Frame count' }), '16')
  await user.selectOptions(screen.getByRole('combobox', { name: 'Cell size' }), '512')
  // Resample bakes the holds into 16 packed cells at the candidate's 100ms frame rate.
  expect(screen.getByText('1.60 s')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ targetFrameCount: 16, cellSize: 512 }))
})

it('selects a dragged range and toggles a thumbnail with the keyboard', () => {
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={candidate} previewUrl="/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)
  const first = screen.getByRole('button', { name: 'Exclude frame 2' })
  const last = screen.getByRole('button', { name: 'Exclude frame 4' })
  fireEvent.pointerDown(first, { button: 0, pointerId: 1 })
  fireEvent.pointerEnter(last, { pointerId: 1 })
  fireEvent.pointerUp(last, { pointerId: 1 })
  expect(sessionStorage.getItem('studio-frame-draft:project-1:idle:candidate-1')).toContain('"orderedFrames":[1,2,3]')
  fireEvent.click(screen.getByRole('button', { name: 'Exclude frame 3' }), { detail: 0 })
  expect(sessionStorage.getItem('studio-frame-draft:project-1:idle:candidate-1')).toContain('"orderedFrames":[1,3]')
})

it('keeps reordered playback while a dragged range changes direction', () => {
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, ordered_frames: [3, 0, 2, 1] }} previewUrl="/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Exclude frame 4' }), { button: 0 })
  fireEvent.pointerEnter(screen.getByRole('button', { name: 'Exclude frame 2' }))
  expect(sessionStorage.getItem('studio-frame-draft:project-1:idle:candidate-1')).toContain('"orderedFrames":[3,2,1]')
  fireEvent.pointerEnter(screen.getByRole('button', { name: 'Include frame 1' }))
  expect(sessionStorage.getItem('studio-frame-draft:project-1:idle:candidate-1')).toContain('"orderedFrames":[3,0,2,1]')
})

it('counts hold ticks when choosing the default output frame count for resizing', async () => {
  const user = userEvent.setup()
  const onSave = vi.fn().mockResolvedValue(undefined)
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, frame_holds: [3, 1, 1, 1] }} previewUrl="/idle.png" fallbackLoop="loop" onSave={onSave} />)
  await user.selectOptions(screen.getByRole('combobox', { name: 'Cell size' }), '256')
  await user.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ targetFrameCount: 8 }))
})

it('reports expanded output cells and clears holds when a dragged range is finalized', () => {
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, frame_holds: [3, 1, 1, 1] }} previewUrl="/idle.png" fallbackLoop="loop" onSave={vi.fn()} />)
  expect(screen.getByText('6 output frames')).toBeInTheDocument()
  fireEvent.pointerDown(screen.getByRole('button', { name: 'Exclude frame 2' }), { button: 0 })
  fireEvent.pointerEnter(screen.getByRole('button', { name: 'Exclude frame 4' }))
  fireEvent.pointerUp(window)
  fireEvent.click(screen.getByRole('button', { name: 'Include frame 1' }), { detail: 0 })
  expect(screen.getByText('4 output frames')).toBeInTheDocument()
  expect(sessionStorage.getItem('studio-frame-draft:project-1:idle:candidate-1')).toContain('"frameHolds":{}')
})


it('seeks source frames independently of edited playback FPS', () => {
  render(<FrameEditor projectId="project-1" motionId="idle" motionName="Idle" candidate={{ ...candidate, metadata: { ...candidate.metadata!, media_kind: 'video' } }} previewUrl="/idle.mp4" fallbackLoop="loop" onSave={vi.fn()} />)
  const video = screen.getByLabelText('Idle frame preview') as HTMLVideoElement
  Object.defineProperty(video, 'duration', { value: 2 })
  fireEvent.loadedMetadata(video)
  fireEvent.change(screen.getByLabelText('Current frame'), { target: { value: '2' } })
  expect(video.currentTime).toBe(1.125)
  fireEvent.change(screen.getByLabelText('Frames per second'), { target: { value: '60' } })
  expect(video.currentTime).toBe(1.125)
})

it('puts loop extraction first and keeps playback timing in advanced controls', () => {
  render(<FrameEditor projectId="project-1" motionId="walk" motionName="Walk" candidate={{ ...candidate, metadata: { ...candidate.metadata!, media_kind: 'video' } }} previewUrl="/walk.mp4" fallbackLoop="loop" onSave={vi.fn()} />)
  const settings = screen.getByRole('region', { name: 'Loop extraction' }).parentElement!
  expect(settings.firstElementChild).toHaveAttribute('aria-label', 'Loop extraction')
  expect(screen.getByRole('button', { name: 'Suggest walk loop' })).toBeVisible()
  expect(screen.getByRole('combobox', { name: 'Loop output frames' })).toBeVisible()
  expect(screen.queryByRole('combobox', { name: 'Frame count' })).not.toBeInTheDocument()
  const advanced = screen.getByLabelText('Frames per second').closest('details')!
  expect(advanced.open).toBe(false)
  expect(screen.getByLabelText('Total duration (ms)').closest('details')).toBe(advanced)
  expect(screen.getByRole('region', { name: 'Sprite output' })).toBeVisible()
})

it.each(['walk', 'idle', 'custom'] as const)('saves one fixed framing transform and pivot for a transparent %s sheet', async (motion) => {
  const user = userEvent.setup()
  const onSave = vi.fn().mockResolvedValue(undefined)
  const original = JSON.stringify(candidate)
  render(<FrameEditor projectId="framing" motionId={motion} motionName={motion} motionPreset={motion === 'custom' ? null : motion} candidate={candidate} previewUrl="/sheet.png" fallbackLoop="loop" onSave={onSave} />)
  await user.click(screen.getByLabelText('Set framing and pivot'))
  // The source is four 64×64 cells, not one 256×64 image.
  expect(screen.getByLabelText('Source anchor X')).toHaveValue(32)
  expect(screen.getByLabelText('Source anchor Y')).toHaveValue(58.88)
  expect(screen.getByLabelText('Body scale')).toHaveValue(4)
  expect(screen.getByLabelText('Target pivot X')).toHaveValue(128)
  expect(screen.getByLabelText('Cell size')).toHaveValue('256')
  for (const [label, value] of [['Body scale', '2'], ['Source anchor X', '30'], ['Source anchor Y', '60'], ['Target pivot X', '120'], ['Target pivot Y', '220'], ['Overhang padding', '12']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  const light = screen.getByRole('img', { name: `${motion} fixed framing on light background` })
  const dark = screen.getByRole('img', { name: `${motion} fixed framing on dark background` })
  expect(light.querySelector('.framing-preview__pivot')).toHaveStyle({ left: '96px', top: '146px' })
  expect(dark.querySelector('.framing-preview__pivot')).toHaveStyle({ left: '96px', top: '146px' })
  await user.click(screen.getByRole('button', { name: 'Save revision' }))
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ cellSize: 256, normalization: { scale: 2, source_anchor_x: 30, source_anchor_y: 60, target_anchor_x: 120, target_anchor_y: 220, padding: 12, cleanup_green_edges: false } }))
  expect(JSON.stringify(candidate)).toBe(original)
})

it('restores explicit framing choices from the local draft without refitting the body', async () => {
  const user = userEvent.setup()
  const props = { projectId: 'framing', motionId: 'walk', motionName: 'Walk', candidate, previewUrl: '/sheet.png', fallbackLoop: 'loop' as const, onSave: vi.fn() }
  const view = render(<FrameEditor {...props} />)
  await user.click(screen.getByLabelText('Set framing and pivot'))
  fireEvent.change(screen.getByLabelText('Body scale'), { target: { value: '1.25' } })
  fireEvent.change(screen.getByLabelText('Target pivot Y'), { target: { value: '211' } })
  view.unmount()
  render(<FrameEditor {...props} />)
  expect(screen.getByLabelText('Body scale')).toHaveValue(1.25)
  expect(screen.getByLabelText('Target pivot Y')).toHaveValue(211)
  fireEvent.change(screen.getByLabelText('Overhang padding'), { target: { value: '-1' } })
  expect(screen.getByRole('alert')).toHaveTextContent('Overhang padding')
  expect(screen.getByRole('button', { name: 'Save revision' })).toBeDisabled()
})

it('keeps Death extraction manual without a hidden Walk suggestion', () => {
  render(<FrameEditor projectId="project-1" motionId="death" motionName="Death" motionPreset="death" candidate={{ ...candidate, metadata: { ...candidate.metadata!, media_kind: 'video' } }} previewUrl="/death.mp4" fallbackLoop="once" onSave={vi.fn()} />)
  expect(screen.queryByRole('button', { name: 'Suggest walk loop' })).not.toBeInTheDocument()
  expect(screen.getByText(/Choose the start and end of this action manually/)).toBeVisible()
  expect(screen.getByLabelText('Set framing and pivot')).toBeVisible()
})

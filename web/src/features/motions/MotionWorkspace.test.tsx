import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { projectFixture, queryWrapper } from '../../test/studioTestUtils'
import { GenerateZone } from './GenerateZone'
import { DEFAULT_MOTION_GENERATION_SETTINGS } from '../../api/types'
import { MotionWorkspace } from './MotionWorkspace'
import { operationRefetchInterval } from './operationPolling'

/** The main column's Generate zone -- scoped so a query for its button never collides with the
 * rail's own retry icon, which can carry the identical accessible name ("Retry Idle" etc). Async
 * because the zone only exists once the project query has resolved and a motion is selected. */
async function generateZone() {
  return within(await screen.findByRole('region', { name: 'Generate' }))
}
async function takesZone() {
  return within(await screen.findByRole('region', { name: 'Takes' }))
}

const motions = [
  { motion_id: 'idle', name: 'Idle', kind: 'preset' as const, semantic_slot: 'idle', loop_policy: 'loop' as const, required: true, candidates: [{ candidate_id: 'idle-a', artifact_key: 'idle-a.png', sha256: 'a', metadata: null, assessment: null, duration_ms: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right' as const, recipe: null, source: 'generated' as const }], selected_candidate_id: 'idle-a' },
  { motion_id: 'wave', name: 'Wave', kind: 'custom' as const, semantic_slot: null, loop_policy: 'loop' as const, required: true, candidates: [{ candidate_id: 'wave-a', artifact_key: 'wave-a.png', sha256: 'b', metadata: null, assessment: null, duration_ms: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right' as const, recipe: null, source: 'generated' as const }, { candidate_id: 'wave-b', artifact_key: 'wave-b.png', sha256: 'c', metadata: null, assessment: null, duration_ms: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right' as const, recipe: null, source: 'generated' as const }], selected_candidate_id: 'wave-a' },
]
const motionProject = { ...projectFixture, manifest: { ...projectFixture.manifest, motions } }
const server = setupServer(
  http.get('/studio/generation-config', () => HttpResponse.json({ settings: null, selected: null, workers: [] })),
  http.get('/studio/projects/project-1', () => HttpResponse.json(motionProject)),
  http.get('/studio/projects/project-1/operations', () => HttpResponse.json([{ job_id: 'job-run', status: 'processing', motion_id: 'wave', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false }])),
  // Reconcile does not settle a merely-running job -- it echoes back whatever the operations
  // listing currently reports. Proxying through the (possibly test-overridden) listing handler
  // keeps every test's reconcile response consistent with its own operations fixture.
  http.post('/studio/projects/project-1/activity/reconcile', async () => {
    const response = await fetch('/studio/projects/project-1/operations')
    return HttpResponse.json({ operations: await response.json(), next_cursor: null })
  }),
  // The motion log queries the durable ledger filtered by motion_id -- not under test here.
  http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: [], next_cursor: null })),
  http.post('/studio/projects/project-1/assets/preview', async ({ request }) => { const body = await request.json() as { asset_key: string }; return HttpResponse.json({ key: body.asset_key, upload_url: `https://assets.invalid/${body.asset_key}` }) }),
  http.post('/studio/projects/project-1/motions/wave/candidates/wave-b/select', async ({ request }) => {
    expect(await request.json()).toEqual({ expected_revision: 4 })
    return HttpResponse.json({ ...motionProject, revision: 5, manifest: { ...motionProject.manifest, motions: [motions[0], { ...motions[1], selected_candidate_id: 'wave-b' }] } })
  }),
)
beforeAll(() => server.listen()); afterEach(() => server.resetHandlers()); afterAll(() => server.close())

it('opens the integrated frame editor from a take card, replacing passive timing metadata', async () => {
  const user = userEvent.setup()
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json({ ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [{ ...motions[0].candidates[0], duration_ms: 400, metadata: { decodable: true, media_kind: 'sheet', frame_count: 4, width: 256, height: 64, constant_geometry: true, has_alpha: true, duplicate_terminal_frame: false, baseline_y: null, packing_estimate: { profile: 'horizontal-strip-v1', width: 256, height: 64 } }, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null }] }] } })))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await screen.findByRole('option', { name: /Idle/ })
  await user.click(await (await takesZone()).findByRole('button', { name: 'Edit frames' }))
  expect(await screen.findByRole('region', { name: 'Idle frame editor' }, { timeout: 5_000 })).toBeInTheDocument()
  expect(screen.getByRole('list', { name: 'Animation frames' })).toBeInTheDocument()
})

it('polls only while an operation still needs integration', () => {
  expect(operationRefetchInterval([])).toBe(false)
  expect(operationRefetchInterval([{ job_id: 'failed', status: 'failed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Try again.', retryable: true }])).toBe(false)
  expect(operationRefetchInterval([{ job_id: 'active', status: 'processing', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false }])).toBe(1500)
})

it('gives a newer active retry precedence over prior failure history, and forces the Generate zone open while it runs', async () => {
  const user = userEvent.setup(); server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([
    { job_id: 'retry', status: 'processing', motion_id: 'wave', output_key: 'retry.mp4', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false },
    { job_id: 'old-failure', status: 'failed', motion_id: 'wave', output_key: 'old.mp4', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Old failure.', retryable: true },
  ])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() }); await user.click(await screen.findByRole('option', { name: /Wave/ }))
  expect((await generateZone()).getByRole('button', { name: /Generating…/ })).toBeDisabled()
  expect((await generateZone()).queryByRole('button', { name: 'Retry Wave' })).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Generating')
})

it('collapses the Generate zone once a motion has takes, keeping the failure history out of the way', async () => {
  const user = userEvent.setup(); server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([{ job_id: 'old-failure', status: 'failed', motion_id: 'wave', output_key: 'old.mp4', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Old failure.', retryable: true }])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() }); await user.click(await screen.findByRole('option', { name: /Wave/ }))
  expect(screen.getByRole('option', { name: 'Wave Selected' })).toHaveTextContent('Selected')
  expect((await generateZone()).getByRole('button', { name: 'Expand' })).toBeInTheDocument()
  expect(screen.queryByText('Old failure.')).not.toBeInTheDocument()
  await user.click((await generateZone()).getByRole('button', { name: 'Expand' }))
  expect((await generateZone()).getByRole('button', { name: 'Regenerate Wave' })).toBeInTheDocument()
})

it('reviews idle while a custom motion is still generating', async () => {
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('option', { name: 'Idle Selected' })).toBeEnabled()
  expect(screen.getByText('Generating…')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Generating…')
  expect(screen.queryByText('Loading motions')).not.toBeInTheDocument()
})

it('selects one take without replacing another motion\'s selection', async () => {
  const user = userEvent.setup()
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('option', { name: /Wave/ }))
  const take = (await takesZone()).getByRole('listitem', { name: 'Take 2' })
  await user.click(within(take).getByRole('button', { name: 'Use animation' }))
  expect(await within(take).findByText('Selected')).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'Idle Selected' })).toHaveTextContent('Selected')
})

it('offers PNG/WebM/GIF downloads resolved to a fresh signed URL (T20)', async () => {
  const user = userEvent.setup()
  const saved: string[] = []
  const originalClick = HTMLAnchorElement.prototype.click
  HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) { saved.push(this.href) }
  server.use(
    http.get('/studio/projects/project-1/motions/wave/candidates/wave-b/sheet.png', () => HttpResponse.json({ key: 'sheet-key', upload_url: 'https://assets.invalid/wave-b/sheet.png' })),
    http.get('/studio/projects/project-1/motions/wave/candidates/wave-b/preview.webm', () => HttpResponse.json({ key: 'webm-key', upload_url: 'https://assets.invalid/wave-b/preview.webm' })),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('option', { name: /Wave/ }))
  const take = (await takesZone()).getByRole('listitem', { name: 'Take 2' })
  await user.click(within(take).getByRole('button', { name: 'Download' }))
  await user.click(within(take).getByRole('button', { name: 'PNG' }))
  await waitFor(() => expect(saved).toContain('https://assets.invalid/wave-b/sheet.png'))
  await user.click(within(take).getByRole('button', { name: 'Download' }))
  await user.click(within(take).getByRole('button', { name: 'WebM' }))
  await waitFor(() => expect(saved).toContain('https://assets.invalid/wave-b/preview.webm'))
  HTMLAnchorElement.prototype.click = originalClick
})

async function revealUpload(user: ReturnType<typeof userEvent.setup>) {
  // No job in flight for this project, so Wave's Generate zone starts collapsed (it already has
  // takes) and needs an explicit Expand.
  server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  await user.click(await screen.findByRole('option', { name: /Wave/ }))
  await user.click((await generateZone()).getByRole('button', { name: 'Expand' }))
  await user.click((await generateZone()).getByRole('button', { name: 'or upload a clip you already have' }))
}

it('ingests an uploaded PNG strip as a candidate with a frame-count field (T21)', async () => {
  const user = userEvent.setup()
  let settleBody: unknown = null
  server.use(
    http.post('/studio/projects/project-1/motions/wave/candidates/uploads', async ({ request }) => {
      expect(await request.json()).toEqual({ content_type: 'image/png', expected_revision: 4 })
      return HttpResponse.json({ key: 'sources/studio/project-1/motion-uploads/x.png', upload_url: '/local-assets/upload' })
    }),
    http.put('/local-assets/upload', () => HttpResponse.text('', { status: 204 })),
    http.post('/studio/projects/project-1/motions/wave/candidates/settle-upload', async ({ request }) => {
      settleBody = await request.json()
      return HttpResponse.json({ ...motionProject, revision: 5 })
    }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await revealUpload(user)
  const input = screen.getByLabelText('Upload existing animation')
  const file = new File(['png-bytes'], 'strip.png', { type: 'image/png' })
  await user.upload(input, file)
  expect(await screen.findByLabelText('Uploaded frame count')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Uploaded frame count'), '8')
  await user.click(screen.getByRole('button', { name: 'Add as candidate' }))
  await waitFor(() => expect(settleBody).toEqual({ expected_revision: 4, asset_key: 'sources/studio/project-1/motion-uploads/x.png', frame_count: 8 }))
})

it('detects a PNG upload by extension when the browser omits File.type (T9)', async () => {
  const user = userEvent.setup()
  let settleBody: unknown = null
  server.use(
    http.post('/studio/projects/project-1/motions/wave/candidates/uploads', async ({ request }) => {
      expect(await request.json()).toEqual({ content_type: 'image/png', expected_revision: 4 })
      return HttpResponse.json({ key: 'sources/studio/project-1/motion-uploads/x.png', upload_url: '/local-assets/upload' })
    }),
    http.put('/local-assets/upload', () => HttpResponse.text('', { status: 204 })),
    http.post('/studio/projects/project-1/motions/wave/candidates/settle-upload', async ({ request }) => {
      settleBody = await request.json()
      return HttpResponse.json({ ...motionProject, revision: 5 })
    }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await revealUpload(user)
  const input = screen.getByLabelText('Upload existing animation')
  const file = new File(['png-bytes'], 'strip.png', { type: '' })
  fireEvent.change(input, { target: { files: [file] } })
  expect(await screen.findByLabelText('Uploaded frame count')).toBeInTheDocument()
  await user.type(screen.getByLabelText('Uploaded frame count'), '8')
  await user.click(screen.getByRole('button', { name: 'Add as candidate' }))
  await waitFor(() => expect(settleBody).toEqual({ expected_revision: 4, asset_key: 'sources/studio/project-1/motion-uploads/x.png', frame_count: 8 }))
})

it('submits generation and leaves completed settlement to project activity', async () => {
  const user = userEvent.setup(); let generated = false
  const emptyWave = { ...motionProject, manifest: { ...motionProject.manifest, motions: [motions[0], { ...motions[1], candidates: [], selected_candidate_id: null }] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(emptyWave)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json(generated ? [{ job_id: 'job-new', status: 'completed', motion_id: 'wave', output_key: 'wave-b.png', progress_percent: null, stage: 'awaiting_settlement', failure_code: null, failure_explanation: null, retryable: false }] : [])),
    http.post('/studio/projects/project-1/motions/wave/generate', async ({ request }) => { expect((await request.json() as { expected_revision: number }).expected_revision).toBe(4); generated = true; return HttpResponse.json({ job_id: 'job-new', status: 'queued', motion_id: 'wave', output_key: 'wave-b.png', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false }) }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('option', { name: 'Wave To generate' }))
  await user.click((await generateZone()).getByRole('button', { name: 'Generate Wave' }))
  expect(await (await generateZone()).findByRole('button', { name: /Generating…/ })).toBeDisabled()
  expect((await takesZone()).getByText('Generating take…')).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'Idle Selected' })).toBeEnabled()
})

it('lists a not-yet-selected take in the Takes gallery instead of a false empty state', async () => {
  const unselectedIdle = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [{ candidate_id: 'idle-a', artifact_key: 'idle-a.mp4', sha256: 'a', metadata: null, assessment: null, duration_ms: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right' as const, recipe: null, source: 'generated' as const }], selected_candidate_id: null }] } }
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json(unselectedIdle)), http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect((await takesZone()).queryByText(/No takes yet/)).not.toBeInTheDocument()
  const take = await (await takesZone()).findByRole('listitem', { name: 'Take 1' })
  expect(within(take).getByRole('button', { name: 'Use animation' })).toBeEnabled()
  expect(await within(take).findByLabelText('Idle take 1 playback')).toBeInTheDocument()
})

it('renders a sheet candidate take as an image even when its key ends in .mp4', async () => {
  const sheetNamedMp4 = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [{ candidate_id: 'idle-a', artifact_key: 'idle-a.mp4', sha256: 'a', metadata: { decodable: true, media_kind: 'sheet' as const, frame_count: 4, width: 256, height: 64, constant_geometry: true, has_alpha: true, duplicate_terminal_frame: false, baseline_y: null, packing_estimate: { profile: 'horizontal-strip-v1', width: 256, height: 64 } }, assessment: null, duration_ms: 400, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right' as const, recipe: null, source: 'generated' as const }], selected_candidate_id: null }] } }
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json(sheetNamedMp4)), http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const take = await (await takesZone()).findByRole('listitem', { name: 'Take 1' })
  expect(await within(take).findByRole('img', { name: /frame 1 of 4/ })).toBeInTheDocument()
  expect(take.querySelector('video')).not.toBeInTheDocument()
})

it('shows an actionable empty Takes state alongside an already-open Generate button', async () => {
  const emptyIdle = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }] } }
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json(emptyIdle)), http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await (await takesZone()).findByText(/No takes yet/)).toBeInTheDocument()
  expect((await generateZone()).getByRole('button', { name: 'Generate Idle' })).toBeInTheDocument()
})

it('refreshes and explains a motion revision conflict', async () => {
  const user = userEvent.setup(); server.use(http.post('/studio/projects/project-1/motions/idle/generate', () => HttpResponse.json({}, { status: 409 })))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await (await generateZone()).findByRole('button', { name: 'Expand' }))
  await user.click((await generateZone()).getByRole('button', { name: 'Regenerate Idle' }))
  expect(await screen.findByText('This project changed elsewhere. Your view has been refreshed.')).toBeInTheDocument()
})

it('offers Retry only for a safely classified transient failure', async () => {
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json({ ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }] } })), http.get('/studio/projects/project-1/operations', () => HttpResponse.json([{ job_id: 'failed', status: 'failed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Generation was interrupted by a temporary service problem. Retry this motion.', retryable: true }])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await (await generateZone()).findByRole('button', { name: 'Retry Idle' })).toBeInTheDocument()
  expect(within(screen.getByRole('listbox', { name: 'Motions' })).getByRole('button', { name: 'Retry Idle' })).toBeInTheDocument() // rail retry icon, same accessible name by design
  // Shown twice by design (BOU-3208): once in the motion rail's per-row summary, and again next
  // to the Generate/Retry button in the detail pane the operator is actually looking at.
  expect(screen.getAllByText(/temporary service problem/).length).toBeGreaterThan(0)
})

it('directs a nonretryable input failure to inspection', async () => {
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json({ ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }] } })), http.get('/studio/projects/project-1/operations', () => HttpResponse.json([{ job_id: 'failed', status: 'failed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'invalid_input', failure_explanation: 'The motion settings are invalid. Inspect and edit the motion before retrying.', retryable: false }])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await (await generateZone()).findByText('The motion settings are invalid. Inspect and edit the motion before retrying.')).toBeInTheDocument()
  expect((await generateZone()).queryByRole('button', { name: 'Retry Idle' })).not.toBeInTheDocument()
})

it('adds the recommended set from the empty state, all pre-checked for the next batch (T5/T6)', async () => {
  const user = userEvent.setup()
  const empty = { ...motionProject, manifest: { ...motionProject.manifest, motions: [] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(empty)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.post('/studio/projects/project-1/motions/batch', async ({ request }) => {
      const body = await request.json() as { motions: Array<{ motion_id: string; preset: string }> }
      expect(body.motions.map((motion) => motion.motion_id)).toEqual(['idle', 'walk', 'attack', 'guard', 'cast', 'hurt'])
      expect(body.motions.every((motion) => motion.preset === motion.motion_id)).toBe(true)
      return HttpResponse.json({
        ...empty, revision: 5,
        manifest: { ...empty.manifest, motions: body.motions.map((motion) => ({ motion_id: motion.motion_id, name: motion.motion_id[0].toUpperCase() + motion.motion_id.slice(1), kind: 'preset', semantic_slot: motion.motion_id, loop_policy: 'loop', required: true, candidates: [], selected_candidate_id: null })) },
      })
    }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('button', { name: 'Add the recommended set' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Add the recommended set' }))
  expect(await screen.findByRole('button', { name: 'Generate 6 motions' })).toBeInTheDocument()
  expect(screen.getByRole('checkbox', { name: 'Include Idle in the next batch' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'Include Hurt in the next batch' })).toBeChecked()
})

it('dispatches the checked motions as one batch from "Generate N motions"', async () => {
  const user = userEvent.setup()
  let batchBody: unknown = null
  const twoUngenerated = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }, { ...motions[1], candidates: [], selected_candidate_id: null }] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(twoUngenerated)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.post('/studio/projects/project-1/motions/generate-batch', async ({ request }) => {
      batchBody = await request.json()
      return HttpResponse.json({ queued: [], failed: [] })
    }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('0 of 2 selected')).toBeInTheDocument()
  // Both motions are pre-checked by default (no selected take, nothing in flight): unchecking
  // Idle leaves only Wave for the batch.
  expect(await screen.findByRole('button', { name: 'Generate 2 motions' })).toBeInTheDocument()
  await user.click(screen.getByRole('checkbox', { name: 'Include Idle in the next batch' }))
  await user.click(screen.getByRole('button', { name: 'Generate 1 motion' }))
  await user.click(within(await screen.findByRole('dialog', { name: 'What happens in each motion' })).getByRole('button', { name: 'Generate' }))
  await waitFor(() => expect(batchBody).toMatchObject({ expected_revision: 4, motion_ids: ['wave'] }))
})

it('asks what happens in every ticked motion before the batch renders, and sends the edited lines', async () => {
  const user = userEvent.setup()
  let batchBody: unknown = null
  const presetAndCustom = { ...motionProject, manifest: { ...motionProject.manifest, motions: [
    { ...motions[0], preset: 'idle' as const, candidates: [], selected_candidate_id: null },
    { ...motions[1], prompt: 'A friendly wave.', candidates: [], selected_candidate_id: null },
  ] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(presetAndCustom)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.post('/studio/projects/project-1/motions/generate-batch', async ({ request }) => {
      batchBody = await request.json()
      return HttpResponse.json({ queued: [], failed: [] })
    }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Generate 2 motions' }))

  const sheet = within(await screen.findByRole('dialog', { name: 'What happens in each motion' }))
  // Prefilled: the preset's default line, and the custom motion's own direction.
  expect(sheet.getByRole('textbox', { name: 'What happens in Idle' })).toHaveValue('breathes and shifts weight')
  expect(sheet.getByRole('textbox', { name: 'What happens in Wave' })).toHaveValue('A friendly wave.')
  await user.clear(sheet.getByRole('textbox', { name: 'What happens in Wave' }))
  await user.type(sheet.getByRole('textbox', { name: 'What happens in Wave' }), 'draws and fires the bow')
  await user.click(sheet.getByRole('button', { name: 'Generate' }))

  await waitFor(() => expect(batchBody).toMatchObject({
    motion_ids: ['idle', 'wave'],
    actions: { idle: 'breathes and shifts weight', wave: 'draws and fires the bow' },
  }))
})

it('carries the Generate zone\'s "What happens" line into the dispatch', async () => {
  const user = userEvent.setup()
  let generateBody: unknown = null
  let patchBody: unknown = null
  const ungenerated = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], preset: 'idle' as const, candidates: [], selected_candidate_id: null }] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(ungenerated)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    // Leaving the field persists the line on its own, so an edit is never lost by not generating.
    http.patch('/studio/projects/project-1/motions/idle', async ({ request }) => {
      patchBody = await request.json()
      return HttpResponse.json({ ...ungenerated, revision: 5 })
    }),
    http.post('/studio/projects/project-1/motions/idle/generate', async ({ request }) => {
      generateBody = await request.json()
      return HttpResponse.json({ job_id: 'job-idle', status: 'queued', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false })
    }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const zone = await generateZone()
  const line = zone.getByRole('textbox', { name: 'What happens in Idle' })
  expect(line).toHaveValue('breathes and shifts weight')
  await user.clear(line)
  await user.type(line, 'shifts weight onto the back foot')
  await user.click(zone.getByRole('button', { name: 'Generate Idle' }))
  await waitFor(() => expect(generateBody).toMatchObject({ action: 'shifts weight onto the back foot' }))
  expect(patchBody).toMatchObject({ motion_id: 'idle', action: 'shifts weight onto the back foot' })
})

it('reads Regenerate when every ticked motion already has a take', async () => {
  const user = userEvent.setup()
  server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  // Both fixture motions have a take, so neither is ticked by default; ticking one asks for a rerun.
  await user.click(await screen.findByRole('checkbox', { name: 'Include Idle in the next batch' }))
  expect(await screen.findByRole('button', { name: 'Regenerate 1 motion' })).toBeInTheDocument()
  await user.click(screen.getByRole('checkbox', { name: 'Include Wave in the next batch' }))
  expect(await screen.findByRole('button', { name: 'Regenerate 2 motions' })).toBeInTheDocument()
})

it('unticks a motion once its batch take lands, leaving the failed one ticked', async () => {
  const user = userEvent.setup()
  let dispatched = false
  const ungenerated = { ...motionProject, manifest: { ...motionProject.manifest, motions: [
    { ...motions[0], candidates: [], selected_candidate_id: null },
    { ...motions[1], candidates: [], selected_candidate_id: null },
  ] } }
  const settled = { ...motionProject, revision: 5, manifest: { ...motionProject.manifest, motions: [
    { ...motions[0], selected_candidate_id: null },
    { ...motions[1], candidates: [], selected_candidate_id: null },
  ] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(dispatched ? settled : ungenerated)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json(dispatched
      ? [{ job_id: 'job-wave', status: 'failed', motion_id: 'wave', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Try again.', retryable: true }]
      : [])),
    http.post('/studio/projects/project-1/motions/generate-batch', () => { dispatched = true; return HttpResponse.json({ queued: [], failed: [] }) }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Generate 2 motions' }))
  await user.click(within(await screen.findByRole('dialog', { name: 'What happens in each motion' })).getByRole('button', { name: 'Generate' }))

  // Idle produced a take and drops its tick; Wave failed and keeps it.
  await waitFor(() => expect(screen.getByRole('checkbox', { name: 'Include Idle in the next batch' })).not.toBeChecked())
  expect(screen.getByRole('checkbox', { name: 'Include Wave in the next batch' })).toBeChecked()
  expect(await screen.findByRole('button', { name: 'Generate 1 motion' })).toBeInTheDocument()
})

it('keeps the checklist ticked after dispatch and turns the batch button into a spinner', async () => {
  const user = userEvent.setup()
  let dispatched = false
  const twoUngenerated = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }, { ...motions[1], candidates: [], selected_candidate_id: null }] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(twoUngenerated)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json(dispatched
      ? [
        { job_id: 'job-idle', status: 'queued', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false },
        { job_id: 'job-wave', status: 'processing', motion_id: 'wave', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false },
      ]
      : [])),
    http.post('/studio/projects/project-1/motions/generate-batch', () => { dispatched = true; return HttpResponse.json({ queued: [], failed: [] }) }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Generate 2 motions' }))
  await user.click(within(await screen.findByRole('dialog', { name: 'What happens in each motion' })).getByRole('button', { name: 'Generate' }))

  // The ticked set survives dispatch -- the operator can re-run the same batch.
  expect(await screen.findByRole('button', { name: 'Generating 2…' })).toBeDisabled()
  expect(screen.getByRole('checkbox', { name: 'Include Idle in the next batch' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'Include Wave in the next batch' })).toBeChecked()
  expect(screen.getByText('Generating 2 motions')).toBeInTheDocument()
  expect(await screen.findByRole('option', { name: 'Idle Queued' })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'Wave Generating…' })).toBeInTheDocument()
})

it('shows a pending take card in the Takes zone while the open motion is generating', async () => {
  const generating = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(generating)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([{ job_id: 'job-idle', status: 'processing', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false }])),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const takes = await takesZone()
  expect(await takes.findByText('Generating take…')).toBeInTheDocument()
  expect(takes.queryByText(/No takes yet/)).not.toBeInTheDocument()
})

it('opens the preset grid from Add motion and reveals the custom form on demand', async () => {
  const user = userEvent.setup()
  server.use(http.post('/studio/projects/project-1/motions', async ({ request }) => {
    expect(await request.json()).toMatchObject({ motion_id: 'attack', name: 'Attack', kind: 'preset', preset: 'attack' })
    return HttpResponse.json({ ...motionProject, revision: 5 })
  }))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Add motion' }))
  expect(screen.getByRole('button', { name: 'Add Attack' })).toBeInTheDocument()
  expect(screen.queryByLabelText('Motion name')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Add Attack' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Add Attack' })).not.toBeInTheDocument())
})

it('forces the Generate zone open and offers Interrupt for a running motion (T13)', async () => {
  const user = userEvent.setup()
  let cancelled = false
  server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([
    { job_id: 'job-run', status: 'processing', motion_id: 'wave', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false },
  ])), http.post('/studio/projects/project-1/motions/wave/operations/job-run/cancel', () => {
    cancelled = true
    return HttpResponse.json({ job_id: 'job-run', status: 'cancelled', motion_id: 'wave', output_key: 'out', progress_percent: null, stage: 'cancelled', failure_code: null, failure_explanation: null, retryable: true })
  }))
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('option', { name: /Wave/ }))
  expect((await generateZone()).getByRole('button', { name: /Generating…/ })).toBeDisabled()
  await user.click((await generateZone()).getByRole('button', { name: 'Interrupt' }))
  expect(cancelled).toBe(true)
})

it('offers Cancel for a queued (not yet running) motion (T13)', async () => {
  server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([
    { job_id: 'job-queued', status: 'queued', motion_id: 'wave', output_key: 'out', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false },
  ])))
  const user = userEvent.setup()
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('option', { name: /Wave/ }))
  expect(await (await generateZone()).findByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  expect((await generateZone()).queryByRole('button', { name: 'Interrupt' })).not.toBeInTheDocument()
})

it('shows a cancelled motion as retryable in the motion list (T13)', async () => {
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json({ ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }, motions[1]] } })),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([
      { job_id: 'job-cancelled', status: 'cancelled', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'cancelled', failure_code: null, failure_explanation: null, retryable: true },
    ])),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('Cancelled')).toBeInTheDocument()
})

it('shows Regenerate with no spinner once the generate mutation resolves and nothing is in flight', async () => {
  const user = userEvent.setup()
  const idleWithTake = { ...motionProject, manifest: { ...motionProject.manifest, motions: [motions[0], motions[1]] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(idleWithTake)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    // The job completes and settles instantly server-side -- by the time the frontend's
    // post-mutation poll lands, there is nothing in flight to report.
    http.post('/studio/projects/project-1/motions/idle/generate', () => HttpResponse.json({ job_id: 'job-new', status: 'completed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'settled', failure_code: null, failure_explanation: null, retryable: false })),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await screen.findByRole('option', { name: 'Idle Selected' })
  await user.click((await generateZone()).getByRole('button', { name: 'Expand' }))
  await user.click((await generateZone()).getByRole('button', { name: 'Regenerate Idle' }))
  expect(await (await generateZone()).findByRole('button', { name: 'Regenerate Idle' })).toBeInTheDocument()
  expect((await generateZone()).queryByText(/Generating/)).not.toBeInTheDocument()
})

it('never retries a failed operations poll, so a transient failure cannot keep serving a stale in-flight snapshot (BOU-3208 follow-up)', async () => {
  let requestCount = 0
  server.use(http.get('/studio/projects/project-1/operations', () => { requestCount += 1; return HttpResponse.json({ detail: 'boom' }, { status: 500 }) }))
  // Deliberately no `retry: false` on the client itself -- the guarantee must come from the
  // operations query's own config, not from a test-only client default masking a production gap.
  const client = new QueryClient()
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/projects/project-1/motions']}>
        <MotionWorkspace projectId="project-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  await screen.findByRole('option', { name: /Idle/ })
  await waitFor(() => expect(requestCount).toBeGreaterThan(0))
  // React Query's default retry backoff waits 1000ms before a second attempt -- outlast that
  // window to prove no retry happens, rather than just that none has happened *yet*.
  await new Promise((resolve) => setTimeout(resolve, 1300))
  expect(requestCount).toBe(1)
}, 10_000)

it('seeds the selected motion and its generation settings from router state (T14 regenerate)', async () => {
  server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const seededSettings = { preset: 'high' as const, steps: 28, seed: 123, width: 768, height: 768, duration_seconds: 6, provider_parameters: {} }
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[{ pathname: '/projects/project-1/motions', state: { seedSettings: { motionId: 'wave', settings: seededSettings } } }]}>
        <MotionWorkspace projectId="project-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(await screen.findByRole('option', { name: 'Wave Selected' })).toHaveTextContent('Selected')
  await userEvent.setup().click((await generateZone()).getByRole('button', { name: 'Expand' }))
  expect(screen.getByLabelText('Duration (seconds)')).toHaveValue(6)
})

it('selects the deep-linked motion and shows the target binding notice (Part A)', async () => {
  server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[{ pathname: '/projects/project-1/motions', state: { handoffNotice: 'Bound to Acme character Ember Knight', selectMotionId: 'wave' } }]}>
        <MotionWorkspace projectId="project-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  expect(await screen.findByRole('option', { name: 'Wave Selected' })).toHaveTextContent('Selected')
  expect(await screen.findByText('Bound to Acme character Ember Knight')).toBeInTheDocument()
})

it('reveals the custom motion form from the Custom card', async () => {
  const user = userEvent.setup()
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Add motion' }))
  await user.click(screen.getByRole('button', { name: 'Custom…' }))
  expect(screen.getByLabelText('Motion name')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add Idle' })).not.toBeInTheDocument()
})

it('closes the overlay preset grid with Escape and returns focus to Add motion', async () => {
  const user = userEvent.setup()
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const addButton = await screen.findByRole('button', { name: 'Add motion' })
  await user.click(addButton)
  const overlay = screen.getByRole('dialog', { name: 'Add a motion' })
  expect(overlay).toBeInTheDocument()

  await user.keyboard('{Escape}')

  expect(screen.queryByRole('dialog', { name: 'Add a motion' })).not.toBeInTheDocument()
  expect(addButton).toHaveFocus()
})

it('shows the server\'s reason and an approve-subject link when generation is blocked by an unapproved subject', async () => {
  const user = userEvent.setup()
  const twoUngenerated = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }, { ...motions[1], candidates: [], selected_candidate_id: null }] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(twoUngenerated)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.post('/studio/projects/project-1/motions/generate-batch', () =>
      HttpResponse.json({ detail: 'motion generation requires the approved current subject' }, { status: 422 })),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Generate 2 motions' }))
  await user.click(within(await screen.findByRole('dialog', { name: 'What happens in each motion' })).getByRole('button', { name: 'Generate' }))
  expect(await screen.findByText('Motion generation requires the approved current subject')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Approve the subject' })).toHaveAttribute('href', '/projects/project-1/subject')
})

it('shows why a finished render has not become a take and retries settlement on demand', async () => {
  const user = userEvent.setup()
  const ungenerated = { ...motionProject, manifest: { ...motionProject.manifest, motions: [{ ...motions[0], candidates: [], selected_candidate_id: null }] } }
  const blocked = [{ job_id: 'job-blocked', status: 'completed' as const, motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'awaiting_settlement' as const, failure_code: null, failure_explanation: null, retryable: false, settlement_blocked: 'completed motion artifact is missing' }]
  let reconciles = 0
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(ungenerated)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json(blocked)),
    http.post('/studio/projects/project-1/activity/reconcile', () => { reconciles += 1; return HttpResponse.json({ operations: blocked, next_cursor: null }) }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const takes = await takesZone()
  expect(await takes.findByText('This render finished but could not become a take: completed motion artifact is missing')).toBeInTheDocument()

  await user.click(takes.getByRole('button', { name: 'Retry' }))

  await waitFor(() => expect(reconciles).toBeGreaterThan(0))
  expect(await takes.findByRole('button', { name: 'Regenerate' })).toBeInTheDocument()
})

it('preserves a cleared action across refreshed motion objects before typing and dispatch', async () => {
  const user = userEvent.setup()
  const motion = { ...motions[0], preset: 'idle' as const, candidates: [] }
  const onGenerate = vi.fn()
  const props = { motion, generationSettings: DEFAULT_MOTION_GENERATION_SETTINGS, onGenerationSettingsChange: vi.fn(), onUploadReference: vi.fn(), onPatchMotion: vi.fn(), onGenerate, onCancel: vi.fn(), generating: false, elapsedSeconds: null, generatePending: false, cancelPending: false, projectId: 'project-1', expectedRevision: 4 }
  const { rerender } = render(<GenerateZone {...props} />)
  const line = screen.getByRole('textbox', { name: 'What happens in Idle' })
  await user.clear(line)
  rerender(<GenerateZone {...props} motion={{ ...motion }} />)
  expect(line).toHaveValue('')
  await user.type(line, 'shifts weight onto the back foot')
  rerender(<GenerateZone {...props} motion={{ ...motion, action: 'A server edit' }} />)
  expect(line).toHaveValue('shifts weight onto the back foot')
  await user.click(screen.getByRole('button', { name: 'Generate Idle' }))
  expect(onGenerate).toHaveBeenCalledWith('shifts weight onto the back foot')
  rerender(<GenerateZone {...props} motion={{ ...motion, motion_id: 'wave', name: 'Wave', action: 'waves once' }} />)
  expect(screen.getByRole('textbox', { name: 'What happens in Wave' })).toHaveValue('waves once')
})

it('submits the discovered eight-step machine default and recipe binding', async () => {
  let submitted: unknown
  server.use(
    http.get('/studio/generation-config', () => HttpResponse.json({ scope: 'hosted', settings: {
      preset: 'standard', width: 512, height: 512, duration_seconds: 4, seed: 42, backdrop: 'chroma_green', steps: 8,
      provider_parameters: { execution_profile: 'h3-w4a8-16gb-lightx8', worker_id: 'desktop', recipe_version: 'recipe', configuration_version: 'config' },
    } })),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.post('/studio/projects/project-1/motions/wave/generate', async ({ request }) => { submitted = await request.json(); return HttpResponse.json({ job_id: 'job', status: 'queued' }) }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await userEvent.click(await screen.findByRole('option', { name: /Wave/ }))
  await userEvent.click(screen.getByRole('button', { name: 'Expand' }))
  await userEvent.click(screen.getByRole('button', { name: 'Advanced generation settings' }))
  expect(screen.getByRole('spinbutton', { name: 'Steps' })).toHaveValue(8)
  expect(screen.getByRole('spinbutton', { name: 'Steps' })).toBeDisabled()
  await userEvent.click(screen.getByRole('button', { name: /Regenerate|Generate Wave/ }))
  await waitFor(() => expect(submitted).toMatchObject({ settings: { steps: 8, provider_parameters: { worker_id: 'desktop', recipe_version: 'recipe', configuration_version: 'config' } } }))
})

it('scopes motion navigation and additions to a supplied view, retaining the original motions', async () => {
  const user = userEvent.setup()
  const original = { ...motions[0] }
  const north = { ...motions[0], motion_id: 'n-walk', name: 'Walk', preset: 'walk', view_id: 'N', view_reference: null }
  const east = { ...motions[1], motion_id: 'e-attack', name: 'Attack', preset: 'attack', view_id: 'E' }
  const current = { ...motionProject, manifest: { ...motionProject.manifest, sprite_layout: 'isometric', views: [{ view_id: 'N', name: 'North', reference: null }, { view_id: 'E', name: 'East', reference: null }], motions: [original, north, east] } }
  let body: Record<string, unknown> | null = null
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(current)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.post('/studio/projects/project-1/motions', async ({ request }) => { body = await request.json() as Record<string, unknown>; return HttpResponse.json({ ...current, revision: 5, manifest: { ...current.manifest, motions: [...current.manifest.motions, { ...body, candidates: [], selected_candidate_id: null }] } }) }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('option', { name: /Idle/ })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'North · Reference needed' }))
  expect(screen.queryByRole('option', { name: /Idle/ })).not.toBeInTheDocument()
  expect(screen.getByRole('option', { name: /Walk/ })).toBeInTheDocument()
  expect(screen.queryByRole('option', { name: /Attack/ })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Expand' }))
  expect((await generateZone()).getByRole('button', { name: 'Regenerate Walk' })).toBeDisabled()
  expect(screen.getByText(/Upload a matching view reference in the view panel/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Add motion' }))
  await user.click(screen.getByRole('button', { name: 'Add Attack' }))
  await waitFor(() => expect(body).toMatchObject({ motion_id: 'n-attack', view_id: 'N', preset: 'attack' }))
  await user.click(screen.getByRole('button', { name: 'Original / unassigned' }))
  expect(screen.getByRole('option', { name: /Idle/ })).toBeInTheDocument()
  expect(original.selected_candidate_id).toBe('idle-a')
})

it('keeps selected takes visible but stale when a view reference is replaced', async () => {
  const user = userEvent.setup()
  const ref = { revision_id: 'view-r1', key: 'north.png', sha256: 'north', width: 128, height: 128 }
  const candidate = { ...motions[0].candidates[0], motion_spec_digest: 'old-spec' }
  const north = { ...motions[0], motion_id: 'n-walk', name: 'Walk', preset: 'walk', view_id: 'N', view_reference: ref, spec_digest: 'old-spec', candidates: [candidate] }
  let current = { ...motionProject, manifest: { ...motionProject.manifest, sprite_layout: 'isometric', views: [{ view_id: 'N', name: 'North', reference: ref }], motions: [north] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(current)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.get('/studio/projects/project-1/setup', () => HttpResponse.json({ revision: 4, setup: { schema_version: 1, sprite_layout: 'isometric', identity_reference: null, views: [{ view_id: 'N', heading: 'N', name: 'North', reference: { revision_id: 'view-r1' }, motions: [{ motion_id: 'n-walk', name: 'Walk', preset: 'walk' }] }] } })),
    http.post('/studio/projects/project-1/setup/preview', async ({ request }) => { const body = await request.json() as { desired_setup: unknown }; return HttpResponse.json({ plan_token: 'clear', expected_revision: 4, desired_setup: body.desired_setup, impacts: [], blocked_operations: [] }) }),
    http.post('/studio/projects/project-1/setup/apply', async () => {
      current = { ...current, revision: 5, manifest: { ...current.manifest, views: [{ view_id: 'N', name: 'North', reference: null as never }], motions: [{ ...north, spec_digest: 'new-spec', view_reference: null as never }] } }
      return HttpResponse.json(current)
    }),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'North · 1 animations' }))
  expect(screen.getByRole('option', { name: 'Walk Selected' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Edit artwork & directions' }))
  await user.click(await screen.findByRole('button', { name: 'Clear reference' }))
  await user.click(screen.getByRole('button', { name: 'Review changes' }))
  await user.click(await screen.findByRole('button', { name: 'Confirm exact changes' }))
  expect(await screen.findByRole('option', { name: 'Walk Stale' })).toBeInTheDocument()
  expect((await takesZone()).getByText('Selected')).toBeInTheDocument()
  expect((await takesZone()).getByText('Stale')).toBeInTheDocument()
  expect((await takesZone()).getByRole('button', { name: 'Regenerate for the current view' })).toBeInTheDocument()
  expect(current.manifest.motions[0].candidates).toEqual([candidate])
  expect(current.manifest.motions[0].selected_candidate_id).toBe('idle-a')
  expect(screen.getByRole('button', { name: 'North · Reference needed' })).toBeInTheDocument()
})

it.each(['reload', 'deep link', 'history settings'] as const)('opens the motion’s named view after loading for %s and preserves later Original selection', async (entry) => {
  const user = userEvent.setup()
  const north = { ...motions[0], motion_id: 'n-walk', name: 'North walk', view_id: 'N', preset: 'walk' }
  const east = { ...motions[1], motion_id: 'e-attack', name: 'East attack', view_id: 'E', preset: 'attack' }
  const current = { ...motionProject, manifest: { ...motionProject.manifest, sprite_layout: 'isometric', views: [{ view_id: 'N', name: 'North', reference: null }, { view_id: 'E', name: 'East', reference: null }], motions: [north, east, motions[0]] } }
  server.use(
    http.get('/studio/projects/project-1', async () => { await new Promise((resolve) => setTimeout(resolve, 10)); return HttpResponse.json(current) }),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const settings = { ...DEFAULT_MOTION_GENERATION_SETTINGS, duration_seconds: 7 }
  const state = entry === 'history settings' ? { seedSettings: { motionId: 'e-attack', settings } } : entry === 'deep link' ? { selectMotionId: 'e-attack' } : undefined
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[{ pathname: '/projects/project-1/motions', state }]}><MotionWorkspace projectId="project-1" /></MemoryRouter></QueryClientProvider>)
  const expected = entry === 'reload' ? 'North walk' : 'East attack'
  expect(await screen.findByRole('option', { name: `${expected} Selected` })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('button', { name: `${entry === 'reload' ? 'North' : 'East'} · Reference needed` })).toHaveAttribute('aria-pressed', 'true')
  expect(screen.queryByRole('option', { name: 'Idle Selected' })).not.toBeInTheDocument()
  if (entry === 'history settings') {
    await user.click((await generateZone()).getByRole('button', { name: 'Expand' }))
    expect(screen.getByLabelText('Duration (seconds)')).toHaveValue(7)
  }
  await user.click(screen.getByRole('button', { name: 'Original / unassigned' }))
  expect(screen.getByRole('option', { name: 'Idle Selected' })).toHaveAttribute('aria-selected', 'true')
  client.setQueryData(['project', 'project-1'], { ...current, revision: 5 })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Original / unassigned' })).toHaveAttribute('aria-pressed', 'true'))
  expect(screen.queryByRole('option', { name: `${expected} Selected` })).not.toBeInTheDocument()
})

it('opens the first declared view on reload when it has no motions yet', async () => {
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json({ ...motionProject, manifest: { ...motionProject.manifest, motions: [], sprite_layout: 'side_view', views: [{ view_id: 'left', name: 'Left', reference: null }] } })),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
  )
  render(<MotionWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('button', { name: 'Edit artwork & directions' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Left · No animations' })).toHaveAttribute('aria-pressed', 'true')
})

it('selects the newly added second animation and keeps it selected after project refresh', async () => {
  const user = userEvent.setup()
  const ref = { revision_id: 'r1', key: 'north.png', sha256: 'north', width: 128, height: 128 }
  const idle = { ...motions[0], kind: 'preset' as const, motion_id: 'n-idle', view_id: 'N', preset: 'idle', candidates: [], selected_candidate_id: null }
  let current = { ...motionProject, manifest: { ...motionProject.manifest, sprite_layout: 'isometric', views: [{ view_id: 'N', name: 'North', reference: ref }], motions: [idle] } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(current)),
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.post('/studio/projects/project-1/motions', async ({ request }) => {
      const body = await request.json() as Record<string, unknown>
      current = { ...current, revision: 5, manifest: { ...current.manifest, motions: [idle, { ...idle, motion_id: String(body.motion_id), name: String(body.name), preset: String(body.preset), semantic_slot: 'walk' }] } }
      return HttpResponse.json(current)
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><MotionWorkspace projectId="project-1" /></MemoryRouter></QueryClientProvider>)
  expect(await screen.findByRole('button', { name: 'Generate Idle' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Add motion' }))
  await user.click(screen.getByRole('button', { name: 'Add Walk' }))
  expect(await screen.findByRole('button', { name: 'Generate Walk' })).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'Walk To generate' })).toHaveAttribute('aria-selected', 'true')
  await client.invalidateQueries({ queryKey: ['project', 'project-1'] })
  expect(screen.getByRole('button', { name: 'Generate Walk' })).toBeInTheDocument()
})

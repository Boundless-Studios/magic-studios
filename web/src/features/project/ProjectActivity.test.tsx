import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { projectFixture } from '../../test/studioTestUtils'
import { ProjectActivity } from './ProjectActivity'

let reconciled = false
const settledProject = { ...projectFixture, revision: 5, manifest: { ...projectFixture.manifest, motions: [{ motion_id: 'idle', name: 'Idle', kind: 'preset' as const, semantic_slot: 'idle', loop_policy: 'loop' as const, required: true, candidates: [{ candidate_id: 'idle-a', artifact_key: 'idle-a.png', sha256: 'a', metadata: null, assessment: null, duration_ms: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null }], selected_candidate_id: 'idle-a' }] } }
const completedUnsettled = { job_id: 'job-1', status: 'completed', motion_id: 'idle', output_key: 'idle-a.png', progress_percent: null, stage: 'awaiting_settlement', failure_code: null, failure_explanation: null, retryable: false }

const server = setupServer(
  http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: [], next_cursor: null })),
  http.get('/studio/projects/project-1', () => HttpResponse.json(reconciled ? settledProject : projectFixture)),
  http.get('/studio/projects/project-1/operations', () => HttpResponse.json(reconciled ? [] : [completedUnsettled])),
  http.post('/studio/projects/project-1/activity/reconcile', () => { reconciled = true; return HttpResponse.json({ operations: [], next_cursor: null }) }),
  http.get('/pool/workers', () => HttpResponse.json({ workers: [] })),
  http.get('/pool/queue', () => HttpResponse.json({ queued: 0, processing: 0, by_capability: {} })),
)
beforeAll(() => server.listen())
afterEach(() => { server.resetHandlers(); reconciled = false })
afterAll(() => server.close())

function renderActivity() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/projects/project-1/motions']}>
        <ProjectActivity projectId="project-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it('settles a completed-but-unsettled operation without the Activity panel being opened', async () => {
  renderActivity()
  expect(screen.getByRole('button', { name: /GPU status/ })).toBeInTheDocument()
  await waitFor(() => expect(reconciled).toBe(true), { timeout: 5000 })
})

it('offers a per-row Cancel for a live operation and a header Cancel all running (T13)', async () => {
  const user = userEvent.setup()
  let cancelledJob = false
  let cancelledAll = false
  const runningOperations = [
    { job_id: 'job-run', operation: 'generate_motion', status: 'processing', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false },
  ]
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json(runningOperations)),
    // A merely-running job is not settled by reconcile; it comes back unchanged (BOU-3208: the
    // shared poller now reconciles while anything is queued or processing, not only while
    // awaiting settlement).
    http.post('/studio/projects/project-1/activity/reconcile', () => HttpResponse.json({ operations: runningOperations, next_cursor: null })),
    http.post('/studio/projects/project-1/motions/idle/operations/job-run/cancel', () => { cancelledJob = true; return HttpResponse.json({ job_id: 'job-run', status: 'cancelled', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'cancelled', failure_code: null, failure_explanation: null, retryable: true }) }),
    http.post('/studio/projects/project-1/operations/cancel-all', () => { cancelledAll = true; return HttpResponse.json({ cancelled: ['job-run'], skipped: [] }) }),
  )
  renderActivity()
  await user.click(screen.getByRole('button', { name: /GPU status/ }))
  expect(await screen.findByRole('button', { name: 'Cancel all running' })).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Interrupt Idle' }))
  expect(cancelledJob).toBe(true)
  await user.click(screen.getByRole('button', { name: 'Cancel all running' }))
  expect(cancelledAll).toBe(true)
})

it('omits Cancel all running when nothing is live', async () => {
  const user = userEvent.setup()
  renderActivity()
  await user.click(screen.getByRole('button', { name: /GPU status/ }))
  await waitFor(() => expect(reconciled).toBe(true))
  expect(screen.queryByRole('button', { name: 'Cancel all running' })).not.toBeInTheDocument()
})

it('renders one strip per pool GPU with utilization and memory, plus the queue pill', async () => {
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.get('/pool/workers', () => HttpResponse.json({ cluster_admin: true, workers: [
      { worker_id: 'rig-a', status: 'busy', last_heartbeat_at: '2026-09-05T18:00:00Z', capabilities: ['motion.v1'], busy_job_id: 'job-elsewhere', draining: false, gpus: [{ index: 0, name: 'RTX 4090', utilization_pct: 73, memory_used_mb: 12288, memory_total_mb: 24576 }] },
    ] })),
    http.get('/pool/queue', () => HttpResponse.json({ queued: 2, processing: 1, by_capability: {} })),
  )
  renderActivity()
  expect(await screen.findByText('rig-a')).toBeInTheDocument()
  expect(screen.getByText('73%')).toBeInTheDocument()
  expect(screen.getByText('12.0 / 24.0 GB')).toBeInTheDocument()
  // The pool-wide queue is fetched once the Studio has confirmed a cluster admin.
  expect(await screen.findByText('2 queued')).toBeInTheDocument()
  expect(screen.queryByText(/failed/)).not.toBeInTheDocument()
})

it('says no GPU is online when the pool holds none', async () => {
  server.use(http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])))
  renderActivity()
  expect(await screen.findByText('No GPU online')).toBeInTheDocument()
})

it('dims the last known strips and marks them stale when the pool call fails', async () => {
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.get('/pool/workers', () => HttpResponse.error()),
    http.get('/pool/queue', () => HttpResponse.error()),
  )
  renderActivity()
  expect(await screen.findByText('stale')).toBeInTheDocument()
})

it('collapses consecutive failures of the same motion into one row', async () => {
  const user = userEvent.setup()
  const failure = (job: string, at: string) => ({ job_id: job, operation: 'generate_motion', status: 'failed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Worker interrupted.', retryable: true, created_at: at, updated_at: at })
  const failures = [failure('a', '2026-09-05T18:00:00Z'), failure('b', '2026-09-05T18:05:00Z'), failure('c', '2026-09-05T18:10:00Z')]
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json(failures)),
    http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: failures, next_cursor: null })),
  )
  renderActivity()
  await user.click(screen.getByRole('button', { name: /GPU status/ }))
  expect(await screen.findByText('Failed 3×')).toBeInTheDocument()
  expect(screen.getAllByText('Worker interrupted.')).toHaveLength(1)
  expect(screen.getAllByRole('button', { name: 'Retry Idle' })).toHaveLength(1)
  await user.click(screen.getByRole('button', { name: 'Show the 3 attempts' }))
  expect(screen.getByText(/job-a|a ·/)).toBeInTheDocument()
})

it('counts settled work from the ledger once the live feed is empty', async () => {
  const settled = (job: string) => ({ job_id: job, operation: 'generate_motion', status: 'completed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'settled', failure_code: null, failure_explanation: null, retryable: false, created_at: '2026-09-05T18:00:00Z', updated_at: '2026-09-05T18:16:00Z', generation_settings: null })
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
    http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: [settled('a'), settled('b'), settled('c')], next_cursor: null })),
  )
  renderActivity()
  await waitFor(() => expect(screen.getByRole('button', { name: /GPU status, 0 running, 0 queued, 0 failed/ })).toBeInTheDocument())
})

it('shows the stats line for a mix of running, failed, and complete operations, unopened', async () => {
  const mixedOperations = [
    { job_id: 'job-run', operation: 'generate_motion', status: 'processing', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false, updated_at: '2026-01-01T15:42:00Z' },
    { job_id: 'job-fail', operation: 'generate_motion', status: 'failed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'oops', retryable: true, updated_at: '2026-01-01T10:00:00Z' },
    { job_id: 'job-done', operation: 'generate_motion', status: 'completed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'settled', failure_code: null, failure_explanation: null, retryable: false, updated_at: '2026-01-01T09:00:00Z' },
  ]
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json(mixedOperations)),
    // The running job keeps this poll "in flight" (BOU-3208), so reconcile is called; it must
    // echo the same mix back rather than settle it, since only job-done is already settled.
    http.post('/studio/projects/project-1/activity/reconcile', () => HttpResponse.json({ operations: mixedOperations, next_cursor: null })),
  )
  renderActivity()
  await waitFor(() => expect(screen.getByRole('button', { name: /GPU status, 1 running, 0 queued, 1 failed/ })).toBeInTheDocument())
})

it('keeps offline GPUs visible by machine name without presenting old metrics as live', async () => {
  server.use(http.get('/pool/workers', () => HttpResponse.json({ cluster_admin: true, workers: [
    { worker_id: 'justin-worker', label: 'Justin desktop', status: 'offline', last_heartbeat_at: '2026-09-05T18:00:00Z', capabilities: [], busy_job_id: null, draining: false, gpus: [{ index: 0, name: 'RTX 3090', utilization_pct: 73, memory_used_mb: 12288, memory_total_mb: 24576 }] },
    { worker_id: 'unnamed-worker', label: 'Unnamed machine', status: 'online', last_heartbeat_at: null, capabilities: [], busy_job_id: null, draining: false, gpus: [] },
  ] })))
  renderActivity()
  expect(await screen.findByText('Justin desktop')).toBeInTheDocument()
  expect(screen.getByText('RTX 3090')).toBeInTheDocument()
  expect(screen.getByText('Offline')).toBeInTheDocument()
  expect(screen.queryByText('73%')).not.toBeInTheDocument()
  expect(screen.getByText('unnamed-worker')).toBeInTheDocument()
  expect(screen.getByText('Connected · GPU not reported')).toBeInTheDocument()
})

it('never asks a non-admin for the pool-wide queue and counts their own queued work', async () => {
  let queueRequested = false
  const queuedOperation = { job_id: 'job-wait', operation: 'generate_motion', status: 'queued', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false }
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([queuedOperation])),
    http.post('/studio/projects/project-1/activity/reconcile', () => HttpResponse.json({ operations: [queuedOperation], next_cursor: null })),
    http.get('/pool/workers', () => HttpResponse.json({ cluster_admin: false, workers: [] })),
    http.get('/pool/queue', () => { queueRequested = true; return HttpResponse.json({ detail: 'pool administrator required' }, { status: 403 }) }),
  )
  renderActivity()
  await waitFor(() => expect(screen.getByRole('button', { name: /GPU status, 0 running, 1 queued, 0 failed/ })).toBeInTheDocument())
  expect(screen.queryByText('stale')).not.toBeInTheDocument()
  expect(queueRequested).toBe(false)
})

it('names a GPU busy on a turnaround as a turnaround, not Subject work', async () => {
  const turnaround = { job_id: 'job-turn', operation: 'generate_turnaround', status: 'processing', motion_id: null, turnaround_id: 'turnaround-abc', output_key: 'turn.mp4', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false }
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([turnaround])),
    http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: [turnaround], next_cursor: null })),
    http.get('/pool/workers', () => HttpResponse.json({ workers: [
      { worker_id: 'rig-a', status: 'busy', last_heartbeat_at: '2026-09-05T18:00:00Z', capabilities: ['motion.v1'], busy_job_id: 'job-turn', draining: false, gpus: [] },
    ] })),
  )
  const user = userEvent.setup()
  renderActivity()
  await user.click(await screen.findByRole('button', { name: /GPU status/ }))
  const busy = await screen.findAllByText(/^Turnaround/)
  expect(busy.length).toBeGreaterThan(0)
  expect(screen.queryByText(/^Subject/)).not.toBeInTheDocument()
})

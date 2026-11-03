import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { projectFixture, queryWrapper } from '../../test/studioTestUtils'
import { SubjectWorkspace } from './SubjectWorkspace'
import { clearSubjectDraft } from './subjectDraft'

const assessed = { ...projectFixture, revision: 5, manifest: { ...projectFixture.manifest, subject: { current_revision: { revision_id: 'subject-r1', artifact_key: 'outputs/studio/project-1/subjects/job.png', sha256: 'a'.repeat(64), width: 512, height: 512, lifecycle: 'ready' as const, parent_revision_ids: [] }, assessment: { assessment_id: 'assessment-1', policy_version: 'subject-v1', artifact_sha256: 'a'.repeat(64), width: 512, height: 512, occupancy_ratio: .72, findings: [] }, approval: { ...projectFixture.manifest.subject.approval, state: 'ready_for_signoff' as const } } } }
const approved = { ...assessed, revision: 6, manifest: { ...assessed.manifest, subject: { ...assessed.manifest.subject, approval: { ...assessed.manifest.subject.approval, state: 'approved' as const, approver_id: 'actor-1' } } } }
let generated = false
// Toggled by the /subject/generate handler so a test can observe the in-flight window between
// submitting and the job settling, without a real async worker (BOU-3208 component coverage).
let inFlight = false
const inFlightOperation = { job_id: 'subject-job', operation: 'generate_subject' as const, status: 'processing' as const, motion_id: null, output_key: 'outputs/studio/project-1/subjects/job.png', progress_percent: null, stage: 'generating' as const, failure_code: null, failure_explanation: null, retryable: false }
const TEST_TARGET = { system: 'acme', display_name: 'Acme', entity_kinds: ['character', 'journey_traveller'], delivery_configured: true, slots: [{ name: 'idle', accepts: ['idle'], winner: 'idle' }] }
const server = setupServer(
  http.get('/studio/targets', () => HttpResponse.json([TEST_TARGET])),
  http.get('/studio/projects/project-1', () => HttpResponse.json(generated ? assessed : projectFixture)),
  http.get('/studio/projects/project-1/operations', () => HttpResponse.json(inFlight ? [inFlightOperation] : [])),
  http.post('/studio/projects/project-1/activity/reconcile', () => HttpResponse.json({ operations: inFlight ? [inFlightOperation] : [], next_cursor: null })),
  http.post('/studio/projects/project-1/subject/references/uploads', async ({ request }) => { expect(await request.json()).toEqual({ content_type: 'image/jpeg', expected_revision: 4 }); return HttpResponse.json({ key: 'sources/studio/project-1/subject-references/a.jpg', upload_url: '/upload/a' }) }),
  http.put('/upload/a', ({ request }) => { expect(request.headers.get('content-type')).toBe('image/jpeg'); return new HttpResponse(null, { status: 204 }) }),
  http.post('/studio/projects/project-1/subject/generate', async ({ request }) => { const body = await request.json() as { reference_keys: string[]; direction: string }; expect(body.reference_keys).toEqual(['sources/studio/project-1/subject-references/a.jpg']); expect(body.direction).toBe('Keep the red scarf'); generated = true; inFlight = true; return HttpResponse.json({ job_id: 'subject-job', status: 'queued', motion_id: null, output_key: 'outputs/studio/project-1/subjects/job.png', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false }) }),
  http.post('/studio/projects/project-1/subject/approve', () => HttpResponse.json(approved)),
  http.post('/studio/projects/project-1/assets/preview', () => HttpResponse.json({ key: 'out', upload_url: 'https://assets.invalid/subject.png' })),
  http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: [], next_cursor: null })),
)
beforeAll(() => { vi.stubGlobal('crypto', { randomUUID: () => 'request-1' }); URL.createObjectURL = vi.fn(() => 'blob:reference'); URL.revokeObjectURL = vi.fn(); server.listen() })
// References now persist across a generation (BOU-3208 operator feedback: kept for the next
// Regenerate rather than cleared), via a module-level draft keyed by project id -- reset it
// between tests so one test's upload does not bleed into the next's, which all share "project-1".
afterEach(() => { generated = false; inFlight = false; server.resetHandlers(); clearSubjectDraft('project-1') }); afterAll(() => server.close())

it('accepts an ordinary JPEG reference and starts subject generation', async () => {
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText(/Add one or more reference images/)).toBeInTheDocument()
  expect(screen.queryByText(/transparent PNG here/i)).not.toBeInTheDocument()
  await user.upload(screen.getByLabelText('Choose subject references'), new File(['jpeg'], 'hero.jpg', { type: 'image/jpeg' }))
  await user.type(screen.getByLabelText(/Guidance/), 'Keep the red scarf')
  await user.click(screen.getByRole('button', { name: 'Generate subject' }))
  expect(await screen.findByText('Ready for approval')).toBeInTheDocument()
})

it('keeps the uploaded references visible while generation is in flight, and turns the button into its own progress indicator (BOU-3208)', async () => {
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.upload(await screen.findByLabelText('Choose subject references'), new File(['jpeg'], 'hero.jpg', { type: 'image/jpeg' }))
  await user.type(screen.getByLabelText(/Guidance/), 'Keep the red scarf')
  await user.click(screen.getByRole('button', { name: 'Generate subject' }))
  // The button becomes the progress indicator -- spinner plus "Generating…" -- instead of a
  // separate status line, and stays disabled while the job is in flight.
  const button = await screen.findByRole('button', { name: /Generating…/ })
  expect(button).toBeDisabled()
  expect(button.querySelector('.button-spinner')).not.toBeNull()
  // The reference used to start this job is no longer cleared on submit -- it stays visible for
  // the next Regenerate rather than being treated as consumed.
  expect(screen.getByAltText('Reference 1: hero.jpg')).toBeInTheDocument()
})

it('reconciles a subject job that is completed but awaiting settlement, and the Chosen card appears once the project refetch lands -- without a reload (BOU-3208 follow-up)', async () => {
  let opsCalls = 0
  let reconcileCalls = 0
  const awaitingSettlement = { job_id: 'subject-job', operation: 'generate_subject' as const, status: 'completed' as const, motion_id: null, output_key: 'outputs/studio/project-1/subjects/job.png', progress_percent: null, stage: 'awaiting_settlement' as const, failure_code: null, failure_explanation: null, retryable: false }
  const settled = { ...awaitingSettlement, stage: 'settled' as const }
  server.use(
    // The job is already sitting at completed/awaiting_settlement the moment the page loads --
    // exactly the pool-executed state that had left the Subject page stuck on "Generating…" in
    // production even though a manual reconcile call settled it instantly.
    http.get('/studio/projects/project-1/operations', () => { opsCalls += 1; return HttpResponse.json([awaitingSettlement]) }),
    http.post('/studio/projects/project-1/activity/reconcile', () => { reconcileCalls += 1; generated = true; return HttpResponse.json({ operations: [settled], next_cursor: null }) }),
  )
  render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  // Reconcile is reached on the very first poll (it runs inside the same fetch as the plain
  // listing whenever anything is in flight), not only on a later interval tick.
  await waitFor(() => expect(reconcileCalls).toBe(1))
  expect(opsCalls).toBe(1)
  // Settling the job invalidates the project query, which refetches the assessed manifest and
  // shows the take -- no reload, no extra click.
  expect(await screen.findByText('Ready for approval')).toBeInTheDocument()
})

it('supports up to six reorderable and removable references', async () => {
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.upload(await screen.findByLabelText('Choose subject references'), [new File(['a'], 'front.webp', { type: 'image/webp' }), new File(['b'], 'side.png', { type: 'image/png' })])
  expect(screen.getByAltText('Reference 1: front.webp')).toBeInTheDocument(); expect(screen.getByAltText('Reference 2: side.png')).toBeInTheDocument()
  await user.click(screen.getAllByRole('button', { name: 'Move earlier' })[1]); expect(screen.getByAltText('Reference 1: side.png')).toBeInTheDocument()
  await user.click(screen.getAllByRole('button', { name: 'Remove' })[0]); expect(screen.queryByAltText(/side.png/)).not.toBeInTheDocument()
})

it('keeps a prepared transparent PNG as an advanced shortcut', async () => {
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() }); await user.click(await screen.findByRole('button', { name: 'Advanced' })); expect(screen.getByText('Use a prepared cutout')).toBeInTheDocument()
})

it('rejects invalid reference media before upload', async () => {
  render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() }); fireEvent.change(await screen.findByLabelText('Choose subject references'), { target: { files: [new File(['gif'], 'hero.gif', { type: 'image/gif' })] } }); expect(await screen.findByText('Choose PNG, JPEG, or WebP reference images.')).toBeInTheDocument()
})

it('approves the generated subject and continues to motions', async () => {
  generated = true; const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() }); await user.click(await screen.findByRole('button', { name: 'Approve subject' })); expect(await screen.findByText(/Subject approved/)).toBeInTheDocument()
})

it('"Correct with feedback" opens an inline form on the Chosen card, and Cancel closes it without touching the left column', async () => {
  generated = true
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Correct with feedback' }))
  expect(screen.getByLabelText('What should change?')).toBeInTheDocument()
  // The compose card's own Guidance field and Generate/Regenerate button are untouched by the
  // Chosen card's inline correction form -- correcting no longer has a left-column mode.
  expect(screen.getByLabelText(/Guidance/)).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Regenerate subject' })).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(screen.queryByLabelText('What should change?')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Correct with feedback' })).toBeInTheDocument()
})

it('runs "Correct with feedback" in one step: the chosen take\'s key first, then the already-staged references, and the typed feedback', async () => {
  generated = true
  server.use(
    http.post('/studio/projects/project-1/subject/references/uploads', async ({ request }) => { expect(await request.json()).toEqual({ content_type: 'image/jpeg', expected_revision: 5 }); return HttpResponse.json({ key: 'sources/studio/project-1/subject-references/a.jpg', upload_url: '/upload/a' }) }),
    http.post('/studio/projects/project-1/subject/generate', async ({ request }) => {
      const body = await request.json() as { reference_keys: string[]; direction: string }
      expect(body.reference_keys).toEqual(['outputs/studio/project-1/subjects/job.png', 'sources/studio/project-1/subject-references/a.jpg'])
      expect(body.direction).toBe('Make the cloak green')
      inFlight = true
      return HttpResponse.json({ job_id: 'subject-job', status: 'queued', motion_id: null, output_key: 'outputs/studio/project-1/subjects/job.png', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false })
    }),
  )
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.upload(await screen.findByLabelText('Choose subject references'), new File(['jpeg'], 'hero.jpg', { type: 'image/jpeg' }))
  await user.click(screen.getByRole('button', { name: 'Correct with feedback' }))
  await user.type(screen.getByLabelText('What should change?'), 'Make the cloak green')
  await user.click(screen.getByRole('button', { name: 'Correct' }))
  // The gallery shows the running placeholder the same way any other generation does, with no
  // separate button in the left column needing a second press.
  expect(await screen.findByRole('status', { name: 'Generating a new take' })).toBeInTheDocument()
})

it('"Start another take" keeps the references and the guidance, and sits next to Generate', async () => {
  generated = true
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.upload(await screen.findByLabelText('Choose subject references'), new File(['jpeg'], 'hero.jpg', { type: 'image/jpeg' }))
  await user.type(screen.getByLabelText(/Guidance/), 'Keep the red scarf')
  const actions = screen.getByRole('button', { name: 'Regenerate subject' }).parentElement
  await user.click(within(actions as HTMLElement).getByRole('button', { name: 'Start another take' }))
  expect(screen.getByAltText('Reference 1: hero.jpg')).toBeInTheDocument()
  expect(screen.getByLabelText(/Guidance/)).toHaveValue('Keep the red scarf')
  expect(screen.getByLabelText(/Guidance/)).toHaveFocus()
  expect(await screen.findByText('Adjust the references or guidance, then Generate')).toBeInTheDocument()
})

it('asks for confirmation before starting another take once the subject is approved', async () => {
  generated = true
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json(approved)))
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Start another take' }))
  expect(screen.getByText('This starts a new take; the current subject stays until you choose the new one.')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Yes, start another take' }))
  expect(screen.queryByText('This starts a new take; the current subject stays until you choose the new one.')).not.toBeInTheDocument()
  expect(await screen.findByText('Adjust the references or guidance, then Generate')).toBeInTheDocument()
})

it('accepts a reference image dropped onto the drop zone', async () => {
  render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const zone = (await screen.findByLabelText('Choose subject references')).closest('.upload-panel') as HTMLElement
  const file = new File(['jpeg'], 'dropped.jpg', { type: 'image/jpeg' })
  fireEvent.dragOver(zone, { dataTransfer: { files: [file] } })
  expect(zone.className).toContain('upload-panel--dragging')
  fireEvent.drop(zone, { dataTransfer: { files: [file] } })
  expect(await screen.findByAltText('Reference 1: dropped.jpg')).toBeInTheDocument()
  expect(zone.className).not.toContain('upload-panel--dragging')
})

it('shows the guidance helper text and no placeholder that suggests preserving things', async () => {
  render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const guidance = await screen.findByLabelText(/Guidance/)
  expect(guidance).not.toHaveAttribute('placeholder')
  expect(screen.getByText(/Leave empty to match the reference/)).toBeInTheDocument()
  expect(screen.queryByText(/Preserve/i)).not.toBeInTheDocument()
})

it('binds and clears an external subject under Advanced', async () => {
  const bound = { ...projectFixture, revision: 5, manifest: { ...projectFixture.manifest, external_subject: { system: 'acme' as const, entity_kind: 'character' as const, entity_id: 'char-42', display_name: 'Ser Ember' } } }
  const cleared = { ...bound, revision: 6, manifest: { ...bound.manifest, external_subject: null } }
  server.use(
    http.patch('/studio/projects/project-1', async ({ request }) => {
      const body = await request.json() as { expected_revision: number; external_subject: unknown }
      if (body.external_subject === null) { expect(body).toEqual({ expected_revision: 5, external_subject: null }); return HttpResponse.json(cleared) }
      expect(body).toEqual({ expected_revision: 4, external_subject: { system: 'acme', entity_kind: 'character', entity_id: 'char-42', display_name: 'Ser Ember' } })
      return HttpResponse.json(bound)
    }),
  )
  const user = userEvent.setup()
  render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Advanced' }))
  await user.type(screen.getByLabelText('External subject entity id'), 'char-42')
  await user.type(screen.getByLabelText('External subject display name'), 'Ser Ember')
  await user.click(screen.getByRole('button', { name: 'Bind external subject' }))
  expect(await screen.findByRole('button', { name: 'Update external subject' })).toBeInTheDocument()
  expect(await screen.findByDisplayValue('Ser Ember')).toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Clear external subject' }))
  expect(await screen.findByRole('button', { name: 'Bind external subject' })).toBeInTheDocument()
})

it('shows the server\'s reason, sentence-cased, when subject generation is rejected', async () => {
  const user = userEvent.setup()
  server.use(http.post('/studio/projects/project-1/subject/generate', () =>
    HttpResponse.json({ detail: 'reference images must show a single character' }, { status: 422 })))
  render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText(/Add one or more reference images/)).toBeInTheDocument()
  await user.upload(screen.getByLabelText('Choose subject references'), new File(['jpeg'], 'hero.jpg', { type: 'image/jpeg' }))
  await user.click(screen.getByRole('button', { name: 'Generate subject' }))
  expect(await screen.findByText('Reference images must show a single character')).toBeInTheDocument()
})

it('never treats a queued turnaround as subject work, though it has no motion_id either', async () => {
  const turnaround = { job_id: 'turn-job', operation: 'generate_turnaround' as const, turnaround_id: `turnaround-${'a'.repeat(32)}`, status: 'queued' as const, motion_id: null, output_key: 'outputs/studio/project-1/turnarounds/t/a.mp4', progress_percent: null, stage: 'queued' as const, failure_code: null, failure_explanation: null, retryable: false }
  let served = 0
  server.use(
    http.get('/studio/projects/project-1/operations', () => HttpResponse.json([turnaround])),
    // A queued job is in flight, so the poller reconciles and uses that listing.
    http.post('/studio/projects/project-1/activity/reconcile', () => { served += 1; return HttpResponse.json({ operations: [turnaround], next_cursor: null }) }),
  )
  const user = userEvent.setup(); render(<SubjectWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.upload(await screen.findByLabelText('Choose subject references'), new File(['jpeg'], 'hero.jpg', { type: 'image/jpeg' }))
  await waitFor(() => expect(served).toBeGreaterThan(0))
  await new Promise((resolve) => setTimeout(resolve, 50))
  expect(screen.getByRole('button', { name: 'Generate subject' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: /Generating…/ })).not.toBeInTheDocument()
})

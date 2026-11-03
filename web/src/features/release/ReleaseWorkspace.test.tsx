import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, expect, it } from 'vitest'
import { projectFixture, queryWrapper } from '../../test/studioTestUtils'
import { resetReleaseLog } from './releaseLog'
import { ReleaseWorkspace } from './ReleaseWorkspace'

const TEST_TARGET = { system: 'acme', display_name: 'Acme', entity_kinds: ['character', 'journey_traveller'], delivery_configured: true, slots: [{ name: 'idle', accepts: ['idle'], winner: 'idle' }] }
const subjectRevision = { revision_id: 'subject-r1', artifact_key: 's.png', sha256: 's', width: 512, height: 512, lifecycle: 'ready' as const, parent_revision_ids: [], source: 'generated' as const, direction: null, created_at: null }
const idleCandidate = { candidate_id: 'idle-r1', artifact_key: 'i.png', sha256: 'i', duration_ms: 1000, metadata: null, assessment: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right' as const, recipe: null, source: 'generated' as const, subject_revision_id: 'subject-r1' }
const ready = {
  ...projectFixture,
  manifest: {
    ...projectFixture.manifest,
    subject: { ...projectFixture.manifest.subject, current_revision: subjectRevision, approval: { ...projectFixture.manifest.subject.approval, state: 'approved' as const } },
    motions: [
      { motion_id: 'idle', name: 'Idle', kind: 'preset' as const, semantic_slot: 'idle', loop_policy: 'loop' as const, required: true, candidates: [idleCandidate], selected_candidate_id: 'idle-r1' },
      { motion_id: 'walk', name: 'Walk', kind: 'preset' as const, semantic_slot: 'walk', loop_policy: 'loop' as const, required: true, candidates: [], selected_candidate_id: null },
    ],
    set_assessment: { policy_version: 'set-policy-v1', findings: [] },
    set_assessment_id: 'set-a1', set_assessment_digest: 'digest', set_assessment_fresh: true,
  },
}
const release = { release_id: 'release-1', project_id: 'project-1', name: 'Launch set', source_revision: 5, manifest_digest: 'm', release_digest: 'r', subject_revision_id: 'subject-r1', motion_revision_ids: { idle: 'idle-r1' }, quality_policy_versions: ['set-v1'], set_approval: { ...projectFixture.manifest.subject.approval, state: 'approved' as const }, deliveries: [] }
const approved = { ...ready, revision: 5, manifest: { ...ready.manifest, set_approval: { ...ready.manifest.set_approval, state: 'approved' as const, assessment_id: 'set-a1', manifest_digest: 'digest' }, set_approval_fresh: true } }

const server = setupServer(
  http.get('/studio/targets', () => HttpResponse.json([TEST_TARGET])),
  http.get('/studio/projects/project-1', () => HttpResponse.json(ready)),
  http.get('/studio/projects/project-1/releases', () => HttpResponse.json([])),
  http.post('/studio/projects/project-1/assets/preview', ({ request }) => HttpResponse.json({ key: 'k', upload_url: `https://storage.test/${request.url}` })),
  http.post('/studio/projects/project-1/quality/set', () => HttpResponse.json({ ...ready, revision: 5, manifest: { ...ready.manifest, set_assessment_fresh: true } })),
  http.post('/studio/projects/project-1/set/approve', () => HttpResponse.json(approved)),
  http.post('/studio/projects/project-1/releases', () => HttpResponse.json(release)),
  http.post('/studio/releases/release-1/exports', async ({ request }) => { expect(await request.json()).toEqual({ profile: 'png-json', expected_revision: 5 }); return HttpResponse.json({ export_id: 'export-1', release_id: 'release-1', profile_name: 'png-json', profile_version: '1', artifact_key: 'exports/e.zip', sha256: 'e', size_bytes: 1024 }) }),
  http.get('/studio/exports/export-1/download', () => HttpResponse.json({ key: 'exports/e.zip', upload_url: 'https://storage.test/first-playable.zip' })),
)
beforeAll(() => server.listen()); afterEach(() => server.resetHandlers()); afterAll(() => server.close())
beforeEach(() => { resetReleaseLog() })

it('shows the set with the subject, the selected take, and a not-selected motion', async () => {
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await screen.findByRole('heading', { name: 'Set' })
  expect(await screen.findByRole('img', { name: 'Walk · not selected' })).toBeInTheDocument()
  expect(await screen.findByText('1 of 2 motions selected')).toBeInTheDocument()
})

it('runs the set assessment, shows no findings, and approves the set', async () => {
  const user = userEvent.setup()
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json({ ...ready, manifest: { ...ready.manifest, set_assessment_fresh: false } })))
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Run quality checks' }))
  expect(await screen.findByText('No set findings.')).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Approve animation set' }))
  expect(await screen.findByRole('button', { name: 'Animation set approved' })).toBeDisabled()
})

it('creates a release only once the set is approved, then requests an export', async () => {
  const user = userEvent.setup()
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('button', { name: 'Create immutable release' })).toHaveAttribute('aria-disabled', 'true')
  await user.click(await screen.findByRole('button', { name: 'Approve animation set' }))
  await user.type(screen.getByLabelText('Release name'), 'Launch set')
  await user.click(screen.getByRole('button', { name: 'Create immutable release' }))
  const card = await screen.findByRole('region', { name: 'Launch set' })
  await user.click(within(card).getByRole('button', { name: 'Request export' }))
  const saved: string[] = []
  const originalClick = HTMLAnchorElement.prototype.click
  HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) { saved.push(this.href) }
  await user.click(await within(card).findByRole('button', { name: 'Download PNG + JSON package' }))
  await waitFor(() => expect(saved).toEqual(['https://storage.test/first-playable.zip']))
  HTMLAnchorElement.prototype.click = originalClick
})

it('attributes a selected candidate finding to its motion with a Fix in Frames link, not a waiver', async () => {
  const withFinding = { ...ready, manifest: { ...ready.manifest, motions: [{ ...ready.manifest.motions[0], candidates: [{ ...idleCandidate, assessment: { policy_version: 'motion-v1', findings: [{ code: 'motion_missing_alpha', severity: 'blocking' as const, message: 'Sprite output must preserve transparency.', action_id: 'derive_alpha' }] } }] }, ready.manifest.motions[1]], set_assessment: { policy_version: 'set-v1', findings: [{ code: 'motion_missing_alpha', severity: 'blocking' as const, message: 'Sprite output must preserve transparency.', action_id: null }] } } }
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json(withFinding)))
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const finding = await screen.findByRole('listitem', { name: 'Idle: motion missing alpha' })
  expect(within(finding).getByRole('link', { name: 'Fix in Frames' })).toHaveAttribute('href', '/projects/project-1/motions')
  expect(within(finding).queryByRole('button', { name: /Waive/ })).not.toBeInTheDocument()
})

it('shows a set finding as a warning and approves without requiring a waiver', async () => {
  // Quality findings are warnings, never blockers (operator decision, 2026-09-05): a set
  // finding is informational and approval proceeds with no per-finding waiver control.
  const user = userEvent.setup()
  const withFinding = { ...ready, manifest: { ...ready.manifest, set_assessment: { policy_version: 'set-v1', findings: [{ code: 'loop_break', severity: 'warning' as const, message: 'The loop has a visible break.', action_id: null }] } } }
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json(withFinding)))
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('Heads up')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Waive/ })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Approve animation set' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Approve animation set' }))
  expect(await screen.findByRole('button', { name: 'Animation set approved' })).toBeDisabled()
})

it('sends an optional note for the record as the waiver reason on approve', async () => {
  const user = userEvent.setup()
  const withFinding = { ...ready, manifest: { ...ready.manifest, set_assessment: { policy_version: 'set-v1', findings: [{ code: 'loop_break', severity: 'warning' as const, message: 'The loop has a visible break.', action_id: null }] } } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(withFinding)),
    http.post('/studio/projects/project-1/set/approve', async ({ request }) => {
      expect(await request.json()).toMatchObject({ waiver_reason: 'Reviewed by hand' })
      return HttpResponse.json(approved)
    }),
  )
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await screen.findByText('Heads up')
  await user.type(screen.getByLabelText('Note for the record (optional)'), 'Reviewed by hand')
  await user.click(screen.getByRole('button', { name: 'Approve animation set' }))
  expect(await screen.findByRole('button', { name: 'Animation set approved' })).toBeDisabled()
})

it('offers Deliver to a configured target and shows the delivered status', async () => {
  const user = userEvent.setup()
  const delivered = { ...release, deliveries: [{ release_id: 'release-1', target: 'acme', status: 'delivered' as const, delivered_at: '2026-01-01T00:00:00Z', summary: '2 sheets, 1 bindings', details: {}, error: null, retryable: false }] }
  server.use(http.post('/studio/releases/release-1/deliveries/acme', () => HttpResponse.json(delivered.deliveries[0])), http.get('/studio/projects/project-1/releases', () => HttpResponse.json([release])))
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const card = await screen.findByRole('region', { name: 'Launch set' })
  await user.click(within(card).getByRole('button', { name: 'Deliver to Acme' }))
  expect(await within(card).findByText('Delivered to Acme: 2 sheets, 1 bindings')).toBeInTheDocument()
})

it('offers a retry when target delivery fails', async () => {
  const user = userEvent.setup()
  const failedRelease = { ...release, deliveries: [{ release_id: 'release-1', target: 'acme', status: 'failed' as const, delivered_at: null, summary: '', details: {}, error: 'importer rejected the package', retryable: false }] }
  server.use(
    http.get('/studio/projects/project-1/releases', () => HttpResponse.json([failedRelease])),
    http.post('/studio/releases/release-1/deliveries/acme', () => HttpResponse.json({ release_id: 'release-1', target: 'acme', status: 'delivered', delivered_at: '2026-01-01T00:00:00Z', summary: '1 sheets, 0 bindings', details: {}, error: null, retryable: false })),
  )
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const card = await screen.findByRole('region', { name: 'Launch set' })
  expect(await within(card).findByText(/Delivery to Acme failed: importer rejected the package/)).toBeInTheDocument()
  await user.click(within(card).getByRole('button', { name: 'Retry' }))
  expect(await within(card).findByText('Delivered to Acme: 1 sheets, 0 bindings')).toBeInTheDocument()
})

it('refreshes and explains a revision conflict on approve', async () => {
  const user = userEvent.setup()
  server.use(http.post('/studio/projects/project-1/set/approve', () => HttpResponse.json({ detail: { message: 'expected revision is stale', current_revision: 5 } }, { status: 409 })))
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Approve animation set' }))
  expect(await screen.findByText('This project changed elsewhere. Your view has been refreshed.')).toBeInTheDocument()
})

it('identifies supplied views on duplicate-action approval thumbnails and finding cards', async () => {
  const finding = { code: 'support_drift', message: 'Check support before approval.', severity: 'warning' }
  const north = { ...ready.manifest.motions[0], motion_id: 'n-idle', view_id: 'N', candidates: [{ ...idleCandidate, assessment: { policy_version: 'test', findings: [finding] } }] }
  const southwest = { ...north, motion_id: 'sw-idle', view_id: 'SW' }
  server.use(http.get('/studio/projects/project-1', () => HttpResponse.json({ ...ready, manifest: { ...ready.manifest, views: [{ view_id: 'N', name: 'North', reference: null }, { view_id: 'SW', name: 'Southwest', reference: null }], motions: [north, southwest, ready.manifest.motions[0]] } })))
  render(<ReleaseWorkspace projectId="project-1" />, { wrapper: queryWrapper() })
  const selected = within(await screen.findByRole('list', { name: 'Selected takes' }))
  expect(await selected.findByText('North (N) · Idle')).toBeInTheDocument()
  expect(await selected.findByText('Southwest (SW) · Idle')).toBeInTheDocument()
  expect(await selected.findByText('Idle', { exact: true })).toBeInTheDocument()
  expect(await screen.findByRole('heading', { name: 'North (N) · Idle: support drift' })).toBeInTheDocument()
  expect(await screen.findByRole('heading', { name: 'Southwest (SW) · Idle: support drift' })).toBeInTheDocument()
})

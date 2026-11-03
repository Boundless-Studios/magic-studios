import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { projectFixture, queryWrapper } from '../../test/studioTestUtils'
import { useProject } from '../../api/queries'
import type { Motion, ProjectResponse, SubjectRevision } from '../../api/types'
import { SubjectTakesGallery } from './SubjectTakesGallery'

/** Selecting a take round-trips through the project query cache (`useProjectMutation`), which a
 * gallery rendered with a static `project` prop -- as most tests here do -- never observes. These
 * two tests instead mount the gallery behind `useProject` so the mutation's own cache update
 * flows back into what the component renders, matching how `SubjectWorkspace` uses it live. */
function Harness({ projectId, onApproved = vi.fn() }: { projectId: string; onApproved?: () => void }) {
  const project = useProject(projectId)
  if (!project.data) return null
  return <SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={onApproved} onCorrectSubmit={vi.fn()} project={project.data} projectId={projectId} />
}

function revision(overrides: Partial<SubjectRevision>): SubjectRevision {
  return { revision_id: 'r1', artifact_key: 'k1', sha256: 'a'.repeat(64), width: 512, height: 512, lifecycle: 'ready', parent_revision_ids: [], source: 'generated', direction: null, created_at: null, ...overrides }
}

function boundMotion(subjectRevisionId: string): Motion {
  return {
    motion_id: 'idle', name: 'Idle', kind: 'preset', semantic_slot: null, loop_policy: 'loop', required: true, selected_candidate_id: null,
    candidates: [{ candidate_id: 'c1', artifact_key: 'k', sha256: 'a'.repeat(64), duration_ms: null, metadata: null, assessment: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null, facing: 'right', recipe: null, source: 'generated', subject_revision_id: subjectRevisionId }],
  }
}

const olderRevision = revision({ revision_id: 'r1', artifact_key: 'k1' })
const currentRevision = revision({ revision_id: 'r2', artifact_key: 'k2' })

function projectWith(overrides: Partial<ProjectResponse['manifest']['subject']>, motions: ProjectResponse['manifest']['motions'] = []): ProjectResponse {
  return {
    ...projectFixture,
    revision: 4,
    manifest: {
      ...projectFixture.manifest,
      motions,
      subject: {
        current_revision: currentRevision,
        revisions: [currentRevision, olderRevision],
        assessment: null,
        approval: { state: 'ready_for_signoff', assessment_id: null, approver_id: null, policy_version: null, artifact_sha256: null, manifest_digest: null, waived_finding_ids: [], waiver_reason: null },
        ...overrides,
      },
    },
  }
}

const server = setupServer(
  http.post('/studio/projects/project-1/assets/preview', ({ request }) => HttpResponse.json({ key: 'out', upload_url: `https://assets.invalid/${new URL(request.url).pathname}` })),
)
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

it('numbers takes from the oldest revision, pins the current one as Chosen, and lists the rest under Previous subjects', async () => {
  const project = projectWith({})
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  const chosen = await screen.findByLabelText('Take 2, chosen')
  expect(within(chosen).getByText('Chosen')).toBeInTheDocument()
  expect(within(chosen).getByText(/Take 2 · 512 × 512 · Generated/)).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Previous subjects' })).toBeInTheDocument()
  expect(await screen.findByLabelText('Take 1')).toBeInTheDocument()
})

it('hides the "Animating this subject" line entirely when no motions are bound, and shows it once they are', async () => {
  const project = projectWith({})
  const { unmount } = render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByLabelText('Take 2, chosen')).toBeInTheDocument()
  expect(screen.queryByText(/Animating this subject/)).not.toBeInTheDocument()
  unmount()

  const bound = projectWith({}, [boundMotion(currentRevision.revision_id)])
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={bound} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('Animating this subject · 1 motion')).toBeInTheDocument()
})

it('shows a "Choose one to continue" hint when takes exist but none is current', async () => {
  const project = projectWith({ current_revision: null })
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('Choose one to continue')).toBeInTheDocument()
  expect(screen.queryByText('Chosen')).not.toBeInTheDocument()
})

it('shows a live placeholder card while a generation is running', async () => {
  const project = projectWith({})
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={12} activeGeneration onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('status', { name: 'Generating a new take' })).toHaveTextContent('Generating… 12 s')
})

it('shows the empty-state line under Previous subjects when the chosen take is the only one', async () => {
  const project = projectWith({}, [])
  const solo: ProjectResponse = { ...project, manifest: { ...project.manifest, subject: { ...project.manifest.subject, revisions: [currentRevision] } } }
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={solo} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('No other subjects yet · generate or correct to create one')).toBeInTheDocument()
  expect(screen.queryByLabelText('Take 1')).not.toBeInTheDocument()
})

it('no longer carries a "New subject" button in its heading (operator, 2026-09-06)', async () => {
  const project = projectWith({}, [])
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('heading', { name: 'Subject takes' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /New subject/ })).not.toBeInTheDocument()
})

it('opens an inline correction form on the Chosen card and submits the typed direction', async () => {
  const project = projectWith({}, [])
  const onCorrectSubmit = vi.fn().mockResolvedValue(undefined)
  const user = userEvent.setup()
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={onCorrectSubmit} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Correct with feedback' }))
  const field = screen.getByLabelText('What should change?')
  await user.type(field, 'make the cloak green')
  await user.click(screen.getByRole('button', { name: 'Correct' }))
  expect(onCorrectSubmit).toHaveBeenCalledWith(currentRevision, 'make the cloak green')
  // The form closes back to the plain "Correct with feedback" button on success.
  expect(await screen.findByRole('button', { name: 'Correct with feedback' })).toBeInTheDocument()
  expect(screen.queryByLabelText('What should change?')).not.toBeInTheDocument()
})

it('cancels the inline correction form without submitting', async () => {
  const project = projectWith({}, [])
  const onCorrectSubmit = vi.fn()
  const user = userEvent.setup()
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={onCorrectSubmit} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Correct with feedback' }))
  await user.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(onCorrectSubmit).not.toHaveBeenCalled()
  expect(screen.queryByLabelText('What should change?')).not.toBeInTheDocument()
})

it('shows the "Correcting…" spinner on the inline form while a correction is in flight', async () => {
  const project = projectWith({}, [])
  const user = userEvent.setup()
  render(<SubjectTakesGallery correcting elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Correct with feedback' }))
  expect(screen.getByRole('button', { name: 'Correcting…' })).toBeDisabled()
})

it('surfaces a rejected correction inline on the Chosen card', async () => {
  const project = projectWith({}, [])
  const onCorrectSubmit = vi.fn().mockRejectedValue(new Error('subject generation is unavailable'))
  const user = userEvent.setup()
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={onCorrectSubmit} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Correct with feedback' }))
  await user.click(screen.getByRole('button', { name: 'Correct' }))
  expect(await screen.findByText('subject generation is unavailable')).toBeInTheDocument()
})

it('auto-approves a chosen take when its findings are only advisory (decision 1)', async () => {
  const advisoryAssessment = { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: olderRevision.sha256, width: 512, height: 512, occupancy_ratio: 0.7, findings: [{ code: 'subject_occupancy_too_low' as const, severity: 'warning' as const, title: 'Low occupancy', explanation: 'e', artifact_sha256: olderRevision.sha256, action_id: null }] }
  const selected: ProjectResponse = { ...projectFixture, revision: 5, manifest: { ...projectFixture.manifest, subject: { current_revision: { ...olderRevision, assessment: advisoryAssessment }, revisions: [{ ...olderRevision, assessment: advisoryAssessment }, currentRevision], assessment: advisoryAssessment, approval: { state: 'ready_for_signoff', assessment_id: null, approver_id: null, policy_version: null, artifact_sha256: null, manifest_digest: null, waived_finding_ids: [], waiver_reason: null } } } }
  const approved: ProjectResponse = { ...selected, revision: 6, manifest: { ...selected.manifest, subject: { ...selected.manifest.subject, approval: { ...selected.manifest.subject.approval, state: 'approved', approver_id: 'actor-1' } } } }
  let approveCalled = false
  const onApproved = vi.fn()
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(projectWith({}))),
    http.post('/studio/projects/project-1/subject/revisions/r1/select', () => HttpResponse.json(selected)),
    http.post('/studio/projects/project-1/subject/approve', () => { approveCalled = true; return HttpResponse.json(approved) }),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" onApproved={onApproved} />, { wrapper: queryWrapper() })
  const otherCard = await screen.findByLabelText('Take 1')
  await user.click(within(otherCard).getByRole('button', { name: 'Use this subject' }))
  await screen.findByText(/Subject approved/)
  expect(approveCalled).toBe(true)
  expect(onApproved).toHaveBeenCalledTimes(1)
})

it('does not call onApproved when the auto-approve step itself fails', async () => {
  const advisoryAssessment = { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: olderRevision.sha256, width: 512, height: 512, occupancy_ratio: 0.7, findings: [{ code: 'subject_occupancy_too_low' as const, severity: 'warning' as const, title: 'Low occupancy', explanation: 'e', artifact_sha256: olderRevision.sha256, action_id: null }] }
  const selected: ProjectResponse = { ...projectFixture, revision: 5, manifest: { ...projectFixture.manifest, subject: { current_revision: { ...olderRevision, assessment: advisoryAssessment }, revisions: [{ ...olderRevision, assessment: advisoryAssessment }, currentRevision], assessment: advisoryAssessment, approval: { state: 'ready_for_signoff', assessment_id: null, approver_id: null, policy_version: null, artifact_sha256: null, manifest_digest: null, waived_finding_ids: [], waiver_reason: null } } } }
  const onApproved = vi.fn()
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(projectWith({}))),
    http.post('/studio/projects/project-1/subject/revisions/r1/select', () => HttpResponse.json(selected)),
    http.post('/studio/projects/project-1/subject/approve', () => new HttpResponse(null, { status: 422 })),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" onApproved={onApproved} />, { wrapper: queryWrapper() })
  const otherCard = await screen.findByLabelText('Take 1')
  await user.click(within(otherCard).getByRole('button', { name: 'Use this subject' }))
  await screen.findByText('Check the file or settings and try again.')
  expect(onApproved).not.toHaveBeenCalled()
})

it('retries an approval with the fresh revision after a 409, without showing the notice, then navigates after the confirmation beat', async () => {
  const assessment = { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: currentRevision.sha256, width: 512, height: 512, occupancy_ratio: 0.9, findings: [] }
  const initial = projectWith({ assessment })
  let projectRevision = initial.revision
  let approveCalls = 0
  const expectedRevisions: number[] = []
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json({ ...initial, revision: projectRevision })),
    http.post('/studio/projects/project-1/subject/approve', async ({ request }) => {
      approveCalls += 1
      const body = (await request.json()) as { expected_revision: number }
      expectedRevisions.push(body.expected_revision)
      if (approveCalls === 1) {
        // Someone else advances the project between the first attempt and the retry -- the
        // refetch the retry triggers must observe this, not the number the card first rendered.
        projectRevision = 9
        return HttpResponse.json({ detail: { message: 'expected revision is stale', current_revision: 9 } }, { status: 409 })
      }
      return HttpResponse.json({ ...initial, revision: projectRevision, manifest: { ...initial.manifest, subject: { ...initial.manifest.subject, approval: { ...initial.manifest.subject.approval, state: 'approved', approver_id: 'actor-1' } } } })
    }),
  )
  const onApproved = vi.fn()
  const user = userEvent.setup()
  render(<Harness projectId="project-1" onApproved={onApproved} />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Approve subject' }))
  await screen.findByText(/Subject approved/)
  expect(approveCalls).toBe(2)
  expect(expectedRevisions).toEqual([initial.revision, 9])
  expect(screen.queryByText('This project changed elsewhere. Your view has been refreshed.')).not.toBeInTheDocument()
  // The hand-off to Motions is deliberately delayed ~800ms so the confirmation is seen.
  expect(onApproved).not.toHaveBeenCalled()
  await new Promise((resolve) => setTimeout(resolve, 850))
  expect(onApproved).toHaveBeenCalledTimes(1)
})

it('auto-approves even a finding carrying the legacy blocking severity', async () => {
  // Quality findings are warnings, never blockers (operator decision, 2026-09-05): there is no
  // "stops for review" path any more -- choosing a take always auto-approves it once it has a
  // settled assessment, whatever severity its findings carry.
  const blockingAssessment = { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: olderRevision.sha256, width: 512, height: 512, occupancy_ratio: 0.7, findings: [{ code: 'subject_missing_alpha' as const, severity: 'blocking' as const, title: 'Missing alpha', explanation: 'No transparent background', artifact_sha256: olderRevision.sha256, action_id: null }] }
  const selected: ProjectResponse = { ...projectFixture, revision: 5, manifest: { ...projectFixture.manifest, subject: { current_revision: { ...olderRevision, assessment: blockingAssessment }, revisions: [{ ...olderRevision, assessment: blockingAssessment }, currentRevision], assessment: blockingAssessment, approval: { state: 'ready_for_signoff', assessment_id: null, approver_id: null, policy_version: null, artifact_sha256: null, manifest_digest: null, waived_finding_ids: [], waiver_reason: null } } } }
  const approved: ProjectResponse = { ...selected, revision: 6, manifest: { ...selected.manifest, subject: { ...selected.manifest.subject, approval: { ...selected.manifest.subject.approval, state: 'waived', approver_id: 'actor-1', waived_finding_ids: ['subject_missing_alpha'], waiver_reason: 'acknowledged' } } } }
  let approveCalled = false
  const onApproved = vi.fn()
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(projectWith({}))),
    http.post('/studio/projects/project-1/subject/revisions/r1/select', () => HttpResponse.json(selected)),
    http.post('/studio/projects/project-1/subject/approve', () => { approveCalled = true; return HttpResponse.json(approved) }),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" onApproved={onApproved} />, { wrapper: queryWrapper() })
  const otherCard = await screen.findByLabelText('Take 1')
  await user.click(within(otherCard).getByRole('button', { name: 'Use this subject' }))
  await screen.findByText(/Subject approved/)
  expect(approveCalled).toBe(true)
  expect(onApproved).toHaveBeenCalledTimes(1)
})

function findingAssessment(code: 'subject_touches_canvas_edge' | 'subject_occupancy_too_low' | 'subject_occupancy_too_high' | 'subject_missing_alpha') {
  return { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: currentRevision.sha256, width: 512, height: 512, occupancy_ratio: 0.7, findings: [{ code, severity: 'warning' as const, title: 'Subject Occupancy Too Low', explanation: 'occupancy_ratio 0.21 is below min_occupancy_ratio 0.35.', artifact_sha256: currentRevision.sha256, action_id: null }] }
}

it('states a finding in plain language with the button that fixes it, never a finding code', async () => {
  const project = projectWith({ assessment: findingAssessment('subject_occupancy_too_low') })
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('The character is small in the frame.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Fit to frame' })).toBeInTheDocument()
  expect(screen.queryByText(/occupancy/i)).not.toBeInTheDocument()
  expect(screen.queryByText(/Heads up/)).not.toBeInTheDocument()
  expect(screen.queryByText(/blocked/i)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Approve subject' })).toBeEnabled()
})

it('maps a touching-edge finding to "Fit to frame" and stray fragments to "Clean up"', async () => {
  const edge = projectWith({ assessment: findingAssessment('subject_touches_canvas_edge') })
  const { unmount } = render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={edge} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('The character touches the edge of the frame.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Fit to frame' })).toBeInTheDocument()
  unmount()

  const debris = projectWith({ assessment: findingAssessment('subject_occupancy_too_high') })
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={debris} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('There are stray fragments around the character.')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Clean up' })).toBeInTheDocument()
})

it('"Fit to frame" calls the reframe route for the chosen take', async () => {
  const initial = projectWith({ assessment: findingAssessment('subject_occupancy_too_low') })
  let body: unknown
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(initial)),
    http.post('/studio/projects/project-1/subject/revisions/r2/reframe', async ({ request }) => { body = await request.json(); return HttpResponse.json({ ...initial, revision: 5 }) }),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Fit to frame' }))
  expect(body).toEqual({ expected_revision: 4, edit: 'reframe' })
})

it('prefills "Correct with feedback" with a human instruction, never the finding code', async () => {
  const project = projectWith({ assessment: findingAssessment('subject_missing_alpha') })
  const user = userEvent.setup()
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('The background has not been removed.')).toBeInTheDocument()
  await user.click(screen.getAllByRole('button', { name: 'Correct with feedback' })[0])
  expect(screen.getByLabelText('What should change?')).toHaveValue('Remove the background completely so only the character remains.')
})

it('has no "Note for the record" field, and approves without sending a reason', async () => {
  const initial = projectWith({ assessment: findingAssessment('subject_touches_canvas_edge') })
  const approved = { ...initial, manifest: { ...initial.manifest, subject: { ...initial.manifest.subject, approval: { ...initial.manifest.subject.approval, state: 'waived' as const } } } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(initial)),
    http.post('/studio/projects/project-1/subject/approve', async ({ request }) => {
      expect(await request.json()).toEqual({ expected_revision: 4 })
      return HttpResponse.json(approved)
    }),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('button', { name: 'Approve subject' })).toBeInTheDocument()
  expect(screen.queryByLabelText(/Note for the record/)).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Approve subject' }))
  await screen.findByText(/Subject approved/)
})

it('shows the "Approving…" spinner while approval is in flight', async () => {
  const assessment = { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: currentRevision.sha256, width: 512, height: 512, occupancy_ratio: 0.9, findings: [] }
  const project = projectWith({ assessment }, [])
  let resolveApprove: (() => void) | undefined
  server.use(
    http.post('/studio/projects/project-1/subject/approve', () => new Promise((resolve) => {
      resolveApprove = () => resolve(HttpResponse.json(project))
    })),
  )
  const user = userEvent.setup()
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Approve subject' }))
  expect(screen.getByRole('button', { name: 'Approving…' })).toBeDisabled()
  resolveApprove?.()
})

it('shows a red inline failure with Retry when approval is rejected outright', async () => {
  const assessment = { assessment_id: 'a1', policy_version: 'v1', artifact_sha256: currentRevision.sha256, width: 512, height: 512, occupancy_ratio: 0.9, findings: [] }
  const project = projectWith({ assessment }, [])
  server.use(
    http.post('/studio/projects/project-1/subject/approve', () => HttpResponse.json({ detail: 'subject approval service unavailable' }, { status: 503 })),
  )
  const user = userEvent.setup()
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Approve subject' }))
  expect(await screen.findByText('subject approval service unavailable')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
})

it('rejects the chosen take and falls back to the previous one', async () => {
  const initial = projectWith({})
  const afterReject: ProjectResponse = { ...initial, revision: 5, manifest: { ...initial.manifest, subject: { ...initial.manifest.subject, current_revision: olderRevision, revisions: [{ ...currentRevision, lifecycle: 'rejected' }, olderRevision] } } }
  const rejectBodies: { expected_revision: number }[] = []
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(initial)),
    http.post('/studio/projects/project-1/subject/revisions/r2/reject', async ({ request }) => { rejectBodies.push((await request.json()) as { expected_revision: number }); return HttpResponse.json(afterReject) }),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" />, { wrapper: queryWrapper() })
  const chosen = await screen.findByLabelText('Take 2, chosen')
  await user.click(within(chosen).getByRole('button', { name: 'Reject' }))
  expect(await screen.findByLabelText('Take 1, chosen')).toBeInTheDocument()
  expect(rejectBodies).toEqual([{ expected_revision: initial.revision }])
  // The rejected take leaves the choosable set for the collapsed group, with no Choose action.
  expect(screen.queryByLabelText('Take 2')).not.toBeInTheDocument()
  const group = await screen.findByLabelText('Rejected subject takes')
  expect(screen.getByText('Rejected takes (1)')).toBeInTheDocument()
  expect(within(group).getByLabelText('Take 2, rejected')).toBeInTheDocument()
  expect(within(group).queryByRole('button', { name: 'Use this subject' })).not.toBeInTheDocument()
})

it('shows the empty state when every take has been rejected', async () => {
  const project = projectWith({ current_revision: null, revisions: [{ ...currentRevision, lifecycle: 'rejected' }, { ...olderRevision, lifecycle: 'rejected' }] })
  render(<SubjectTakesGallery correcting={false} elapsedSeconds={null} activeGeneration={false} onApproved={vi.fn()} onCorrectSubmit={vi.fn()} project={project} projectId="project-1" />, { wrapper: queryWrapper() })
  expect(await screen.findByText('No subject yet · generate one to continue')).toBeInTheDocument()
  expect(screen.getByText('Rejected takes (2)')).toBeInTheDocument()
  expect(screen.queryByText('Choose one to continue')).not.toBeInTheDocument()
})

it('restores a rejected take back into the choosable set', async () => {
  const initial = projectWith({ revisions: [currentRevision, { ...olderRevision, lifecycle: 'rejected' }] })
  const afterRestore: ProjectResponse = { ...initial, revision: 5, manifest: { ...initial.manifest, subject: { ...initial.manifest.subject, revisions: [currentRevision, olderRevision] } } }
  let restoreCalled = false
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json(initial)),
    http.post('/studio/projects/project-1/subject/revisions/r1/restore', () => { restoreCalled = true; return HttpResponse.json(afterRestore) }),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" />, { wrapper: queryWrapper() })
  const rejectedCard = await screen.findByLabelText('Take 1, rejected')
  await user.click(within(rejectedCard).getByRole('button', { name: 'Restore' }))
  expect(await screen.findByLabelText('Take 1')).toBeInTheDocument()
  expect(restoreCalled).toBe(true)
  expect(screen.queryByText(/Rejected takes/)).not.toBeInTheDocument()
  // Chosen selection is untouched by a restore.
  expect(screen.getByLabelText('Take 2, chosen')).toBeInTheDocument()
})

it('retries a reject with the fresh revision after a 409', async () => {
  const initial = projectWith({})
  let projectRevision = initial.revision
  const expectedRevisions: number[] = []
  const afterReject: ProjectResponse = { ...initial, revision: 10, manifest: { ...initial.manifest, subject: { ...initial.manifest.subject, current_revision: olderRevision, revisions: [{ ...currentRevision, lifecycle: 'rejected' }, olderRevision] } } }
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json({ ...initial, revision: projectRevision })),
    http.post('/studio/projects/project-1/subject/revisions/r2/reject', async ({ request }) => {
      const body = (await request.json()) as { expected_revision: number }
      expectedRevisions.push(body.expected_revision)
      if (expectedRevisions.length === 1) { projectRevision = 9; return HttpResponse.json({ detail: { message: 'expected revision is stale', current_revision: 9 } }, { status: 409 }) }
      return HttpResponse.json(afterReject)
    }),
  )
  const user = userEvent.setup()
  render(<Harness projectId="project-1" />, { wrapper: queryWrapper() })
  const chosen = await screen.findByLabelText('Take 2, chosen')
  await user.click(within(chosen).getByRole('button', { name: 'Reject' }))
  expect(await screen.findByLabelText('Take 1, chosen')).toBeInTheDocument()
  expect(expectedRevisions).toEqual([initial.revision, 9])
})

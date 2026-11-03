import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { projectFixture } from '../../test/studioTestUtils'
import { SubjectWorkspace } from '../subject/SubjectWorkspace'
import { LiveProjectRoute } from './ProjectRoute'

const readyForApproval = {
  ...projectFixture,
  manifest: {
    ...projectFixture.manifest,
    subject: {
      current_revision: { revision_id: 'subject-r1', artifact_key: 'outputs/studio/project-1/subjects/job.png', sha256: 'a'.repeat(64), width: 512, height: 512, lifecycle: 'ready' as const, parent_revision_ids: [] },
      assessment: { assessment_id: 'assessment-1', policy_version: 'subject-v1', artifact_sha256: 'a'.repeat(64), width: 512, height: 512, occupancy_ratio: 0.72, findings: [] },
      approval: { ...projectFixture.manifest.subject.approval, state: 'ready_for_signoff' as const },
    },
  },
}
let releaseApprove: (value: unknown) => void = () => undefined
const server = setupServer(
  http.get('/studio/projects/project-1', () => HttpResponse.json(readyForApproval)),
  http.get('/studio/projects/project-1/releases', () => HttpResponse.json([])),
  http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
  http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: [], next_cursor: null })),
  http.post('/studio/projects/project-1/activity/reconcile', () => HttpResponse.json({ operations: [], next_cursor: null })),
  http.post('/studio/projects/project-1/assets/preview', () => HttpResponse.json({ key: 'out', upload_url: 'https://assets.invalid/subject.png' })),
  http.post('/studio/projects/project-1/subject/approve', () => new Promise((resolve) => { releaseApprove = () => resolve(HttpResponse.json({ ...readyForApproval, revision: 5 })) })),
)
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function renderLiveRoute() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/projects/project-1/subject']}>
        <Routes>
          <Route path="/projects/:projectId" element={<LiveProjectRoute />}>
            <Route path="subject" element={<SubjectWorkspace projectId="project-1" />} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

it('shows an honest save pill that reflects an in-flight mutation', async () => {
  const user = userEvent.setup()
  renderLiveRoute()
  expect(await screen.findByText('Saved')).toBeInTheDocument()

  await user.click(await screen.findByRole('button', { name: 'Approve subject' }))
  expect(await screen.findByText('Saving')).toBeInTheDocument()

  releaseApprove(undefined)
  await waitFor(() => expect(screen.getByText('Saved')).toBeInTheDocument())
})

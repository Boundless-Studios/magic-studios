import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import type { ProjectSummary } from '../../api/types'
import { ProjectHome } from './ProjectHome'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const readable: ProjectSummary = {
  project_id: 'project-1', name: 'Ember Knight', revision: 1,
  created_at: '2026-01-01T09:00:00Z', updated_at: '2026-01-01T09:00:00Z', created_by: 'owner-1',
  subject_state: 'approved', motion_count: 2, release_count: 1, external_subject: null,
  activity: { running: 0, failed: 0, completed: 0, last_activity_at: null },
  unreadable: false,
}
const broken: ProjectSummary = {
  project_id: 'project-2', name: 'Broken raw name', revision: 4,
  created_at: '2026-01-01T09:00:00Z', updated_at: '2026-01-02T09:00:00Z', created_by: 'owner-1',
  subject_state: 'none', motion_count: 0, release_count: 0, external_subject: null,
  activity: { running: 0, failed: 0, completed: 0, last_activity_at: null },
  unreadable: true,
}

function renderHome(projects: ProjectSummary[], onRepaired = vi.fn()) {
  return { onRepaired, ...render(
    <MemoryRouter>
      <ProjectHome projects={projects} onNewProject={vi.fn()} onRepaired={onRepaired} />
    </MemoryRouter>,
  ) }
}

it('renders an unreadable project as a repair card instead of a link', () => {
  renderHome([readable, broken])

  expect(screen.getByRole('link', { name: /Ember Knight/ })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: /Broken raw name/ })).not.toBeInTheDocument()
  expect(screen.getByText('Broken raw name')).toBeInTheDocument()
  expect(screen.getByText('Needs repair: the last saved version could not be read')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Repair' })).toBeInTheDocument()
})

it('repairs the project and refetches the list on success', async () => {
  const user = userEvent.setup()
  let repairCalled = false
  server.use(
    http.post('/studio/projects/project-2/repair', () => {
      repairCalled = true
      return HttpResponse.json({
        project_id: 'project-2', current_revision_before: 4, restored_from_revision: 3,
        new_revision: 5, repaired: true, bad_revisions: [],
      })
    }),
  )
  const { onRepaired } = renderHome([broken])

  await user.click(screen.getByRole('button', { name: 'Repair' }))

  expect(await screen.findByText('Restored to revision 5')).toBeInTheDocument()
  expect(repairCalled).toBe(true)
  expect(onRepaired).toHaveBeenCalledTimes(1)
})

it('shows an inline error when repair fails', async () => {
  const user = userEvent.setup()
  server.use(
    http.post('/studio/projects/project-2/repair', () => HttpResponse.json({ detail: 'boom' }, { status: 500 })),
  )
  const { onRepaired } = renderHome([broken])

  await user.click(screen.getByRole('button', { name: 'Repair' }))

  expect(await screen.findByText('The project could not be repaired. Try again.')).toBeInTheDocument()
  expect(onRepaired).not.toHaveBeenCalled()
})

it('warns that unopened work is about to be deleted for inactivity, and only then', () => {
  renderHome([{ ...readable, release_count: 0, expires_at: '2026-04-01T09:00:00Z' }, { ...readable, project_id: 'project-3', name: 'Kept' }])

  expect(screen.getAllByText(/Deleted on .* unless you open it/)).toHaveLength(1)
  expect(screen.getByRole('link', { name: /Ember Knight/ })).toHaveTextContent(/Deleted on .*2026 unless you open it/)
})

it('warns on an unreadable project too, where repairing is what keeps it', () => {
  renderHome([{ ...broken, expires_at: '2026-04-01T09:00:00Z' }])

  expect(screen.getByText(/Deleted on .*2026 unless you repair it/)).toBeInTheDocument()
})

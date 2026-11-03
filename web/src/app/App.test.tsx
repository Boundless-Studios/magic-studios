import * as Sentry from '@sentry/react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { RouteFallback, StudioErrorBoundary, StudioRoutes, StudioUnavailable } from './App'
import type { ProjectRepository } from '../features/project/projectRepository'
import { previewProjectRepository } from '../features/project/projectRepository'
import { SentryErrorBoundary } from '../monitoring'
import { projectFixture } from '../test/studioTestUtils'

const TEST_TARGET = {
  system: 'acme', display_name: 'Acme', entity_kinds: ['character', 'journey_traveller'], delivery_configured: true,
  slots: [
    { name: 'idle', accepts: ['idle'], winner: 'idle' },
    { name: 'move', accepts: ['walk'], winner: 'walk' },
    { name: 'attack', accepts: ['attack'], winner: 'attack' },
    { name: 'trip', accepts: ['hurt', 'trip'], winner: 'trip' },
    { name: 'guard', accepts: ['guard'], winner: 'guard' },
    { name: 'cast', accepts: ['cast'], winner: 'cast' },
  ],
}
const repository: ProjectRepository = {
  async getById(projectId) {
    if (projectId === 'project-2') {
      return { id: 'project-2', name: 'Moon Ranger', saveState: 'Saved' }
    }
    return null
  },
}
const server = setupServer(
  http.get('/pool/workers', () => HttpResponse.json({ workers: [] })),
  http.get('/studio/projects/:projectId/operations', () => HttpResponse.json([])),
  http.get('/studio/projects/:projectId/activity', () => HttpResponse.json({ operations: [], next_cursor: null })),
  http.get('/studio/projects/:projectId/history', () => HttpResponse.json([])),
  http.get('/studio/targets', () => HttpResponse.json([])),
http.get('/studio/generation-config', () => HttpResponse.json({ settings: null })), http.get('/studio/projects', () => HttpResponse.json([])), http.get('/studio/movies', () => HttpResponse.json([])))
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function renderRoute(path: string, source: ProjectRepository = repository) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <StudioRoutes repository={source} />
    </MemoryRouter>,
  )
}

describe('Studio routes', () => {
  it('resumes a movie-only account from the project home', async () => {
    server.use(http.get('/studio/movies', () => HttpResponse.json([{ movie_id: 'movie-existing', name: 'Existing film', shots: [], releases: [], compositions: {}, assembly: null }])))
    renderRoute('/')
    expect(await screen.findByRole('link', { name: /Existing film/ })).toHaveAttribute('href', '/movies/movie-existing/story')
    expect(screen.getByRole('button', { name: 'New project' })).toBeVisible()
  })

  it('creates a Movie project and opens its own story workspace', async () => {
    const user = userEvent.setup()
    const movie = { movie_id: 'movie-new', name: 'Last light', owner_id: 'owner', revision: 1, brief: '', style: '', batch_size: 2, shots: [], cut: [], assembly: null, releases: [] }
    server.use(http.get('/studio/movies', () => HttpResponse.json([])), http.post('/studio/movies', async ({ request }) => {
      expect(await request.json()).toEqual({ name: 'Last light' })
      return HttpResponse.json(movie)
    }), http.get('/studio/movies/movie-new', () => HttpResponse.json(movie)), http.get('/studio/movies/capabilities', () => HttpResponse.json({ generation_available: true, composition_available: false, max_duration_seconds: 10, max_batch_size: 6 })))
    renderRoute('/')
    await user.click(await screen.findByRole('button', { name: 'Make a movie instead' }))
    await user.type(screen.getByLabelText('Project name'), 'Last light')
    await user.click(screen.getByRole('button', { name: 'Create project' }))
    expect(await screen.findByRole('heading', { name: 'Movie' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Movie' })).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Motions' })).not.toBeInTheDocument()
  })

  it('starts with artwork before creating a workspace', async () => {
    renderRoute('/')
    expect(await screen.findByRole('heading', { name: 'Upload your character' })).toBeVisible()
    expect(screen.getByLabelText('Character / project name')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Create project' })).not.toBeInTheDocument()
  })

  it('shows the home list instead of onboarding when the caller already has projects', async () => {
    const summary = {
      project_id: 'project-9', name: 'Ember Knight', revision: 3,
      created_at: '2026-01-01T09:00:00Z', updated_at: '2026-01-02T15:42:00Z', created_by: 'owner-1',
      subject_state: 'approved', motion_count: 2, release_count: 1, external_subject: null,
      activity: { running: 0, failed: 1, completed: 4, last_activity_at: '2026-01-02T15:42:00Z' },
    }
    server.use(http.get('/studio/projects', () => HttpResponse.json([summary])))
    renderRoute('/')

    expect(await screen.findByRole('heading', { name: 'Your projects' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Ember Knight/ })).toHaveAttribute('href', '/projects/project-9/motions')
    expect(screen.queryByLabelText('Project name')).not.toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'New project' }))
    expect(await screen.findByLabelText('Character / project name')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
  })

  it('loads the project named by the route parameter', async () => {
    renderRoute('/projects/project-2/history')

    expect(await screen.findByRole('heading', { name: 'Moon Ranger', level: 1 })).toBeInTheDocument()
    expect(screen.queryByText('Ember Knight')).not.toBeInTheDocument()
  })

  it('shows a polished not-found state for an unknown project', async () => {
    renderRoute('/projects/missing/subject')

    expect(await screen.findByRole('heading', { name: 'Project not found' })).toBeInTheDocument()
    expect(screen.getByText(/may have been moved or you may not have access/i)).toBeInTheDocument()
  })

  it('opens an already-bound project on a repeat deep link, selecting the requested motion (Part A)', async () => {
    const bound = {
      ...projectFixture,
      project_id: 'project-3',
      manifest: {
        ...projectFixture.manifest,
        project_id: 'project-3',
        external_subject: { system: 'acme' as const, entity_kind: 'character' as const, entity_id: 'char-3', display_name: 'Ember Knight' },
        motions: [{ motion_id: 'walk', name: 'Walk', kind: 'preset' as const, semantic_slot: 'walk', loop_policy: 'loop' as const, required: true, candidates: [], selected_candidate_id: null }],
      },
    }
    server.use(
      http.get('/studio/targets', () => HttpResponse.json([TEST_TARGET])),
      http.get('/studio/projects/for-external-subject', ({ request }) => {
        const url = new URL(request.url)
        expect(url.searchParams.get('system')).toBe('acme')
        expect(url.searchParams.get('entity_kind')).toBe('character')
        expect(url.searchParams.get('entity_id')).toBe('char-3')
        return HttpResponse.json(bound)
      }),
      http.get('/studio/projects/project-3', () => HttpResponse.json(bound)),
      http.get('/studio/projects/project-3/releases', () => HttpResponse.json([])),
      http.get('/studio/projects/project-3/operations', () => HttpResponse.json([])),
    )

    render(
      <MemoryRouter initialEntries={['/new?target=acme&entity_kind=character&entity_id=char-3&name=Ember%20Knight&slot=move']}>
        <StudioRoutes repository={previewProjectRepository} />
      </MemoryRouter>,
    )

    const selectedWalk = await within(await screen.findByRole('listbox', { name: 'Motions' })).findByRole('option', { selected: true })
    expect(selectedWalk).toHaveTextContent('Walk')
    expect(await screen.findByText('Bound to Acme character Ember Knight')).toBeInTheDocument()
  })

  it('creates a project bound to a target subject and opens Motions when no project exists yet (Part A)', async () => {
    const created = { project_id: 'project-4' }
    const withMotion = {
      ...projectFixture,
      project_id: 'project-4',
      manifest: {
        ...projectFixture.manifest,
        project_id: 'project-4',
        name: 'Moon Ranger',
        external_subject: { system: 'acme' as const, entity_kind: 'character' as const, entity_id: 'char-4', display_name: 'Moon Ranger' },
        motions: [{ motion_id: 'idle', name: 'Idle', kind: 'preset' as const, semantic_slot: 'idle', loop_policy: 'loop' as const, required: true, candidates: [], selected_candidate_id: null }],
      },
    }
    server.use(
      http.get('/studio/targets', () => HttpResponse.json([TEST_TARGET])),
      http.get('/studio/projects/for-external-subject', () => HttpResponse.json({ detail: 'not found' }, { status: 404 })),
      http.post('/studio/projects', async ({ request }) => {
        expect(await request.json()).toMatchObject({
          name: 'Moon Ranger',
          external_subject: { system: 'acme', entity_kind: 'character', entity_id: 'char-4', display_name: 'Moon Ranger' },
          preset_motions: ['idle'],
        })
        return HttpResponse.json(created)
      }),
      http.get('/studio/projects/project-4', () => HttpResponse.json(withMotion)),
      http.get('/studio/projects/project-4/releases', () => HttpResponse.json([])),
      http.get('/studio/projects/project-4/operations', () => HttpResponse.json([])),
    )

    render(
      <MemoryRouter initialEntries={['/new?target=acme&entity_kind=character&entity_id=char-4&name=Moon%20Ranger&slot=idle']}>
        <StudioRoutes repository={previewProjectRepository} />
      </MemoryRouter>,
    )

    const selectedIdle = await within(await screen.findByRole('listbox', { name: 'Motions' })).findByRole('option', { selected: true })
    expect(selectedIdle).toHaveTextContent('Idle')
    expect(await screen.findByText('Bound to Acme character Moon Ranger')).toBeInTheDocument()
  })

  it('shows a not-found state for an unknown route', async () => {
    renderRoute('/somewhere-else')

    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
  })

  it('shows a route error state when project loading fails', async () => {
    const failingRepository: ProjectRepository = {
      async getById() {
        throw new Error('offline')
      },
    }
    renderRoute('/projects/project-2/subject', failingRepository)

    expect(await screen.findByRole('heading', { name: 'Studio unavailable' })).toBeInTheDocument()
    expect(screen.getByText(/could not load this project/i)).toBeInTheDocument()
  })

  it('provides a restrained route-loading fallback', () => {
    render(<RouteFallback />)

    expect(screen.getByRole('status')).toHaveTextContent('Opening studio')
  })

  it('shows a polished recovery state when a lazy route fails to render', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    function BrokenRoute(): never {
      throw new Error('chunk failed')
    }

    render(
      <StudioErrorBoundary>
        <BrokenRoute />
      </StudioErrorBoundary>,
    )

    expect(screen.getByRole('heading', { name: 'Studio unavailable' })).toBeInTheDocument()
    expect(screen.getByText(/could not open this part of the studio/i)).toBeInTheDocument()
    consoleError.mockRestore()
  })

  it('reports to Sentry and shows the Studio-unavailable fallback for an app-level crash', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const sentEnvelopes: unknown[] = []
    Sentry.init({
      dsn: 'https://abc@example.ingest.sentry.io/123',
      transport: () => ({
        send: (envelope) => { sentEnvelopes.push(envelope); return Promise.resolve({}) },
        flush: () => Promise.resolve(true),
      }),
    })
    function BrokenApp(): never {
      throw new Error('app crash')
    }

    render(
      <SentryErrorBoundary fallback={<StudioUnavailable />}>
        <BrokenApp />
      </SentryErrorBoundary>,
    )

    expect(screen.getByRole('heading', { name: 'Studio unavailable' })).toBeInTheDocument()
    await vi.waitFor(() => expect(sentEnvelopes.length).toBeGreaterThan(0))
    Sentry.getClient()?.close()
    consoleError.mockRestore()
  })
})

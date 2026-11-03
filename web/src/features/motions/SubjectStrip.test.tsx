import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import type { SubjectRevision } from '../../api/types'
import { SubjectStrip } from './SubjectStrip'

const subjectRevision: SubjectRevision = {
  revision_id: 'rev-1', artifact_key: 'subjects/project-1/rev-1.png', sha256: 'x', width: 512, height: 512,
  lifecycle: 'ready', parent_revision_ids: [], source: 'generated', direction: null, created_at: null,
}

const server = setupServer()
beforeAll(() => server.listen()); afterEach(() => server.resetHandlers()); afterAll(() => server.close())

function renderStrip(props: Partial<Parameters<typeof SubjectStrip>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <SubjectStrip projectId="project-1" subjectRevision={null} externalSubject={null} projectName="Pequod" {...props} />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

it('renders the chosen subject thumbnail via the shared preview mechanism, with a checkerboard background', async () => {
  server.use(http.post('/studio/projects/project-1/assets/preview', async ({ request }) => {
    const body = await request.json() as { asset_key: string }
    return HttpResponse.json({ key: body.asset_key, upload_url: `https://assets.invalid/${body.asset_key}` })
  }))
  renderStrip({ subjectRevision })
  const img = await screen.findByAltText('Pequod thumbnail')
  expect(img).toHaveAttribute('src', 'https://assets.invalid/subjects/project-1/rev-1.png')
  expect(img.parentElement).toHaveClass('checkerboard')
  expect(screen.queryByText('P')).not.toBeInTheDocument()
})

it('falls back to the initial while the preview is loading', () => {
  server.use(http.post('/studio/projects/project-1/assets/preview', () => new Promise(() => { /* never resolves */ })))
  renderStrip({ subjectRevision })
  expect(screen.getByText('P')).toBeInTheDocument()
  expect(screen.queryByAltText('Pequod thumbnail')).not.toBeInTheDocument()
})

it('falls back to the initial when there is no subject yet', () => {
  renderStrip({ subjectRevision: null })
  expect(screen.getByText('P')).toBeInTheDocument()
})

it('falls back to the initial (without getting stuck) when the preview request fails', async () => {
  server.use(http.post('/studio/projects/project-1/assets/preview', () => HttpResponse.json({ detail: 'not found' }, { status: 404 })))
  renderStrip({ subjectRevision })
  expect(await screen.findByText('P')).toBeInTheDocument()
  expect(screen.queryByAltText('Pequod thumbnail')).not.toBeInTheDocument()
})

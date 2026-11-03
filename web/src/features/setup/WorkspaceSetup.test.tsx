import { render, screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import type { ProjectResponse } from '../../api/types'
import { projectFixture, queryWrapper } from '../../test/studioTestUtils'
import { WorkspaceSetup } from './WorkspaceSetup'

const reference = { revision_id: 'rev-s', key: 'sources/s.png', sha256: 'a'.repeat(64), width: 512, height: 512 }
const project: ProjectResponse = { ...projectFixture, manifest: { ...projectFixture.manifest, sprite_layout: 'isometric', views: [{ view_id: 'S', heading: 'S', name: 'Front', reference }] } }
const server = setupServer(
  http.get('/studio/generation-config', () => HttpResponse.json({ workers: [] })),
  http.get('/studio/projects/project-1/turnarounds', () => HttpResponse.json({ turnarounds: [] })),
  http.get('/studio/projects/project-1/view-drafts', () => HttpResponse.json({ drafts: [] })),
)
beforeAll(() => server.listen()); afterEach(() => { server.resetHandlers(); window.history.replaceState(null, '', '/') }); afterAll(() => server.close())

it("opens the turnaround panel when onboarding's Generate the other directions sent the person here", async () => {
  window.history.replaceState(null, '', '/projects/project-1/motions?directions=generate')
  render(<WorkspaceSetup project={project} selectedViewId={null} onSelect={() => {}} onBusyChange={() => {}} />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('dialog', { name: 'Generate other directions' })).toBeVisible()
  // The request is consumed: a reload does not reopen it.
  expect(window.location.search).toBe('')
})

it('stays closed on an ordinary visit', async () => {
  window.history.replaceState(null, '', '/projects/project-1/motions')
  render(<WorkspaceSetup project={project} selectedViewId={null} onSelect={() => {}} onBusyChange={() => {}} />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('button', { name: 'Generate other directions' })).toBeVisible()
  expect(screen.queryByRole('dialog', { name: 'Generate other directions' })).not.toBeInTheDocument()
})

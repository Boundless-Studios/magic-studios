import { render, screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { axe } from 'vitest-axe'
import { MotionWorkspace } from './motions/MotionWorkspace'
import { ReleaseWorkspace } from './release/ReleaseWorkspace'
import { SubjectWorkspace } from './subject/SubjectWorkspace'
import { projectFixture, queryWrapper } from '../test/studioTestUtils'

const server = setupServer(
  http.get('/studio/projects/project-1', () => HttpResponse.json(projectFixture)),
  http.get('/studio/projects/project-1/operations', () => HttpResponse.json([])),
  http.get('/studio/projects/project-1/releases', () => HttpResponse.json([])),
)
beforeAll(() => server.listen()); afterEach(() => server.resetHandlers()); afterAll(() => server.close())

for (const [name, Workspace, heading] of [
  ['subject', SubjectWorkspace, 'Create the character to animate'],
  ['motions', MotionWorkspace, 'Build the animation set'],
  ['release', ReleaseWorkspace, 'Approve the set and ship it'],
] as const) {
  it(`${name} workspace has no axe violations`, async () => {
    const view = render(<Workspace projectId="project-1" />, { wrapper: queryWrapper() })
    await screen.findByRole('heading', { name: heading })
    const result = await axe(view.container, { rules: { 'color-contrast': { enabled: false } } })
    expect(result.violations.filter((violation) => violation.impact === 'serious' || violation.impact === 'critical')).toEqual([])
  })
}

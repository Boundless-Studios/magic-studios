import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { queryWrapper } from '../../test/studioTestUtils'
import { ProjectHistory } from './ProjectHistory'

const entries = [
  { revision: 1, timestamp: '2026-08-31T00:00:00Z', actor: 'user-1', summary: 'Created the project', failed: false },
  { revision: 2, timestamp: '2026-08-31T00:05:00Z', actor: 'user-1', summary: 'Approved the subject', failed: false },
  { revision: null, timestamp: '2026-08-31T00:06:00Z', actor: 'user-1', summary: 'Generation failed before a result was created.', failed: true },
]
const firstOperationPage = {
  operations: [
    { job_id: 'job-1', operation: 'generate_motion', status: 'completed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'settled', failure_code: null, failure_explanation: null, retryable: false, generation_settings: { preset: 'high', steps: 28, seed: 123, width: 768, height: 768, duration_seconds: 6, provider_parameters: {} }, prompt: 'subtle breathing idle' },
    { job_id: 'job-2', operation: 'generate_motion', status: 'failed', motion_id: 'walk', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Generation was interrupted by a temporary service problem. Retry this motion.', retryable: true, generation_settings: { preset: 'standard', steps: 20, seed: 42, width: 512, height: 512, duration_seconds: 4, provider_parameters: {} }, prompt: 'steady forward walk' },
  ],
  next_cursor: 'cursor-2',
}
const secondOperationPage = {
  operations: [
    { job_id: 'job-3', operation: 'generate_motion', status: 'processing', motion_id: 'attack', output_key: 'out', progress_percent: 40, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false, generation_settings: { preset: 'quick', steps: 16, seed: 7, width: 864, height: 480, duration_seconds: 4, provider_parameters: {} }, prompt: 'one decisive weapon swing' },
  ],
  next_cursor: null,
}
const server = setupServer(
  http.get('/studio/projects/project-1/history', () => HttpResponse.json(entries)),
  http.get('/studio/projects/project-1/activity', ({ request }) => {
    const cursor = new URL(request.url).searchParams.get('cursor')
    return HttpResponse.json(cursor === 'cursor-2' ? secondOperationPage : firstOperationPage)
  }),
)
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

it('lists history newest first with a one-line summary and marks failures', async () => {
  render(<ProjectHistory projectId="project-1" />, { wrapper: queryWrapper() })

  const items = await screen.findAllByRole('listitem')
  expect(items).toHaveLength(3)
  expect(items[0]).toHaveTextContent('Generation failed before a result was created.')
  expect(items[1]).toHaveTextContent('Approved the subject')
  expect(items[1]).toHaveTextContent('Revision 2')
  expect(items[2]).toHaveTextContent('Created the project')
  expect(items[0]).toHaveTextContent('Failed')
})

it('shows an empty state before any revision exists', async () => {
  server.use(http.get('/studio/projects/project-1/history', () => HttpResponse.json([])))
  render(<ProjectHistory projectId="project-1" />, { wrapper: queryWrapper() })

  expect(await screen.findByText('No history yet.')).toBeInTheDocument()
})

it('lists operations with settings, status, and failure text, and loads more (T14)', async () => {
  const user = userEvent.setup()
  render(<ProjectHistory projectId="project-1" />, { wrapper: queryWrapper() })

  await user.click(await screen.findByRole('tab', { name: 'Operations' }))

  expect(await screen.findByText('high · 28 steps · seed 123 · 768×768 · 6s')).toBeInTheDocument()
  expect(screen.getByText('Generation was interrupted by a temporary service problem. Retry this motion.')).toBeInTheDocument()
  expect(screen.queryByText('quick · 16 steps · seed 7 · 864×480 · 4s')).not.toBeInTheDocument()

  await user.click(screen.getByRole('button', { name: 'Load more' }))

  expect(await screen.findByText('quick · 16 steps · seed 7 · 864×480 · 4s')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
})

it('navigates to Motions with a seeded settings draft from "Regenerate with these settings" (T14)', async () => {
  const user = userEvent.setup()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/projects/project-1/history']}>
        <Routes>
          <Route path="/projects/:projectId/history" element={<ProjectHistory projectId="project-1" />} />
          <Route path="/projects/:projectId/motions" element={<p>Motions workspace</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )

  await user.click(await screen.findByRole('tab', { name: 'Operations' }))
  const walkRow = (await screen.findByText('standard · 20 steps · seed 42 · 512×512 · 4s')).closest('li')
  expect(walkRow).not.toBeNull()
  await user.click(within(walkRow as HTMLElement).getByRole('button', { name: 'Regenerate with these settings' }))

  expect(await screen.findByText('Motions workspace')).toBeInTheDocument()
})

it('labels a turnaround in the operations list as a turnaround, not Subject', async () => {
  server.use(http.get('/studio/projects/project-1/activity', () => HttpResponse.json({ operations: [
    { job_id: 'job-turn', operation: 'generate_turnaround', status: 'completed', motion_id: null, turnaround_id: 'turnaround-abc', output_key: 'turn.mp4', progress_percent: null, stage: 'settled', failure_code: null, failure_explanation: null, retryable: false, generation_settings: null },
    { job_id: 'job-subject', operation: 'generate_subject', status: 'completed', motion_id: null, output_key: 'subject.png', progress_percent: null, stage: 'settled', failure_code: null, failure_explanation: null, retryable: false, generation_settings: null },
  ], next_cursor: null })))
  const user = userEvent.setup()
  render(<ProjectHistory projectId="project-1" />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('tab', { name: 'Operations' }))
  const list = await screen.findByRole('list', { name: 'Project operations' })
  const [turnaround, subject] = within(list).getAllByRole('listitem')
  expect(within(turnaround).getByText('Turnaround')).toBeInTheDocument()
  expect(within(subject).getByText('Subject')).toBeInTheDocument()
})

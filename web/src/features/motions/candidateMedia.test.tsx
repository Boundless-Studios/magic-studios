import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import userEvent from '@testing-library/user-event'
import { render, screen } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import type { MotionCandidate } from '../../api/types'
import { CandidateDownloadMenu, CandidateMedia } from './candidateMedia'

const candidate: MotionCandidate = {
  candidate_id: 'idle-a', artifact_key: 'idle-a.mp4', sha256: 'a', duration_ms: 1200, metadata: null,
  assessment: null, ordered_frames: null, frame_duration_ms: null, loop_policy: null, generation_settings: null,
  facing: 'right', recipe: null, source: 'generated',
}

const server = setupServer()
beforeAll(() => server.listen()); afterEach(() => server.resetHandlers()); afterAll(() => server.close())

function renderMedia() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><CandidateMedia projectId="project-1" candidate={candidate} name="Idle take 1" /></QueryClientProvider>)
}

it('shows a quiet "Preview unavailable" without waiting through retries when the preview request fails', async () => {
  let requestCount = 0
  server.use(http.post('/studio/projects/project-1/assets/preview', () => { requestCount += 1; return HttpResponse.json({ detail: 'not found' }, { status: 404 }) }))
  renderMedia()
  expect(screen.getByText('Loading preview')).toBeInTheDocument()
  expect(await screen.findByText('Preview unavailable')).toBeInTheDocument()
  // No retry: exactly one request, not the default retry-with-backoff's three.
  expect(requestCount).toBe(1)
})

it('requests the preview once per artifact key even when mounted more than once', async () => {
  let requestCount = 0
  server.use(http.post('/studio/projects/project-1/assets/preview', async ({ request }) => {
    requestCount += 1
    const body = await request.json() as { asset_key: string }
    return HttpResponse.json({ key: body.asset_key, upload_url: `https://assets.invalid/${body.asset_key}` })
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <CandidateMedia projectId="project-1" candidate={candidate} name="Idle take 1" />
    <CandidateMedia projectId="project-1" candidate={candidate} name="Idle take 1 (rail)" compact />
  </QueryClientProvider>)
  await screen.findAllByLabelText('Idle take 1 playback')
  expect(requestCount).toBe(1)
})


it('keeps preparation failures retryable and restores focus when the chooser closes', async () => {
  const user = userEvent.setup()
  server.use(http.get('/studio/projects/project-1/motions/idle/candidates/idle-a/sheet.png', () => new HttpResponse(null, { status: 503 })))
  render(<CandidateDownloadMenu projectId="project-1" motionId="idle" candidateId="idle-a" />)
  await user.click(screen.getByRole('button', { name: 'Download' }))
  await user.click(screen.getByRole('button', { name: 'PNG' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('temporarily unavailable')
  expect(screen.getByRole('button', { name: 'PNG' })).toBeEnabled()
  await user.keyboard('{Escape}')
  expect(screen.getByRole('button', { name: 'Download' })).toHaveFocus()
  expect(screen.queryByRole('group', { name: 'Download format' })).not.toBeInTheDocument()
})

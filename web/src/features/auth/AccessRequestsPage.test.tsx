import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'

import { AccessRequestsPage } from './AccessRequestsPage'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><AccessRequestsPage /></MemoryRouter></QueryClientProvider>)
}

describe('Past access requests (admin)', () => {
  it('lists past requests and invitations, dismisses either, and offers no way to invite', async () => {
    const user = userEvent.setup()
    let invited = false
    let rows: Array<Record<string, unknown>> = [
      { email: 'ada@example.com', first_requested_at: '2026-10-20T10:00:00Z', last_requested_at: '2026-10-21T10:00:00Z', request_count: 2, invited_at: null },
      { email: 'bo@example.com', first_requested_at: '2026-10-21T09:00:00Z', last_requested_at: '2026-10-21T09:00:00Z', request_count: 0, invited_at: '2026-10-21T12:00:00Z', invited_by: 'admin@example.com' },
    ]
    server.use(
      http.get('/studio/access-requests', ({ request }) => {
        const wantInvited = new URL(request.url).searchParams.get('status') === 'invited'
        const items = rows.filter((row) => Boolean(row.invited_at) === wantInvited)
        return HttpResponse.json({ items, total: items.length })
      }),
      http.post('/studio/invitations', () => { invited = true; return HttpResponse.json({}, { status: 410 }) }),
      http.delete('/studio/access-requests', ({ request }) => { const email = new URL(request.url).searchParams.get('email'); rows = rows.filter((row) => row.email !== email); return new HttpResponse(null, { status: 204 }) }),
    )
    renderPage()

    expect(await screen.findByRole('heading', { name: 'Past access requests' })).toBeInTheDocument()
    expect(await screen.findByText(/2 times/)).toBeInTheDocument()
    expect(screen.getByText(/Invited .* by admin@example.com/)).toBeInTheDocument()
    expect(screen.getByText(/They grant nothing/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve|invite/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Dismiss ada@example.com' }))
    expect(await screen.findByText('No past requests.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Dismiss bo@example.com' }))
    expect(await screen.findByText('No past invitations.')).toBeInTheDocument()
    expect(invited).toBe(false)
  })

  it('pages through a long list of past requests', async () => {
    const user = userEvent.setup()
    const offsets: string[] = []
    server.use(http.get('/studio/access-requests', ({ request }) => {
      const params = new URL(request.url).searchParams
      if (params.get('status') === 'invited') return HttpResponse.json({ items: [], total: 0 })
      const offset = Number(params.get('offset'))
      offsets.push(String(offset))
      const items = Array.from({ length: offset ? 20 : 50 }, (_, n) => ({ email: `p${offset + n}@example.com`, first_requested_at: '2026-10-21T09:00:00Z', last_requested_at: '2026-10-21T09:00:00Z', request_count: 1, invited_at: null }))
      return HttpResponse.json({ items, total: 70 })
    }))
    renderPage()

    expect(await screen.findByText('1–50 of 70')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByText('51–70 of 70')).toBeInTheDocument()
    expect(screen.getByText('p50@example.com')).toBeInTheDocument()
    expect(offsets).toEqual(['0', '50'])
  })

  it('steps back a page when the last row on a later page is dismissed', async () => {
    const user = userEvent.setup()
    let total = 51
    server.use(
      http.get('/studio/access-requests', ({ request }) => {
        const params = new URL(request.url).searchParams
        if (params.get('status') === 'invited') return HttpResponse.json({ items: [], total: 0 })
        const offset = Number(params.get('offset'))
        const count = Math.max(0, Math.min(50, total - offset))
        const items = Array.from({ length: count }, (_, n) => ({ email: `p${offset + n}@example.com`, first_requested_at: '2026-10-21T09:00:00Z', last_requested_at: '2026-10-21T09:00:00Z', request_count: 1, invited_at: null }))
        return HttpResponse.json({ items, total })
      }),
      http.delete('/studio/access-requests', () => { total -= 1; return new HttpResponse(null, { status: 204 }) }),
    )
    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Next' }))
    await user.click(await screen.findByRole('button', { name: 'Dismiss p50@example.com' }))

    expect(await screen.findByText('p0@example.com')).toBeInTheDocument()
    expect(screen.queryByText('No past requests.')).not.toBeInTheDocument()
  })

  it('says when a dismissal fails', async () => {
    const user = userEvent.setup()
    server.use(
      http.get('/studio/access-requests', ({ request }) => {
        const invited = new URL(request.url).searchParams.get('status') === 'invited'
        const items = invited ? [] : [{ email: 'ada@example.com', first_requested_at: '2026-10-21T09:00:00Z', last_requested_at: '2026-10-21T09:00:00Z', request_count: 1, invited_at: null }]
        return HttpResponse.json({ items, total: items.length })
      }),
      http.delete('/studio/access-requests', () => HttpResponse.json({ detail: 'boom' }, { status: 500 })),
    )
    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Dismiss ada@example.com' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('ada@example.com could not be dismissed. Try again.')
  })

  it('tells a non-admin the page is for admins', async () => {
    server.use(http.get('/studio/access-requests', () => HttpResponse.json({ detail: 'administrator required' }, { status: 403 })))
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent('Only admins can see past access requests.')
  })
})

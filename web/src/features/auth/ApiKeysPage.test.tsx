import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'

import { ApiKeysPage } from './ApiKeysPage'
import type { ApiKey } from '../../api/types'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const existing: ApiKey = {
  key_id: 'key-1', actor_id: 'email:someone@example.com', name: 'Laptop',
  created_at: '2026-09-01T09:00:00Z', last_used_at: null, revoked_at: null,
}

function renderKeys() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <MemoryRouter><QueryClientProvider client={client}><ApiKeysPage /></QueryClientProvider></MemoryRouter>,
  )
}

describe('API keys page', () => {
  it('lists the caller keys and links the API documentation', async () => {
    server.use(http.get('/studio/api-keys', () => HttpResponse.json([existing])))
    renderKeys()
    expect(await screen.findByText('Laptop')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'API documentation' })).toHaveAttribute('href', '/studio/api.md')
    expect(screen.getByRole('link', { name: 'API reference' })).toHaveAttribute('href', '/studio/reference')
  })

  it('reveals a created key once and never in the refreshed list', async () => {
    const user = userEvent.setup()
    let keys: ApiKey[] = []
    server.use(
      http.get('/studio/api-keys', () => HttpResponse.json(keys)),
      http.post('/studio/api-keys', async ({ request }) => {
        expect(await request.json()).toEqual({ name: 'CI runner' })
        const created = { ...existing, key_id: 'key-2', name: 'CI runner' }
        keys = [created]
        return HttpResponse.json({ ...created, key: 'mvs_live_secret_value' })
      }),
    )
    renderKeys()

    await user.type(await screen.findByLabelText('Key name'), 'CI runner')
    await user.click(screen.getByRole('button', { name: 'Create key' }))

    expect(await screen.findByText('mvs_live_secret_value')).toBeInTheDocument()
    expect(screen.getByText('Copy it now — this is the only time it is shown.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByText('mvs_live_secret_value')).not.toBeInTheDocument()
    expect(await screen.findByText('CI runner')).toBeInTheDocument()
  })

  it('revokes a key behind an inline confirm', async () => {
    const user = userEvent.setup()
    let keys: ApiKey[] = [existing]
    server.use(
      http.get('/studio/api-keys', () => HttpResponse.json(keys)),
      http.delete('/studio/api-keys/key-1', () => {
        keys = [{ ...existing, revoked_at: '2026-09-06T10:00:00Z' }]
        return new HttpResponse(null, { status: 204 })
      }),
    )
    renderKeys()

    await user.click(await screen.findByRole('button', { name: 'Revoke Laptop' }))
    expect(screen.getByText('Revoke Laptop?')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Yes, revoke' }))

    expect(await screen.findByText(/^Revoked /)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke Laptop' })).not.toBeInTheDocument()
  })
})

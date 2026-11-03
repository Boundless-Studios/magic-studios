import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'

import { MachinesPage } from './MachinesPage'
import type { EnrolledWorker } from '../../api/types'

const server = setupServer(http.get('/pool/workers', () => HttpResponse.json({ workers: [] })))
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const machine: EnrolledWorker = {
  worker_id: 'machine-1',
  label: 'Desk rig',
  created_at: '2026-09-01T09:00:00Z',
  last_seen_at: null,
  revoked_at: null,
}

function renderMachines() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <MemoryRouter><QueryClientProvider client={client}><MachinesPage /></QueryClientProvider></MemoryRouter>,
  )
}

describe('Machines page', () => {
  it('says plainly when there are no machines rather than showing an empty list', async () => {
    server.use(http.get('/studio/workers', () => HttpResponse.json([])))

    renderMachines()

    expect(await screen.findByText(/No machines yet/)).toBeInTheDocument()
  })

  it('lists a machine and shows it has never connected', async () => {
    server.use(http.get('/studio/workers', () => HttpResponse.json([machine])))

    renderMachines()

    expect(await screen.findByText('Desk rig')).toBeInTheDocument()
    expect(screen.getByText('Not connected yet')).toBeInTheDocument()
  })

  it('reveals a pairing code once, with when it expires', async () => {
    server.use(
      http.get('/studio/workers', () => HttpResponse.json([])),
      http.post('/studio/workers/pairing-tokens', () =>
        HttpResponse.json({ token: 'mvsp_abc123', expires_at: '2026-09-07T13:00:00Z' }),
      ),
    )
    renderMachines()
    await screen.findByText(/No machines yet/)

    await userEvent.click(screen.getByRole('button', { name: 'Add a machine' }))

    // Both are always offered: the machine being attached is often not the one
    // you are reading this on, and `curl … | sh` fails outright in PowerShell.
    await userEvent.click(screen.getByRole('button', { name: 'macOS or Linux' }))
    expect(await screen.findByText(/curl -fsSL .*\/attach\.sh .* mvsp_abc123/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Windows' }))
    // Through the environment, not an argument: a piped script cannot take one,
    // and `iex` has no -Args parameter at all.
    expect(await screen.findByText(/MVS_CODE="mvsp_abc123".*attach\.ps1 \| iex/)).toBeInTheDocument()
    // The wording has to tell the reader it is single-use, because there is no
    // route to read it back and no way for us to show it again.
    expect(screen.getByText(/works once/)).toBeInTheDocument()
  })

  it('asks before removing a machine and says what removal does', async () => {
    server.use(http.get('/studio/workers', () => HttpResponse.json([machine])))
    renderMachines()
    await screen.findByText('Desk rig')

    await userEvent.click(screen.getByRole('button', { name: 'Remove Desk rig' }))

    expect(screen.getByText(/stops taking work immediately/)).toBeInTheDocument()
  })

  it('shows a removed machine as removed rather than hiding it', async () => {
    server.use(
      http.get('/studio/workers', () =>
        HttpResponse.json([{ ...machine, revoked_at: '2026-09-02T10:00:00Z' }]),
      ),
    )

    renderMachines()

    expect(await screen.findByText(/^Removed /)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove Desk rig' })).not.toBeInTheDocument()
  })

  it('reports a failure to load rather than looking empty', async () => {
    server.use(http.get('/studio/workers', () => new HttpResponse(null, { status: 500 })))

    renderMachines()

    expect(await screen.findByRole('alert')).toHaveTextContent('Your machines could not be loaded.')
  })
})

 it('shows the full admin cluster and uses authoritative heartbeat presence', async () => {
    server.use(http.get('/studio/workers', () => HttpResponse.json([machine])), http.get('/pool/workers', () => HttpResponse.json({ cluster_admin: true, workers: [
      { worker_id: 'machine-1', status: 'online', last_heartbeat_at: new Date().toISOString(), gpus: [], capabilities: [] },
      { worker_id: 'justin-worker', status: 'online', last_heartbeat_at: new Date().toISOString(), gpus: [{ index: 0, name: 'RTX 3090', utilization_pct: 0, memory_used_mb: 1024, memory_total_mb: 24576 }], capabilities: [] },
    ] })))
    renderMachines()
    expect(await screen.findByRole('heading', { name: 'Full cluster' })).toBeVisible()
    expect(screen.getByText('justin-worker')).toBeVisible()
    expect(screen.getByText(/RTX 3090.*1.0 \/ 24.0 GB/)).toBeVisible()
    expect(screen.queryByText('Not connected yet')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove justin-worker' })).not.toBeInTheDocument()
  })

it('includes the explicit 16GB profile and Windows ComfyUI URL in a WSL command', async () => {
  server.use(
    http.get('/studio/workers', () => HttpResponse.json([])),
    http.post('/studio/workers/pairing-tokens', () => HttpResponse.json({ token: 'mvsp_test', expires_at: '2026-10-18T00:00:00Z' })),
  )
  renderMachines()
  await userEvent.click(await screen.findByRole('button', { name: 'Add a machine' }))
  await userEvent.click(screen.getByRole('button', { name: 'macOS or Linux' }))
  await userEvent.selectOptions(screen.getByLabelText('Machine configuration'), 'h3-16gb-lightx8')
  expect(screen.getByText(/curl -fsSL .*MVS_WORKER_PROFILE='h3-16gb-lightx8' OGWP_COMFY_URL='http:\/\/127.0.0.1:8189' sh -s -- mvsp_test/)).toBeInTheDocument()
  await userEvent.clear(screen.getByLabelText('Local ComfyUI URL'))
  await userEvent.type(screen.getByLabelText('Local ComfyUI URL'), "http://bad'; echo unsafe")
  expect(screen.getByRole('button', { name: 'Copy command' })).toBeDisabled()
  expect(screen.queryByText(/curl -fsSL/)).not.toBeInTheDocument()
})

it('offers the 12GB subject-image configuration and names it on an attached machine', async () => {
  server.use(
    http.get('/studio/workers', () => HttpResponse.json([machine])),
    http.get('/pool/workers', () => HttpResponse.json({ cluster_admin: false, workers: [
      { worker_id: 'machine-1', status: 'online', last_heartbeat_at: new Date().toISOString(), gpus: [], capabilities: [], runtime_versions: { magic_profile: 'flux2-klein-subject-12gb' } },
    ] })),
    http.post('/studio/workers/pairing-tokens', () => HttpResponse.json({ token: 'mvsp_test', expires_at: '2026-10-18T00:00:00Z' })),
  )
  renderMachines()
  expect(await screen.findByText('Subject images · 12 GB · 1024² · 20 steps')).toBeVisible()
  await userEvent.click(screen.getByRole('button', { name: 'Add a machine' }))
  await userEvent.click(screen.getByRole('button', { name: 'macOS or Linux' }))
  await userEvent.selectOptions(screen.getByLabelText('Machine configuration'), 'flux2-klein-subject-12gb')
  expect(screen.getByText(/MVS_WORKER_PROFILE='flux2-klein-subject-12gb' OGWP_COMFY_URL='http:\/\/127.0.0.1:8189' sh -s -- mvsp_test/)).toBeInTheDocument()
})

it('lists what a smaller card can run and why not the rest', async () => {
  server.use(
    http.get('/studio/workers', () => HttpResponse.json([machine])),
    http.get('/pool/workers', () => HttpResponse.json({ cluster_admin: false, workers: [
      { worker_id: 'machine-1', status: 'online', last_heartbeat_at: new Date().toISOString(), gpus: [], capabilities: [], features: [
        { key: 'subjects', label: 'subject images', available: true, reason: null },
        { key: 'motions', label: 'motions', available: false, reason: 'motions need a 16 GB card and this one has 12 GB' },
        { key: 'movie_takes', label: 'movie takes', available: false, reason: 'movie takes need a 24 GB card and this one has 12 GB' },
        { key: 'movie_processing', label: 'movie processing', available: true, reason: null },
      ] },
    ] })),
  )
  renderMachines()
  const features = await screen.findByRole('list', { name: 'What this machine can run' })
  expect(features).toHaveTextContent('Can run: subject images, movie processing')
  expect(features).toHaveTextContent('Motions: motions need a 16 GB card and this one has 12 GB. A Higgsfield key below runs motions without it.')
  expect(features).toHaveTextContent('Movie takes: movie takes need a 24 GB card and this one has 12 GB.')
})

describe('Higgsfield key', () => {
  it('saves a Higgsfield key without ever displaying it again', async () => {
    let saved: unknown = null
    let configured = false
    server.use(
      http.get('/studio/workers', () => HttpResponse.json([])),
      http.get('/studio/providers', () => HttpResponse.json([{ provider: 'higgsfield', available: true, configured, key_hint: configured ? '…9876' : null, updated_at: null }])),
      http.put('/studio/providers/higgsfield', async ({ request }) => {
        saved = await request.json()
        configured = true
        return HttpResponse.json({ provider: 'higgsfield', available: true, configured: true, key_hint: '…9876', updated_at: null })
      }),
    )
    renderMachines()

    const input = await screen.findByLabelText('Higgsfield API key')
    expect(input).toHaveAttribute('type', 'password')
    await userEvent.type(input, 'key-id:secret-9876')
    await userEvent.click(screen.getByRole('button', { name: 'Save key' }))

    expect(await screen.findByText('…9876')).toBeInTheDocument()
    expect(saved).toEqual({ api_key: 'key-id:secret-9876' })
    expect(screen.getByLabelText('Higgsfield API key')).toHaveValue('')
    expect(screen.queryByText(/secret-9876/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove key' })).toBeInTheDocument()
  })

  it('offers no key panel when the server has provider keys turned off', async () => {
    server.use(
      http.get('/studio/workers', () => HttpResponse.json([])),
      http.get('/studio/providers', () => HttpResponse.json([{ provider: 'higgsfield', available: false, configured: false, key_hint: null, updated_at: null }])),
    )
    renderMachines()
    await screen.findByText(/No machines yet/)

    expect(screen.queryByLabelText('Higgsfield API key')).not.toBeInTheDocument()
  })
})

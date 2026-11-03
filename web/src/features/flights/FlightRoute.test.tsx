import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { FlightRoute } from './FlightRoute'
import { flightApi, type FlightTake, type SceneFlight } from './flightApi'

vi.mock('./flightApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./flightApi')>()
  return { ...actual, flightApi: {
    ...actual.flightApi,
    get: vi.fn(), addImage: vi.fn(), setLegs: vi.fn(), generate: vi.fn(), select: vi.fn(), cancel: vi.fn(), chain: vi.fn(),
  } }
})

const recipe = { width: 768, height: 448, fps: 24, steps: 20, default_duration_seconds: 5, min_duration_seconds: 5, max_duration_seconds: 15, duration_step_seconds: 0.5, max_legs: 24 }
const take = (id: string, overrides: Partial<FlightTake> = {}): FlightTake => ({
  take_id: id, request_id: `r-${id}`, status: 'succeeded', stale: false, error: null, video_key: `outputs/${id}.mp4`, seed: 42,
  width: 768, height: 448, fps: 24, length_frames: 124, steps: 20, duration_seconds: 5.17, first_frame_diff: 0.004, last_frame_diff: 0.011,
  executor_preset: 'balanced', completed_by_worker_id: null, job_id: id, ...overrides,
})
const leg = { leg_id: 'a', index: 0, name: 'Harrowfen to Miretrace', origin_image_key: 'img/a.png', destination_image_key: 'img/b.png', prompt: 'Travel from Harrowfen to Miretrace.', negative_prompt: '', duration_seconds: 5, selected_take_id: null, takes: [] }
const base: SceneFlight = { flight_id: 'f1', name: 'Fen flight', revision: 4, generation_available: true, recipe, images: ['img/a.png', 'img/b.png'], legs: [], chain: null }

function renderFlight(flight: SceneFlight) {
  vi.mocked(flightApi.get).mockResolvedValue(structuredClone(flight))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/flights/f1']}><Routes><Route path="/flights/:flightId" element={<FlightRoute />} /></Routes></MemoryRouter></QueryClientProvider>)
}

beforeEach(() => { vi.clearAllMocks() })

it('plans a leg between two uploaded images and saves it', async () => {
  vi.mocked(flightApi.setLegs).mockResolvedValue({ ...base, revision: 5, legs: [leg] })
  const user = userEvent.setup(); renderFlight(base)
  await user.click(await screen.findByRole('button', { name: 'Add a leg' }))
  expect(screen.getByLabelText('Origin image')).toHaveValue('img/a.png')
  expect(screen.getByLabelText('Destination image')).toHaveValue('img/b.png')
  await user.type(screen.getByLabelText('Origin place name'), 'Harrowfen')
  await user.type(screen.getByLabelText('Destination place name'), 'Miretrace')
  await user.click(screen.getByRole('button', { name: 'Save legs' }))
  expect(flightApi.setLegs).toHaveBeenCalledWith('f1', 4, [expect.objectContaining({
    origin_image_key: 'img/a.png', destination_image_key: 'img/b.png', origin_name: 'Harrowfen', destination_name: 'Miretrace', prompt: '', duration_seconds: 5,
  })])
  expect(await screen.findByRole('button', { name: 'Generate this leg' })).toBeEnabled()
})

it('uploads location images as the flight images', async () => {
  vi.mocked(flightApi.addImage).mockResolvedValue({ image_key: 'img/c.png', revision: 5, width: 1536, height: 864 })
  const user = userEvent.setup(); renderFlight(base)
  const file = new File(['png'], 'fen.png', { type: 'image/png' })
  await user.upload(await screen.findByLabelText('Add location images'), file)
  expect(flightApi.addImage).toHaveBeenCalledWith('f1', file)
})

it('generates, re-rolls and selects takes, then chains when every leg has one', async () => {
  const withTakes = { ...base, legs: [{ ...leg, selected_take_id: 't1', takes: [take('t1'), take('t2', { seed: 7 })] }] }
  vi.mocked(flightApi.generate).mockResolvedValue(withTakes)
  vi.mocked(flightApi.select).mockResolvedValue({ ...withTakes, legs: [{ ...withTakes.legs[0], selected_take_id: 't2' }] })
  vi.mocked(flightApi.chain).mockResolvedValue({ ...withTakes, chain: { request_id: 'c', status: 'succeeded', video_key: 'cut.mp4', error: null, joins: [] } })
  const user = userEvent.setup(); renderFlight(withTakes)
  const takes = await screen.findByRole('region', { name: 'Harrowfen to Miretrace takes' })
  expect(within(takes).getAllByText(/First frame vs origin: 0.4%/)[0]).toBeVisible()
  await user.click(within(takes).getByRole('button', { name: 'Re-roll this leg' }))
  expect(flightApi.generate).toHaveBeenCalledWith('f1', 'a', 4, expect.any(String))
  const second = within(takes).getAllByRole('listitem')[1]
  await user.click(within(second).getByRole('button', { name: 'Use this take' }))
  expect(flightApi.select).toHaveBeenCalledWith('f1', 'a', 4, 't2')
  expect(within(second).getByRole('link', { name: 'Download' })).toHaveAttribute('href', '/studio/scene-flights/f1/legs/a/takes/t2/video?download=true')
  await user.click(screen.getByRole('button', { name: 'Chain flight' }))
  expect(await screen.findByRole('link', { name: 'Download chained flight' })).toHaveAttribute('href', '/studio/scene-flights/f1/chain/video?download=true')
})

it('keeps generation off where no executor can pin a last frame', async () => {
  renderFlight({ ...base, generation_available: false, legs: [leg] })
  expect(await screen.findByText(/no GPU executor that can pin/)).toBeVisible()
  expect(screen.getByRole('button', { name: 'Generate this leg' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Chain flight' })).toBeDisabled()
})

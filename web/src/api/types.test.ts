import { expect, it } from 'vitest'
import { canStartTurnaround, type GenerationConfig } from './types'

const worker = (available: boolean, lastFrame?: boolean, profile = 'h3-24gb-standard') => ({
  worker_id: 'desk', available, reason: null, profile: { profile },
  ...(lastFrame === undefined ? {} : { features: { last_frame: lastFrame } }),
})

it('offers a turnaround only when an available machine itself reports last-frame support', () => {
  expect(canStartTurnaround({ workers: [worker(true, true)] } satisfies GenerationConfig)).toBe(true)
  expect(canStartTurnaround({ workers: [worker(true, false), worker(false, true)] })).toBe(false)
  // A server that predates the field is never read as eligible.
  expect(canStartTurnaround({ workers: [worker(true)] })).toBe(false)
  expect(canStartTurnaround({})).toBe(false)
  expect(canStartTurnaround(undefined)).toBe(false)
})

it('never offers a turnaround on the 16GB LightX recipe, even when it reports last-frame support', () => {
  expect(canStartTurnaround({ workers: [worker(true, true, 'h3-16gb-lightx8')] })).toBe(false)
  expect(canStartTurnaround({ workers: [worker(true, true, 'h3-16gb-lightx8'), worker(true, true)] })).toBe(true)
  expect(canStartTurnaround({ workers: [{ worker_id: 'desk', available: true, reason: null, profile: null, features: { last_frame: true } }] })).toBe(false)
})

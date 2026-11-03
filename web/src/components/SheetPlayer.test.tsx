import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MotionCandidate } from '../api/types'
import { SheetPlayer } from './SheetPlayer'

const candidate: MotionCandidate = {
  candidate_id: 'candidate-1', artifact_key: 'idle.png', sha256: 'a', duration_ms: 400,
  metadata: { decodable: true, media_kind: 'sheet', frame_count: 4, width: 256, height: 64, constant_geometry: true, has_alpha: true, duplicate_terminal_frame: false, baseline_y: null, packing_estimate: { profile: 'horizontal-strip-v1', width: 256, height: 64 } },
  assessment: null, ordered_frames: null, frame_duration_ms: 100, loop_policy: 'loop',
  generation_settings: null, facing: 'right', recipe: null, source: 'generated',
}

let observed: { target: Element; callback: IntersectionObserverCallback } | null = null

class FakeIntersectionObserver {
  constructor(private callback: IntersectionObserverCallback) {}
  observe(target: Element) { observed = { target, callback: this.callback } }
  disconnect() { observed = null }
  unobserve() {}
}

beforeEach(() => {
  vi.useFakeTimers()
  observed = null
})
afterEach(() => {
  vi.useRealTimers()
})

describe('SheetPlayer', () => {
  it('fits the measured frame without stretching it to a wide preview card', () => {
    render(<SheetPlayer name="Idle" imageUrl="/idle.png" candidate={candidate} className="take-card__media" />)
    const player = screen.getByRole('img')
    const viewport = player.querySelector('svg')!
    expect(viewport).toHaveAttribute('viewBox', '0 0 64 64')
    expect(viewport).toHaveAttribute('preserveAspectRatio', 'xMidYMid meet')
    const cell = viewport.querySelector('svg')!
    expect(cell).toHaveAttribute('width', '64')
    expect(cell).toHaveAttribute('height', '64')
    expect(cell).toHaveAttribute('overflow', 'hidden')
    expect(viewport.querySelector('image')).toHaveAttribute('width', '256')
    act(() => { vi.advanceTimersByTime(100) })
    expect(viewport.querySelector('image')).toHaveAttribute('x', '-64')
  })

  it('advances frames on a timer at frame_duration_ms', () => {
    render(<SheetPlayer name="Idle" imageUrl="https://assets.invalid/idle.png" candidate={candidate} />)
    expect(screen.getByRole('img', { name: 'Idle, frame 1 of 4' })).toBeInTheDocument()

    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('img', { name: 'Idle, frame 2 of 4' })).toBeInTheDocument()

    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('img', { name: 'Idle, frame 3 of 4' })).toBeInTheDocument()
  })

  it('expands holds into the playback sequence', () => {
    const held: MotionCandidate = { ...candidate, ordered_frames: [0, 1, 2, 3], frame_holds: [1, 3, 1, 1] }
    render(<SheetPlayer name="Idle" imageUrl="https://assets.invalid/idle.png" candidate={held} />)
    expect(screen.getByRole('img', { name: 'Idle, frame 1 of 6' })).toBeInTheDocument()
  })

  it('mirrors a left-facing candidate', () => {
    render(<SheetPlayer name="Idle" imageUrl="https://assets.invalid/idle.png" candidate={{ ...candidate, facing: 'left' }} />)
    expect(screen.getByRole('img', { name: 'Idle, frame 1 of 4' })).toHaveStyle({ transform: 'scaleX(-1)' })
  })

  it('pauses when off-screen and treats missing IntersectionObserver as always visible', () => {
    const originalIO = globalThis.IntersectionObserver
    // @ts-expect-error test-only override of a browser global
    globalThis.IntersectionObserver = FakeIntersectionObserver
    render(<SheetPlayer name="Idle" imageUrl="https://assets.invalid/idle.png" candidate={candidate} />)
    expect(observed).not.toBeNull()

    act(() => { observed!.callback([{ isIntersecting: false } as IntersectionObserverEntry], observed!.target as unknown as IntersectionObserver) })
    act(() => { vi.advanceTimersByTime(500) })
    expect(screen.getByRole('img', { name: 'Idle, frame 1 of 4' })).toBeInTheDocument()

    act(() => { observed!.callback([{ isIntersecting: true } as IntersectionObserverEntry], observed!.target as unknown as IntersectionObserver) })
    act(() => { vi.advanceTimersByTime(100) })
    expect(screen.getByRole('img', { name: 'Idle, frame 2 of 4' })).toBeInTheDocument()

    globalThis.IntersectionObserver = originalIO
  })
})

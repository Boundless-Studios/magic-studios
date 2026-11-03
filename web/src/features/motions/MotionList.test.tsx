import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import type { Motion, Operation } from '../../api/types'
import { MotionList } from './MotionList'
import { railThumbFrameStyle } from './motionRailThumb'

const idle: Motion = { motion_id: 'idle', name: 'Idle', kind: 'preset', semantic_slot: 'idle', loop_policy: 'loop', required: true, candidates: [], selected_candidate_id: null }
const walk: Motion = { motion_id: 'walk', name: 'Walk', kind: 'preset', semantic_slot: 'walk', loop_policy: 'loop', required: true, candidates: [], selected_candidate_id: null }
const attack: Motion = { motion_id: 'attack', name: 'Attack', kind: 'preset', semantic_slot: 'attack', loop_policy: 'once', required: true, candidates: [], selected_candidate_id: null }

function renderRail(selectedId: string | null, onSelect: (id: string) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MotionList projectId="project-1" motions={[idle, walk, attack]} operations={[]} selectedId={selectedId} onSelect={onSelect} checkedIds={new Set()} onToggleChecked={vi.fn()} />
    </QueryClientProvider>,
  )
}

it('renders a listbox of options with aria-selected reflecting the current selection', () => {
  renderRail('walk', vi.fn())
  const listbox = screen.getByRole('listbox', { name: 'Motions' })
  expect(listbox).toBeInTheDocument()
  expect(screen.getByRole('option', { name: 'Walk To generate' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('option', { name: 'Idle To generate' })).toHaveAttribute('aria-selected', 'false')
})

it('moves selection down with ArrowDown and up with ArrowUp', async () => {
  const user = userEvent.setup()
  const onSelect = vi.fn()
  renderRail('idle', onSelect)
  screen.getByRole('option', { name: 'Idle To generate' }).focus()

  await user.keyboard('{ArrowDown}')
  expect(onSelect).toHaveBeenLastCalledWith('walk')

  await user.keyboard('{ArrowDown}')
  expect(onSelect).toHaveBeenLastCalledWith('attack')

  // Already at the last row: ArrowDown does not wrap or call again with an out-of-range id.
  await user.keyboard('{ArrowDown}')
  expect(onSelect).toHaveBeenCalledTimes(2)
})

it('activates the focused row with Enter', async () => {
  const user = userEvent.setup()
  const onSelect = vi.fn()
  renderRail('idle', onSelect)
  screen.getByRole('option', { name: 'Walk To generate' }).focus()

  await user.keyboard('{Enter}')
  expect(onSelect).toHaveBeenCalledWith('walk')
})

it('computes frame 0 of a sprite sheet by real pixel math, not CSS percentage sprite math', () => {
  // 256x64 sheet, 4 frames -> each frame is 64x64. Scaled to a 40px tile (auto 40px height),
  // the scale factor is 40/64, so the scaled sheet is 160px wide and frame 0 sits at x=0.
  const style = railThumbFrameStyle('https://assets.invalid/idle.png', { width: 256, height: 64, frame_count: 4 }, 40)
  expect(style.backgroundImage).toBe('url("https://assets.invalid/idle.png")')
  expect(style.backgroundSize).toBe('auto 40px')
  expect(style.backgroundPosition).toBe('-0px center')
  expect(style.backgroundRepeat).toBe('no-repeat')
})

it('renders exactly one status text on a queued/processing row (the chip), no second raw stage line', () => {
  const operations: Operation[] = [{ job_id: 'job-1', status: 'processing', motion_id: 'walk', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false }]
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MotionList projectId="project-1" motions={[idle, walk, attack]} operations={operations} selectedId="idle" onSelect={vi.fn()} checkedIds={new Set()} onToggleChecked={vi.fn()} />
    </QueryClientProvider>,
  )
  const row = screen.getByRole('option', { name: 'Walk Generating…' })
  expect(row.querySelectorAll('[role="status"]')).toHaveLength(1)
  expect(row).toHaveTextContent('Generating…')
})

it('shows an accent, spinning pill on every in-flight row -- "Generating…" running, "Queued" waiting', () => {
  const operations: Operation[] = [
    { job_id: 'job-1', status: 'processing', motion_id: 'walk', output_key: 'out', progress_percent: null, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false },
    { job_id: 'job-2', status: 'queued', motion_id: 'attack', output_key: 'out', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false },
  ]
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MotionList projectId="project-1" motions={[idle, walk, attack]} operations={operations} selectedId="idle" onSelect={vi.fn()} checkedIds={new Set()} onToggleChecked={vi.fn()} />
    </QueryClientProvider>,
  )
  const running = screen.getByRole('option', { name: 'Walk Generating…' }).querySelector('.chip')!
  expect(running).toHaveClass('chip--accent')
  expect(running.querySelector('.button-spinner')).not.toBeNull()

  const queued = screen.getByRole('option', { name: 'Attack Queued' }).querySelector('.chip')!
  expect(queued).toHaveTextContent('Queued')
  expect(queued).toHaveClass('chip--accent')
  expect(queued.querySelector('.button-spinner')).not.toBeNull()

  // An idle row keeps the quiet neutral pill and does not spin.
  const idleChip = screen.getByRole('option', { name: 'Idle To generate' }).querySelector('.chip')!
  expect(idleChip).toHaveClass('chip--neutral')
  expect(idleChip.querySelector('.button-spinner')).toBeNull()
})

it('keeps the failure pill in the warning colour', () => {
  const operations: Operation[] = [{ job_id: 'job-1', status: 'failed', motion_id: 'walk', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Try again.', retryable: true }]
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MotionList projectId="project-1" motions={[walk]} operations={operations} selectedId="walk" onSelect={vi.fn()} checkedIds={new Set()} onToggleChecked={vi.fn()} />
    </QueryClientProvider>,
  )
  const chip = screen.getByRole('option', { name: 'Walk Failed' }).querySelector('.chip')!
  expect(chip).toHaveTextContent('Failed')
  expect(chip).toHaveClass('chip--danger')
})

it('toggles a row\'s inclusion checkbox without changing the selected motion', async () => {
  const user = userEvent.setup()
  const onSelect = vi.fn()
  const onToggleChecked = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <MotionList projectId="project-1" motions={[idle, walk, attack]} operations={[]} selectedId="idle" onSelect={onSelect} checkedIds={new Set(['idle'])} onToggleChecked={onToggleChecked} />
    </QueryClientProvider>,
  )
  const checkbox = screen.getByRole('checkbox', { name: 'Include Walk in the next batch' })
  expect(checkbox).not.toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'Include Idle in the next batch' })).toBeChecked()
  await user.click(checkbox)
  expect(onToggleChecked).toHaveBeenCalledWith('walk')
  expect(onSelect).not.toHaveBeenCalled()
})

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { axe } from 'vitest-axe'

import { ProjectShell, type StudioProject } from './ProjectShell'
import { ProjectActivity } from './ProjectActivity'

const PROJECT: StudioProject = {
  id: 'project-1',
  name: 'Ember Knight',
  saveState: 'Saved',
}

afterEach(() => vi.useRealTimers())

function renderShell(actions: { onExport?: () => void; onRelease?: () => void } = {}, activity?: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}><MemoryRouter initialEntries={['/projects/project-1/subject']}>
      <Routes>
        <Route path="/projects/:projectId" element={<ProjectShell activity={activity} project={PROJECT} {...actions} />}>
          <Route path="subject" element={<h2>Subject workspace</h2>} />
          <Route path="motions" element={<h2>Motion workspace</h2>} />
        </Route>
      </Routes>
    </MemoryRouter></QueryClientProvider>,
  )
}

describe('ProjectShell', () => {
  it('shows durable project destinations without pipeline jargon', () => {
    renderShell()

    const navigation = screen.getByRole('navigation', { name: 'Project' })
    expect(navigation).toHaveTextContent('Subject')
    expect(navigation).toHaveTextContent('Motions')
    expect(navigation).toHaveTextContent('Release & export')
    expect(navigation).not.toHaveTextContent('Quality')
    expect(navigation).not.toHaveTextContent('History')
    expect(screen.queryByText(/manifest|job id|provider/i)).not.toBeInTheDocument()
  })

  it('keeps unavailable actions focusable and visibly explains their prerequisites', async () => {
    const user = userEvent.setup()
    renderShell()

    expect(screen.getByRole('heading', { name: 'Ember Knight', level: 1 })).toBeInTheDocument()
    expect(screen.getByText('Saved')).toBeInTheDocument()
    const exportButton = screen.getByRole('button', { name: 'Export' })
    const releaseButton = screen.getByRole('button', { name: 'Release' })
    expect(exportButton).not.toBeDisabled()
    expect(releaseButton).not.toBeDisabled()
    expect(exportButton).toHaveAttribute('aria-disabled', 'true')
    expect(releaseButton).toHaveAttribute('aria-disabled', 'true')
    expect(exportButton).toHaveAccessibleDescription('Complete and release this animation set first.')
    expect(releaseButton).toHaveAccessibleDescription('Approve the subject and motion set first.')
    expect(screen.getByText('Approve the subject and motion set first.')).toBeVisible()
    expect(screen.getByText('Complete and release this animation set first.')).toBeVisible()

    await user.tab()
    await user.tab()
    expect(screen.getByRole('link', { name: 'All projects' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('link', { name: 'New project' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Account' })).toHaveFocus()
    await user.tab()
    expect(exportButton).toHaveFocus()
    await user.tab()
    expect(releaseButton).toHaveFocus()
  })

  it('guards unavailable actions from pointer and keyboard activation', async () => {
    const user = userEvent.setup()
    const onExport = vi.fn()
    const onRelease = vi.fn()
    renderShell({ onExport, onRelease })

    const exportButton = screen.getByRole('button', { name: 'Export' })
    const releaseButton = screen.getByRole('button', { name: 'Release' })
    await user.click(exportButton)
    releaseButton.focus()
    await user.keyboard('{Enter}')
    await user.keyboard(' ')

    expect(onExport).not.toHaveBeenCalled()
    expect(onRelease).not.toHaveBeenCalled()
    expect(window.location.hash).toBe('')
  })

  it('marks the current destination and updates it after navigation', async () => {
    const user = userEvent.setup()
    renderShell()

    const subject = screen.getByRole('link', { name: 'Subject' })
    const motions = screen.getByRole('link', { name: 'Motions' })
    expect(subject).toHaveAttribute('aria-current', 'page')
    expect(motions).not.toHaveAttribute('aria-current')

    await user.click(motions)

    expect(motions).toHaveAttribute('aria-current', 'page')
    expect(subject).not.toHaveAttribute('aria-current')
    expect(screen.getByRole('heading', { name: 'Motion workspace' })).toBeInTheDocument()
  })

  it('moves focus to the main workspace from the skip link', async () => {
    const user = userEvent.setup()
    renderShell()

    const skipLink = screen.getByRole('link', { name: 'Skip to content' })
    const main = screen.getByRole('main')
    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main-content')
    expect(main).toHaveAttribute('id', 'main-content')
    expect(main).toHaveAttribute('tabindex', '-1')

    await user.click(skipLink)

    expect(main).toHaveFocus()
  })

  it('has no axe violations', async () => {
    const view = renderShell()
    const result = await axe(view.container, { rules: { 'color-contrast': { enabled: false } } })
    expect(result.violations.filter((violation) => violation.impact === 'serious' || violation.impact === 'critical')).toEqual([])
  })

  it('shows durable activity with real progress and retry policy', async () => {
    renderShell({}, <ProjectActivity projectId="project-1" operations={[
      { job_id: 'run', operation: 'generate_motion', status: 'processing', motion_id: 'victory', output_key: 'out', progress_percent: 42, stage: 'generating', failure_code: null, failure_explanation: null, retryable: false, created_at: '2026-08-31T00:00:00Z', updated_at: '2026-08-31T00:00:01Z' },
      { job_id: 'fail', operation: 'generate_motion', status: 'failed', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'failed', failure_code: 'temporary_failure', failure_explanation: 'Worker interrupted.', retryable: true, created_at: null, updated_at: null },
    ]} workers={[]} queue={{ queued: 0, processing: 0, by_capability: {} }} />)
    await userEvent.click(screen.getByRole('button', { name: /GPU status, 1 running, 0 queued, 1 failed/ }))
    expect(screen.getByText('Victory')).toBeInTheDocument(); expect(screen.getByRole('progressbar', { name: 'Victory progress' })).toHaveAttribute('value', '42'); expect(screen.getByRole('button', { name: 'Retry Idle' })).toBeInTheDocument(); expect(screen.getAllByText('No GPU online')).toHaveLength(2)
  })

  it('traps keyboard focus inside the activity dialog', async () => {
    const user = userEvent.setup()
    renderShell({}, <ProjectActivity projectId="project-1" operations={[]} workers={[]} queue={{ queued: 0, processing: 0, by_capability: {} }} />)
    await user.click(screen.getByRole('button', { name: /GPU status/ }))
    const close = screen.getByRole('button', { name: 'Close activity' })
    const log = screen.getByRole('link', { name: 'Full activity log' })
    expect(close).toHaveFocus()
    await user.tab({ shift: true })
    expect(log).toHaveFocus()
    await user.tab()
    expect(close).toHaveFocus()
    await user.tab({ shift: true })
    expect(log).toHaveFocus()
  })
})

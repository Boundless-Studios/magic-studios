import { useLayoutEffect, useRef, type MouseEvent, type ReactNode } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { useStudioSession } from '../auth/sessionContext'
import { rememberProject } from './recentProject'

import { AccountMenu } from '../auth/AccountMenu'
import { Button } from '../../components/ui/Button'
import { StatusText } from '../../components/ui/StatusText'

export interface StudioProject {
  id: string
  name: string
  saveState: 'Saved' | 'Saving' | 'Unsaved changes'
  releaseReady?: boolean
  exportReady?: boolean
}

export interface ProjectShellProps {
  /** Replaces the Export/Release pair for workflows without releases. */
  actions?: ReactNode
  activity?: ReactNode
  children?: ReactNode
  onExport?: () => void
  onRelease?: () => void
  project: StudioProject
  workflow?: { name: string; destinations: { label: string; path: string }[]; releaseHint: string; exportHint: string }
}

const destinations = [
  { label: 'Subject', path: 'subject' },
  { label: 'Motions', path: 'motions' },
  { label: 'Release & export', path: 'exports' },
] as const

function Wordmark() {
  return (
    <div className="wordmark" aria-label="Magic Studios">
      <span className="wordmark__mark" aria-hidden="true">M</span>
      <span className="wordmark__name">Magic Studios</span>
    </div>
  )
}

function ProjectNavigation({ items = destinations }: { items?: readonly { label: string; path: string }[] }) {
  return (
    <nav aria-label="Project" className="project-navigation">
      {items.map(({ label, path }) => (
        <NavLink
          className={({ isActive }) => `project-navigation__link${isActive ? ' is-active' : ''}`}
          key={path}
          to={path}
        >
          {label}
        </NavLink>
      ))}
    </nav>
  )
}

export function ProjectShell({ actions, activity, children, onExport, onRelease, project, workflow }: ProjectShellProps) {
  const session = useStudioSession()
  const { pathname } = useLocation()
  useLayoutEffect(() => {
    if (session) rememberProject(session.actorId, pathname)
  }, [session, pathname])
  const saveTone = project.saveState === 'Saved' ? 'success' : 'neutral'
  const mainRef = useRef<HTMLElement>(null)
  const navigate = useNavigate()

  function focusWorkspace(event: MouseEvent<HTMLAnchorElement>) {
    event.preventDefault()
    mainRef.current?.focus()
    window.history.replaceState(null, '', '#main-content')
  }

  return (
    <div className="project-shell">
      <a className="skip-link" href="#main-content" onClick={focusWorkspace}>Skip to content</a>

      <aside className="project-sidebar">
        <Wordmark />
        <div className="project-sidebar__context">
          <span className="project-sidebar__label">Project</span>
          <span className="project-sidebar__name">{project.name}</span>
        </div>
        <p className="project-sidebar__footer">{workflow?.name ?? 'Animation Studio'}</p>
      </aside>

      <div className="project-shell__body">
        <header className="project-header">
          <div className="project-header__mobile-brand"><Wordmark /></div>
          <div className="project-header__title">
            <h1>{project.name}</h1>
            <nav aria-label="Switch project"><Link className="button" to="/projects">All projects</Link> <Link className="button" to="/projects/new">New project</Link></nav>
            <StatusText tone={saveTone}>{project.saveState}</StatusText>
          </div>
          <div className="project-header__action-area">
            <div className="project-header__session">{activity}<AccountMenu /></div>
            {actions ?? <>
            <div className="project-header__actions">
              <Button
                aria-describedby={project.exportReady ? undefined : 'export-unavailable-reason'}
                aria-disabled={!project.exportReady}
                onClick={onExport ?? (() => navigate('exports'))}
              >
                Export
              </Button>
              <Button
                aria-describedby={project.releaseReady ? undefined : 'release-unavailable-reason'}
                aria-disabled={!project.releaseReady}
                onClick={onRelease ?? (() => navigate('exports'))}
                variant="primary"
              >
                Release
              </Button>
            </div>
            <div className="project-header__prerequisites">
              {!project.releaseReady && <span id="release-unavailable-reason">{workflow?.releaseHint ?? 'Approve the subject and motion set first.'}</span>}
              {!project.exportReady && <span id="export-unavailable-reason">{workflow?.exportHint ?? 'Complete and release this animation set first.'}</span>}
            </div>
            </>}
          </div>
        </header>

        <div className="project-responsive-nav"><ProjectNavigation items={workflow?.destinations} /></div>

        <main className="project-content" id="main-content" ref={mainRef} tabIndex={-1}>
          {children ?? <Outlet />}
        </main>
      </div>
    </div>
  )
}

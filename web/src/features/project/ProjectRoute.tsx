import { useEffect, useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'

import { ProjectShell, type StudioProject } from './ProjectShell'
import type { ProjectRepository } from './projectRepository'
import { useProject, useProjectSaveState } from '../../api/queries'
import { useQuery } from '@tanstack/react-query'
import { studioApi } from '../../api/client'
import { ProjectActivity } from './ProjectActivity'

export function WorkspacePlaceholder({ title }: { title: string }) {
  return (
    <section className="workspace-placeholder" aria-labelledby="workspace-title">
      <p className="eyebrow">Project workspace</p>
      <h2 id="workspace-title">{title}</h2>
      <p>This workspace is ready for the next part of your animation set.</p>
    </section>
  )
}

type ProjectLoadState =
  | { status: 'loading' }
  | { status: 'ready'; project: StudioProject }
  | { status: 'not-found' }
  | { status: 'error' }

export function ProjectRoute({ repository }: { repository: ProjectRepository }) {
  const { projectId } = useParams()
  const [state, setState] = useState<ProjectLoadState>({ status: 'loading' })

  useEffect(() => {
    let active = true
    setState({ status: 'loading' })

    if (!projectId) {
      setState({ status: 'not-found' })
      return () => { active = false }
    }

    repository.getById(projectId)
      .then((project) => {
        if (active) setState(project ? { status: 'ready', project } : { status: 'not-found' })
      })
      .catch(() => {
        if (active) setState({ status: 'error' })
      })

    return () => { active = false }
  }, [projectId, repository])

  if (state.status === 'loading') return <ProjectLoadingState />
  if (state.status === 'not-found') return <ProjectNotFoundState />
  if (state.status === 'error') return <ProjectErrorState />
  return <ProjectShell project={state.project} />
}

export function LiveProjectRoute() {
  const { projectId } = useParams(); const project = useProject(projectId ?? ''); const releases = useQuery({ queryKey: ['releases', projectId], queryFn: () => studioApi.releases(projectId!), enabled: Boolean(projectId) })
  const saveState = useProjectSaveState(projectId ?? '')
  const navigate = useNavigate()
  if (project.isPending || releases.isPending) return <ProjectLoadingState />
  if (!project.data) return project.error && 'status' in project.error && project.error.status === 404 ? <ProjectNotFoundState /> : <ProjectErrorState />
  return <ProjectShell
    activity={<ProjectActivity projectId={project.data.project_id} />}
    // Release deep-links into the Release page's exports section (studio-ux-flow.md section 5):
    // "Release" focuses the name field, "Export" scrolls to the newest release's export controls.
    onExport={() => navigate('exports', { state: { scrollToExport: true } })}
    onRelease={() => navigate('exports', { state: { focusReleaseName: true } })}
    project={{ id: project.data.project_id, name: project.data.manifest.name, saveState, releaseReady: project.data.manifest.set_approval_fresh, exportReady: Boolean(releases.data?.length) }}
  />
}

export function ProjectIndex() {
  return <Navigate replace to="subject" />
}

export function ErrorState({ description, title }: { description: string; title: string }) {
  return (
    <main className="route-state">
      <div className="route-state__card">
        <div className="wordmark__mark" aria-hidden="true">M</div>
        <p className="eyebrow">Magic Studios</p>
        <h1>{title}</h1>
        <p>{description}</p>
        <a className="button route-state__action" href="/">Return to studio</a>
      </div>
    </main>
  )
}

export function ProjectErrorState() {
  return (
    <ErrorState
      description="We could not load this project. Try again in a moment."
      title="Studio unavailable"
    />
  )
}

export function ProjectNotFoundState() {
  return (
    <ErrorState
      description="This project may have been moved or you may not have access."
      title="Project not found"
    />
  )
}

export function ProjectLoadingState() {
  return (
    <main className="route-state" role="status">
      <div className="route-state__card route-state__card--loading">
        <span className="route-loading-mark" aria-hidden="true" />
        <span>Loading project</span>
      </div>
    </main>
  )
}

export function NotFoundPage() {
  return (
    <ErrorState
      description="The page you requested does not exist."
      title="Page not found"
    />
  )
}

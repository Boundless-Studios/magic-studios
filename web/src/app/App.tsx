import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Component, lazy, Suspense, useEffect, useRef, useState, type ComponentType, type FormEvent, type ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import type { ProjectRepository } from '../features/project/projectRepository'
import { previewProjectRepository } from '../features/project/projectRepository'
import { useStudioSession } from '../features/auth/sessionContext'
import { availableRecentProject, readRecentProject } from '../features/project/recentProject'
import { ProjectHome } from '../features/project/ProjectHome'
import { fetchStudioSession, redirectToLogin, type StudioSession } from '../api/auth'
import { AccountMenu } from '../features/auth/AccountMenu'
import { LoginPage } from '../features/auth/LoginPage'
import { StudioSessionContext } from '../features/auth/sessionContext'
import { ApiError, studioApi } from '../api/client'
import { presetForTargetSlot } from '../features/motions/motionPresets'
import { SentryErrorBoundary } from '../monitoring'
import { movieApi } from '../features/movies/movieApi'
import { flightApi } from '../features/flights/flightApi'
import { audioApi } from '../features/audio/audioApi'
import '../features/movies/movies.css'
import { StoragePolicy } from '../features/policies/StoragePolicy'
import { ArtFirstOnboarding } from '../features/setup/ArtFirstOnboarding'
import { LegacyDonateRedirect, SupportPage, SupportThanksPage } from '../features/support/SupportPage'
import { AboutPage } from '../features/welcome/AboutPage'
import { WelcomePage } from '../features/welcome/WelcomePage'
import { GuidePage } from '../features/guide/GuidePage'
import { ServerPageRedirect } from '../features/reference/ServerPageRedirect'
import { PrivacyPolicy } from '../features/legal/PrivacyPolicy'
import { TermsOfService } from '../features/legal/TermsOfService'
import { hostedService } from '../hostedService'

const queryClient = new QueryClient()

export function RouteFallback() {
  return (
    <main className="route-state" role="status">
      <div className="route-state__card route-state__card--loading">
        <span className="route-loading-mark" aria-hidden="true" />
        <span>Opening studio</span>
      </div>
    </main>
  )
}

interface StudioErrorBoundaryState {
  failed: boolean
}

export function StudioUnavailable() {
  return (
    <main className="route-state">
      <div className="route-state__card">
        <div className="wordmark__mark" aria-hidden="true">M</div>
        <p className="eyebrow">Magic Studios</p>
        <h1>Studio unavailable</h1>
        <p>We could not open this part of the studio. Refresh the page to try again.</p>
        <button className="button route-state__action" onClick={() => window.location.reload()} type="button">
          Refresh page
        </button>
      </div>
    </main>
  )
}

export class StudioErrorBoundary extends Component<{ children: ReactNode }, StudioErrorBoundaryState> {
  state: StudioErrorBoundaryState = { failed: false }

  static getDerivedStateFromError(): StudioErrorBoundaryState {
    return { failed: true }
  }

  render() {
    if (this.state.failed) {
      return <StudioUnavailable />
    }
    return this.props.children
  }
}

const LazyProjectRoute = lazy(() => import('../features/project/ProjectRoute').then((route) => ({ default: route.ProjectRoute })))
const LazyLiveProjectRoute = lazy(() => import('../features/project/ProjectRoute').then((route) => ({ default: route.LiveProjectRoute })))
const LazyNotFoundPage = lazy(() => import('../features/project/ProjectRoute').then((route) => ({ default: route.NotFoundPage })))
const LazySubject = lazy(() => import('../features/subject/SubjectWorkspace').then((route) => ({ default: route.SubjectWorkspace })))
const LazyMotions = lazy(() => import('../features/motions/MotionWorkspace').then((route) => ({ default: route.MotionWorkspace })))
const LazyRelease = lazy(() => import('../features/release/ReleaseWorkspace').then((route) => ({ default: route.ReleaseWorkspace })))
const LazyHistory = lazy(() => import('../features/project/ProjectHistory').then((route) => ({ default: route.ProjectHistory })))
const LazyFlight = lazy(() => import('../features/flights/FlightRoute').then((route) => ({ default: route.FlightRoute })))
const LazyMovie = lazy(() => import('../features/movies/MovieRoute').then((route) => ({ default: route.MovieRoute })))
const LazyAudio = lazy(() => import('../features/audio/AudioRoute').then((route) => ({ default: route.AudioRoute })))
const LazyApiKeys = lazy(() => import('../features/auth/ApiKeysPage').then((route) => ({ default: route.ApiKeysPage })))
const LazyMachines = lazy(() => import('../features/auth/MachinesPage').then((route) => ({ default: route.MachinesPage })))
const LazyAccessRequests = lazy(() => import('../features/auth/AccessRequestsPage').then((route) => ({ default: route.AccessRequestsPage })))
const LazyDeleteAccount = lazy(() => import('../features/auth/DeleteAccountPage').then((route) => ({ default: route.DeleteAccountPage })))

export function StudioRoutes({ repository }: { repository: ProjectRepository }) {
  return (
    <QueryClientProvider client={queryClient}><StudioErrorBoundary>
      <Suspense fallback={<RouteFallback />}>
        <Routes>
          <Route path="/" element={<Home resume />} />
          <Route path="/projects" element={<Home />} />
          <Route path="/projects/new" element={<NewProject />} />
          <Route path="/new" element={<NewFromTarget />} />
          <Route path="/movies/:movieId/:step?" element={<LazyMovie />} />
          <Route path="/flights/:flightId" element={<LazyFlight />} />
          <Route path="/audio/:setId" element={<LazyAudio />} />
          <Route path="/account/api-keys" element={<LazyApiKeys />} />
          <Route path="/account/machines" element={<LazyMachines />} />
          <Route path="/account/delete" element={<LazyDeleteAccount />} />
          {hostedService && <Route path="/account/access-requests" element={<LazyAccessRequests />} />}
          <Route path="/projects/:projectId" element={repository === previewProjectRepository ? <LazyLiveProjectRoute /> : <LazyProjectRoute repository={repository} />}>
            <Route index element={<Navigate replace to="subject" />} />
            <Route path="subject" element={<WorkspaceRoute component={LazySubject} />} />
            <Route path="motions" element={<WorkspaceRoute component={LazyMotions} />} />
            {/* Quality is folded into Release (studio-ux-flow.md section 5): set-level findings
                and waivers now live on the Release page, so the old Quality destination just
                redirects there. */}
            <Route path="quality" element={<Navigate replace to="../exports" />} />
            <Route path="exports" element={<WorkspaceRoute component={LazyRelease} />} />
            <Route path="history" element={<WorkspaceRoute component={LazyHistory} />} />
          </Route>
          <Route path="*" element={<LazyNotFoundPage />} />
        </Routes>
      </Suspense>
    </StudioErrorBoundary></QueryClientProvider>
  )
}

function ProjectOnboarding({ onCancel }: { onCancel?: () => void } = {}) {
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'animation' | 'movie' | 'flight' | 'audio'>('animation')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      if (kind === 'movie') {
        const movie = await movieApi.create(name.trim())
        navigate(`/movies/${movie.movie_id}/story`, { replace: true })
      } else if (kind === 'flight') {
        const flight = await flightApi.create(name.trim())
        navigate(`/flights/${flight.flight_id}`, { replace: true })
      } else if (kind === 'audio') {
        const audioSet = await audioApi.create(name.trim())
        navigate(`/audio/${audioSet.set_id}`, { replace: true })
      } else {
        const project = await studioApi.createProject(name.trim())
        navigate(`/projects/${project.project_id}/subject`, { replace: true })
      }
    } catch {
      setError('The project could not be created. Try again.')
      setSubmitting(false)
    }
  }
  if (kind === 'animation') return <ArtFirstOnboarding account={<AccountMenu />} onCancel={onCancel} onMovie={() => setKind('movie')} onFlight={() => setKind('flight')} onAudio={() => setKind('audio')} />
  return <main className="route-state"><div className="route-state__card">
    <div className="project-home__header"><p className="eyebrow">Magic Studios</p><AccountMenu /></div><h1>{onCancel ? 'Create a new project' : 'Create your first project'}</h1>
    <form onSubmit={submit}>
      <fieldset className="movie-product-options" disabled={submitting} aria-label="Project type">
        <label><input type="radio" name="project-type" value="animation" checked={false} onChange={() => setKind('animation')} />Animation<small>Build a consistent character animation set.</small></label>
        <label><input type="radio" name="project-type" value="movie" checked={kind === 'movie'} onChange={() => setKind('movie')} />Movie<small>Make a coherent film, a few clips at a time.</small></label>
        <label><input type="radio" name="project-type" value="flight" checked={kind === 'flight'} onChange={() => setKind('flight')} />Scene flight<small>Fly the camera between location images, each leg pinned to both.</small></label>
        <label><input type="radio" name="project-type" value="audio" checked={kind === 'audio'} onChange={() => setKind('audio')} />Audio<small>Gather sound effects, ambience and music from free libraries or your files, review them and release a licensed pack.</small></label>
      </fieldset>
      <label htmlFor="project-name">Project name</label>
      <input id="project-name" value={name} onChange={(event) => setName(event.target.value)} required />
      <button className="button route-state__action" disabled={submitting || !name.trim()} type="submit">{submitting ? 'Creating…' : 'Create project'}</button>
      {onCancel && <button className="button" type="button" onClick={onCancel}>Cancel</button>}
    </form>{error && <p role="alert">{error}</p>}
  </div></main>
}

/** `/` (T-project-home): a returning user's own projects, or the onboarding
 * form when they have none yet. "New project" reopens the same onboarding
 * form without leaving the route. */
function NewProject() {
  const navigate = useNavigate()
  return <ProjectOnboarding onCancel={() => navigate('/')} />
}

function Home({ resume = false }: { resume?: boolean }) {
  const session = useStudioSession()
  const projects = useQuery({ queryKey: ['projects'], queryFn: studioApi.listProjects })
  const movies = useQuery({ queryKey: ['movies'], queryFn: movieApi.list, retry: false })
  const flights = useQuery({ queryKey: ['scene-flights'], queryFn: flightApi.list, retry: false })
  const audioSets = useQuery({ queryKey: ['audio-sets'], queryFn: audioApi.list, retry: false })
  const [creating, setCreating] = useState(false)
  if (projects.isPending || movies.isPending || flights.isPending || audioSets.isPending) return <RouteFallback />
  const list = projects.isError ? [] : projects.data
  const recent = session && resume && !creating ? availableRecentProject(
    readRecentProject(session.actorId), list.map((project) => project.project_id), (movies.data ?? []).map((movie) => movie.movie_id),
  ) : null
  if (recent) return <Navigate replace to={recent} />
  const others = !!movies.data?.length || !!flights.data?.length || !!audioSets.data?.length
  if (creating || (list.length === 0 && !others && !movies.isError)) {
    return <ProjectOnboarding onCancel={list.length > 0 || others ? () => setCreating(false) : undefined} />
  }
  return <ProjectHome projects={list} movies={movies.data ?? []} flights={flights.data ?? []} audioSets={audioSets.data ?? []} moviesError={movies.isError} onNewProject={() => setCreating(true)} onRepaired={() => projects.refetch()} onDeleted={() => { void projects.refetch(); void movies.refetch(); void flights.refetch(); void audioSets.refetch() }} />
}

/** `/new?target=&entity_kind=&entity_id=&name=&slot=` (Part A deep link): resolves `target`
 * against `GET /studio/targets` (no target's name is hard-coded here), then binds an existing
 * project to that external subject if one is already bound, otherwise creates one already bound
 * and pre-populated with the requested (or recommended) motions, then opens Motions with that
 * motion selected and a one-line "Bound to <target> character …" notice. */
export function NewFromTarget() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const [failure, setFailure] = useState<string | null>(null)
  const startedRef = useRef(false)
  useEffect(() => {
    // StrictMode double-invokes effects in development, running setup, cleanup, then setup again
    // on the *same* component instance (refs survive that cleanup) -- so a plain cancellation
    // flag flipped in the cleanup would silently swallow the real run's own navigate() call.
    // Guard with a ref instead: run the lookup-then-create sequence at most once per mount, since
    // a second create would 409 against the first's own binding.
    if (startedRef.current) return
    startedRef.current = true
    async function run() {
      const targetSystem = params.get('target')
      const entityKind = params.get('entity_kind')
      const entityId = params.get('entity_id')
      const name = (params.get('name') ?? '').trim() || 'Character'
      const slotParam = params.get('slot')
      if (!targetSystem || !entityKind || !entityId) {
        setFailure('This link is missing a valid character to bind to.')
        return
      }
      let target
      try {
        target = (await studioApi.targets()).find((item) => item.system === targetSystem)
      } catch {
        setFailure('The available targets could not be looked up.')
        return
      }
      if (!target || !target.entity_kinds.includes(entityKind)) {
        setFailure('This link is missing a valid character to bind to.')
        return
      }
      const preset = slotParam ? presetForTargetSlot(target, slotParam) : undefined
      if (slotParam && !preset) {
        setFailure(`"${slotParam}" is not a motion slot ${target.display_name} recognizes.`)
        return
      }
      const notice = `Bound to ${target.display_name} character ${name}`
      try {
        const existing = await studioApi.projectForExternalSubject(targetSystem, entityKind, entityId)
        navigate(`/projects/${existing.project_id}/motions`, { replace: true, state: { handoffNotice: notice, selectMotionId: preset } })
        return
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) {
          setFailure(`The ${target.display_name} character could not be looked up.`)
          return
        }
      }
      try {
        const project = await studioApi.createProject(name, { system: targetSystem, entity_kind: entityKind, entity_id: entityId, display_name: name }, preset ? [preset] : [])
        navigate(`/projects/${project.project_id}/motions`, { replace: true, state: { handoffNotice: notice, selectMotionId: preset } })
      } catch {
        setFailure(`The project could not be created for this ${target.display_name} character.`)
      }
    }
    run()
  }, [params, navigate])
  if (failure) {
    return <main className="route-state"><div className="route-state__card">
      <p className="eyebrow">Magic Studios</p><h1>Could not open project</h1><p role="alert">{failure}</p>
    </div></main>
  }
  return <RouteFallback />
}

function WorkspaceRoute({ component: Component }: { component: ComponentType<{ projectId: string }> }) {
  const { projectId = 'project-1' } = useParams()
  return <Component projectId={projectId} />
}

export function App() {
  return (
    <SentryErrorBoundary fallback={<StudioUnavailable />}>
      <Suspense fallback={<RouteFallback />}>
        <BrowserRouter>
          <Routes>
            {/* `/login` and the public pages sit outside the session gate -- gating `/login` would loop. */}
            <Route path="/login" element={<LoginPage />} />
            {/* Cost (once Pricing) is now a section of About; keep old links working. */}
            <Route path="/cost" element={<Navigate replace to="/about#cost" />} />
            <Route path="/pricing" element={<Navigate replace to="/about#cost" />} />
            {/* FastAPI's old documentation URLs; the API serves the reference under /studio/. */}
            <Route path="/docs" element={<ServerPageRedirect to="/studio/docs" />} />
            <Route path="/redoc" element={<ServerPageRedirect to="/studio/reference" />} />
            <Route path="/storage-policy" element={<StoragePolicy />} />
            <Route path="/welcome" element={<WelcomePage />} />
            <Route path="/about" element={<AboutPage />} />
            <Route path="/guide" element={<GuidePage />} />
            <Route path="/privacy" element={<PrivacyPolicy />} />
            <Route path="/terms" element={<TermsOfService />} />
            {hostedService && <>
              <Route path="/support" element={<SupportPage />} />
              <Route path="/support/thanks" element={<SupportThanksPage />} />
              <Route path="/donate" element={<LegacyDonateRedirect to="/support" />} />
              <Route path="/donate/thanks" element={<LegacyDonateRedirect to="/support/thanks" />} />
            </>}
            <Route path="*" element={<StudioAuthGate><StudioRoutes repository={previewProjectRepository} /></StudioAuthGate>} />
          </Routes>
        </BrowserRouter>
      </Suspense>
    </SentryErrorBoundary>
  )
}

export function StudioAuthGate({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<StudioSession | null>(null)
  useEffect(() => {
    let active = true
    fetchStudioSession().then((result) => {
      if (!active) return
      if (result) setSession(result)
      else redirectToLogin()
    })
    return () => { active = false }
  }, [])
  return session ? <StudioSessionContext.Provider value={session}>{children}</StudioSessionContext.Provider> : <RouteFallback />
}

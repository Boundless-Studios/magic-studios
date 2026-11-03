import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, Navigate, useNavigate, useParams } from 'react-router-dom'
import { ApiError, studioApi } from '../../api/client'
import { GpuStripView } from '../project/ProjectActivity'
import { stripsFromWorkers } from '../project/gpuStatus'
import { ProjectShell } from '../project/ProjectShell'
import { MovieStory } from './MovieStory'
import { MovieCut, MovieReleaseWorkspace, MovieHistory } from './MovieCut'
import { movieActive, movieApi, type Movie, type MovieCapabilities } from './movieApi'
import './movies.css'

export interface MovieWorkspaceProps {
  movie: Movie; capabilities?: MovieCapabilities; busy: boolean
  run: (action: () => Promise<Movie>) => Promise<boolean>
}
const steps = ['story', 'shots', 'cut', 'review', 'release', 'history'] as const
const navigation = [ { label: 'Movie', path: 'story' }, { label: 'Review', path: 'review' }, { label: 'Release & export', path: 'release' }, { label: 'History', path: 'history' } ]

function MoviePoolStatus() {
  const workers = useQuery({ queryKey: ['pool-workers'], queryFn: studioApi.poolWorkers, refetchInterval: 15000, retry: false })
  const strips = stripsFromWorkers(workers.data?.workers ?? [])
  return <Link to="/account/machines" aria-label="GPU network and machines" className="gpu-strips">
    {strips.map((strip) => <GpuStripView key={strip.key} strip={strip} />)}
    {!strips.length && <span className="gpu-strip gpu-strip--empty">{workers.isPending ? 'Checking GPU network…' : workers.isError ? 'GPU status unavailable' : 'No GPU online'}</span>}
  </Link>
}

export function MovieRoute() {
  const { movieId = '', step = 'story' } = useParams()
  const navigate = useNavigate()
  const client = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const running = useRef(false)
  const movie = useQuery({ queryKey: ['movie', movieId], queryFn: async () => {
    const current = await movieApi.get(movieId)
    return movieActive(current) ? movieApi.reconcile(movieId) : current
  }, refetchInterval: (query) => !running.current && query.state.data && movieActive(query.state.data) ? 3000 : false })
  const capabilities = useQuery({ queryKey: ['movie-capabilities'], queryFn: movieApi.capabilities })
  useEffect(() => { const main = document.getElementById('main-content'); if (main) main.scrollTop = 0 }, [step, movie.isPending])

  async function run(action: () => Promise<Movie>): Promise<boolean> {
    if (running.current) return false
    running.current = true; setBusy(true); setError(null)
    await client.cancelQueries({ queryKey: ['movie', movieId] })
    try {
      const updated = await action()
      client.setQueryData(['movie', movieId], updated)
      await client.invalidateQueries({ queryKey: ['movie-history', movieId] })
      return true
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The movie could not be updated.')
      if (failure instanceof ApiError && failure.status === 409) setConflict(true)
      return false
    } finally { running.current = false; setBusy(false) }
  }

  if (step === 'cut') return <Navigate replace to={`/movies/${movieId}/review`} />
  if (step === 'shots') return <Navigate replace to={`/movies/${movieId}/story`} />
  if (!steps.includes(step as typeof steps[number])) return <Navigate replace to={`/movies/${movieId}/story`} />
  if (movie.isPending) return <main className="route-state" role="status">Opening movie studio</main>
  if (!movie.data) return <main className="route-state"><div><h1>Movie unavailable</h1><p>{movie.error?.message}</p><Link to="/">Return to studio</Link></div></main>
  const data = movie.data
  const props = { movie: data, capabilities: capabilities.data, busy: busy || conflict, run }
  return <ProjectShell
    activity={<MoviePoolStatus />}
    project={{ id: movieId, name: data.name, saveState: busy ? 'Saving' : 'Saved', releaseReady: data.assembly?.status === 'succeeded', exportReady: data.releases.length > 0 }}
    workflow={{ name: 'Movie Studio', destinations: navigation.map((item) => ({ ...item, path: `/movies/${movieId}/${item.path}` })), releaseHint: 'Review and save your movie first.', exportHint: 'Release your reviewed movie to export.' }}
    onExport={() => navigate(`/movies/${movieId}/release`)} onRelease={() => navigate(`/movies/${movieId}/release`)}
  >
    <div className="movie-workspace">

      {(error || movie.error) && <div className="movie-notice movie-notice--error" role="alert">{error || movie.error?.message}{conflict && <button className="button" onClick={() => window.location.reload()}>Reload latest movie</button>}</div>}
      {capabilities.error && <p className="movie-notice" role="alert">Worker availability could not be checked. Planning and editing remain available.</p>}
      {step === 'story' && <MovieStory {...props} />}
      {step === 'review' && <MovieCut {...props} />}
      {step === 'release' && <MovieReleaseWorkspace {...props} />}
      {step === 'history' && <MovieHistory movie={data} />}
    </div>
  </ProjectShell>
}

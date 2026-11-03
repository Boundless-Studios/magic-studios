import { useState } from 'react'
import { Link } from 'react-router-dom'
import { studioApi } from '../../api/client'
import type { ProjectSummary } from '../../api/types'
import { activityCountsLabel, activityLastLabel } from './activityStats'
import { movieActive, type Movie } from '../movies/movieApi'
import type { FlightSummary } from '../flights/flightApi'
import { audioApi, type AudioSet } from '../audio/audioApi'
import { AccountMenu } from '../auth/AccountMenu'
import { DeleteItem } from '../../components/DeleteItem'

function subjectStateLabel(state: ProjectSummary['subject_state']): string {
  if (state === 'approved') return 'Subject approved'
  if (state === 'pending') return 'Subject pending'
  return 'No subject yet'
}

/** A project whose current revision failed to validate (MAGIC-VIDEO-EDITOR-BACKEND-A):
 * the list still shows it -- by raw name, since the manifest cannot be read --
 * with a Repair button instead of the usual link into Subject/Motions. */
function UnreadableProjectCard({ project, onRepaired }: { project: ProjectSummary; onRepaired: () => void }) {
  const [repairing, setRepairing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [restoredTo, setRestoredTo] = useState<number | null>(null)

  async function repair() {
    setRepairing(true)
    setError(null)
    try {
      const report = await studioApi.repairProject(project.project_id)
      if (report.repaired && report.new_revision !== null) {
        setRestoredTo(report.new_revision)
      }
      onRepaired()
    } catch {
      setError('The project could not be repaired. Try again.')
    } finally {
      setRepairing(false)
    }
  }

  return <div className="project-home__unreadable">
    <strong>{project.name}</strong>
    <span>Needs repair: the last saved version could not be read</span>
    <ExpiryNote expiresAt={project.expires_at} action="repair" />
    <button className="button" type="button" onClick={repair} disabled={repairing}>
      {repairing ? 'Repairing…' : 'Repair'}
    </button>
    {restoredTo !== null && <p role="status">Restored to revision {restoredTo}</p>}
    {error && <p role="alert">{error}</p>}
  </div>
}

/** The project home list (T-project-home): a returning user's own projects,
 * newest activity first, so they can pick up where they left off instead of
 * hunting for a stale deep link. Each row routes back into Subject (nothing
 * approved yet) or Motions (subject already approved). A project whose
 * current revision is unreadable is not a link -- it offers Repair instead
 * (MAGIC-VIDEO-EDITOR-BACKEND-A). */
/** Unreleased work nobody opens is deleted for inactivity; the list warns first. */
function ExpiryNote({ expiresAt, action = 'open' }: { expiresAt?: string | null; action?: 'open' | 'repair' }) {
  if (!expiresAt) return null
  const date = new Date(expiresAt).toLocaleDateString([], { dateStyle: 'medium' })
  return <span className="project-home__expiry">Deleted on {date} unless you {action} it</span>
}

export function ProjectHome({ projects, movies = [], flights = [], audioSets = [], moviesError = false, onNewProject, onRepaired, onDeleted = () => {} }: { projects: ProjectSummary[]; movies?: Movie[]; flights?: FlightSummary[]; audioSets?: AudioSet[]; moviesError?: boolean; onNewProject: () => void; onRepaired: () => void; onDeleted?: () => void }) {
  return <main className="route-state"><div className="route-state__card project-home">
    <p className="eyebrow">Magic Studios</p>
    <div className="project-home__header"><h1>Your projects</h1><AccountMenu /></div>
    <button className="button route-state__action" type="button" onClick={onNewProject}>New project</button>
    {moviesError && <p role="alert">Your movies could not be loaded. Refresh to try again.</p>}
    <ul className="project-home__list">
      {movies.map((movie) => <li key={movie.movie_id}><Link to={`/movies/${movie.movie_id}/${movie.releases.length ? 'release' : movie.shots.length ? 'shots' : 'story'}`}><strong>{movie.name}</strong><span>Movie · {movie.shots.length} shots · {movie.releases.length} releases</span><span className="project-home__activity">{movieActive(movie) ? 'Work in progress' : 'Ready to continue'}</span><ExpiryNote expiresAt={movie.expires_at} /></Link><DeleteItem kind="movie" name={movie.name} onDelete={() => studioApi.deleteMovie(movie.movie_id, movie.name)} onDeleted={onDeleted} /></li>)}
      {flights.map((flight) => <li key={flight.flight_id}><Link to={`/flights/${flight.flight_id}`}><strong>{flight.name}</strong><span>Scene flight · {flight.leg_count} leg{flight.leg_count === 1 ? '' : 's'}</span><span className="project-home__activity">{flight.active_takes ? 'Work in progress' : 'Ready to continue'}</span><ExpiryNote expiresAt={flight.expires_at} /></Link><DeleteItem kind="scene flight" name={flight.name} onDelete={() => studioApi.deleteFlight(flight.flight_id, flight.name)} onDeleted={onDeleted} /></li>)}
      {audioSets.map((audioSet) => <li key={audioSet.set_id}><Link to={`/audio/${audioSet.set_id}`}><strong>{audioSet.name}</strong><span>Audio · {audioSet.cues.length} cue{audioSet.cues.length === 1 ? '' : 's'} · {audioSet.releases.length} release{audioSet.releases.length === 1 ? '' : 's'}</span><span className="project-home__activity">{audioSet.candidates.length} candidate{audioSet.candidates.length === 1 ? '' : 's'}</span></Link><DeleteItem kind="audio set" name={audioSet.name} onDelete={() => audioApi.remove(audioSet.set_id, audioSet.name)} onDeleted={onDeleted} /></li>)}
      {projects.map((project) => {
        if (project.unreadable) {
          return <li key={project.project_id}><UnreadableProjectCard project={project} onRepaired={onRepaired} /><DeleteItem kind="project" name={project.name} onDelete={() => studioApi.deleteProject(project.project_id, project.name)} onDeleted={onDeleted} /></li>
        }
        const target = project.subject_state === 'approved' ? 'motions' : 'subject'
        const counts = { running: project.activity.running, failed: project.activity.failed, completed: project.activity.completed, lastActivityAt: project.activity.last_activity_at }
        return <li key={project.project_id}>
          <Link to={`/projects/${project.project_id}/${target}`}>
            <strong>{project.name}</strong>
            <span>{subjectStateLabel(project.subject_state)} · {project.motion_count} motion{project.motion_count === 1 ? '' : 's'} · {project.release_count} release{project.release_count === 1 ? '' : 's'}</span>
            <span className="project-home__activity">{activityCountsLabel(counts)} · {activityLastLabel(counts)}</span>
            <time dateTime={project.updated_at}>Updated {new Date(project.updated_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</time>
            <ExpiryNote expiresAt={project.expires_at} />
          </Link>
          <DeleteItem kind="project" name={project.name} onDelete={() => studioApi.deleteProject(project.project_id, project.name)} onDeleted={onDeleted} />
        </li>
      })}
    </ul>
  </div></main>
}

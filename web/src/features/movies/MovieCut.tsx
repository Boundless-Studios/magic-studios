/* eslint-disable jsx-a11y/media-has-caption -- This editor previews the user's assembled media before a caption track has been authored. */
import { useQuery } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '../../components/ui/Button'
import { MovieClipPlayer } from './MovieClipPlayer'
import { MAX_SHOT_SECONDS, movieApi, type CutClip, type Movie } from './movieApi'
import type { MovieWorkspaceProps } from './MovieRoute'
import { MovieSequence } from './MovieSequence'
import { MovieSoundtracks } from './MovieSoundtracks'
import { useDraftRevision } from './useDraftRevision'

interface DraftClip { id: string; clip: CutClip }
const transitions = [{ value: 0, label: 'Cut' }, { value: 0.5, label: 'Smooth dissolve · 0.5 seconds' }, { value: 1, label: 'Slow dissolve · 1 second' }]
// The server omits a hard cut's transition and an unmuted clip's mute flag.
const normalized = (clips: CutClip[]) => JSON.stringify(clips.map((clip) => ({ ...clip, transition_seconds: clip.transition_seconds ?? 0, mute: clip.mute ?? false })))

export function MovieCut({ movie, busy, run, capabilities }: MovieWorkspaceProps) {
  // Until the user edits, the sequence follows the server: the saved cut, or
  // the selected takes in story order when nothing has been saved yet.
  const [draft, setDraft] = useState<DraftClip[] | null>(null)
  const [activeId, setActiveId] = useState<string | null>(null)
  const [moveError, setMoveError] = useState<string | null>(null)
  const requestId = useRef(crypto.randomUUID())
  const revision = useDraftRevision(movie.cut, movie.revision)
  const fromStory = !movie.cut.length
  // A selection changes the revision, so the server's cut is read again.
  const selected = useQuery({ queryKey: ['movie-selected-cut', movie.movie_id, movie.revision], queryFn: () => movieApi.selectedCut(movie.movie_id) })
  const sequence = draft ?? (fromStory ? selected.data ?? [] : movie.cut).map((clip, index) => ({ id: `clip-${index}`, clip }))
  const clips = sequence.map((item) => item.clip)
  const unsaved = draft !== null ? normalized(clips) !== normalized(movie.cut) : fromStory && clips.length > 0
  const activeIndex = Math.max(0, sequence.findIndex((item) => item.id === activeId))
  const active = sequence[activeIndex]
  const takeOf = (clip: CutClip) => movie.shots.find((item) => item.shot_id === clip.shot_id)?.takes.find((item) => item.take_id === clip.take_id)
  const shotOf = (clip: CutClip) => movie.shots.find((item) => item.shot_id === clip.shot_id)
  const duration = clips.reduce((sum, clip, index) => {
    const next = clips[index + 1]
    const overlap = next ? Math.floor(Math.min(clip.transition_seconds ?? 0, (clip.out_seconds - clip.in_seconds) / 2, (next.out_seconds - next.in_seconds) / 2) * 24) / 24 : 0
    return sum + clip.out_seconds - clip.in_seconds - overlap
  }, 0)
  const problem = (clip: CutClip) => {
    const shot = shotOf(clip)
    const take = takeOf(clip)
    if (!take || take.stale || take.status !== 'succeeded') return 'Outdated take'
    if (clip.in_seconds < 0 || clip.out_seconds <= clip.in_seconds || clip.out_seconds > (take.duration_seconds ?? shot?.duration_seconds ?? 0)) return 'Check the trim'
    return null
  }
  const invalid = clips.some(problem)
  const assembling = ['waiting', 'pending', 'running', 'submitting'].includes(movie.assembly?.status ?? '')
  const assembled = !unsaved && movie.assembly?.status === 'succeeded' && Boolean(movie.assembly.asset_key)
  function edit(next: DraftClip[]) { setDraft(next); setMoveError(null) }
  function patch(index: number, value: Partial<CutClip>) { edit(sequence.map((item, position) => position === index ? { ...item, clip: { ...item.clip, ...value } } : item)) }
  function move(from: number, to: number) {
    const next = [...sequence]
    const [moving] = next.splice(from, 1)
    next.splice(to, 0, moving)
    // A continuation take starts from its source take's final frame.
    const broken = next.some(({ clip }, index) => {
      const source = shotOf(clip)?.continuation_of
      return source && next.some((item) => item.clip.shot_id === source) && !next.slice(0, index).some((item) => item.clip.shot_id === source)
    })
    if (broken) { setMoveError('Keep continuation clips after the clips they continue.'); return false }
    edit(next); return true
  }
  async function saveAndAssemble() {
    let current = movie.revision
    if (unsaved && !await run(async () => { const result = await movieApi.cut(movie.movie_id, { expected_revision: revision.expectedRevision, clips }); revision.committed(result.cut, result.revision); current = result.revision; return result })) return
    setDraft(null)
    if (await run(() => movieApi.assemble(movie.movie_id, current, requestId.current))) requestId.current = crypto.randomUUID()
  }
  const playerClips = clips.map((clip) => { const take = takeOf(clip); return { name: shotOf(clip)?.name ?? 'Missing shot', src: take?.asset_key ? movieApi.assetUrl(movie.movie_id, take.asset_key) : '', in_seconds: clip.in_seconds, out_seconds: clip.out_seconds, mute: clip.mute } })
  const noTakes = !clips.length && !movie.shots.some((shot) => shot.takes.some((take) => take.status === 'succeeded'))
  const loading = fromStory && draft === null && selected.isPending
  const unreadable = fromStory && draft === null && selected.isError
  return <section aria-labelledby="movie-cut-title">
    <header className="workspace-heading movie-heading"><div><p className="eyebrow">2 · Review</p><h2 id="movie-cut-title">Review your movie</h2><p>Put your clips in order, trim them, and watch the movie.</p></div><Link className="button" to={`/movies/${movie.movie_id}/release`}>Release & export →</Link></header>
    {loading ? <p role="status" className="movie-muted">Loading your clips…</p> : unreadable ? <p role="alert" className="movie-notice movie-notice--error">Your clips could not be loaded. Reload the page to try again.</p> : noTakes ? <div className="movie-empty"><span className="movie-empty-mark" aria-hidden="true">▷</span><h3>No clips yet</h3><p>Generate your shots on the Movie page. Each shot’s selected take appears here as a clip.</p><Link className="button" to={`/movies/${movie.movie_id}/story`}>Go to Movie</Link></div>
      : <div className="movie-cut-layout"><div className="movie-cut-column"><div className="movie-cut-main">
        <div className="movie-preview">{assembled ? <video key={movie.assembly!.asset_key} controls playsInline preload="metadata" src={movieApi.assetUrl(movie.movie_id, movie.assembly!.asset_key!)} aria-label="Assembled movie preview" />
          : clips.length ? <MovieClipPlayer clips={playerClips} index={activeIndex} onIndex={(index) => setActiveId(sequence[index]?.id ?? null)} />
            : <div className="movie-preview-empty"><h3>No clips in the sequence</h3><p>Add your selected takes to start.</p></div>}</div>
        <p className="movie-muted">{assembled ? 'Assembled movie with dissolves and music. Change the sequence to preview clips again.' : assembling ? 'Assembling your movie… The clips play in order until it is ready.' : 'Clips play in order with hard cuts. Assemble to add dissolves and music.'}</p>
        {movie.assembly?.error && <p className="movie-notice movie-notice--error" role="alert">{movie.assembly.error}</p>}
        {assembled && (movie.assembly!.joins?.length ?? 0) > 0 && <div className="movie-joins" role="group" aria-label="Joins">
          <div className="movie-section-title"><h4>Joins</h4><span className="movie-muted">{movie.assembly!.joins!.filter((j) => j.diff > j.limit).length === 0 ? 'Every cut sits within the reference range.' : `${movie.assembly!.joins!.filter((j) => j.diff > j.limit).length} cut${movie.assembly!.joins!.filter((j) => j.diff > j.limit).length === 1 ? '' : 's'} read as a jump.`}</span></div>
          <ol className="movie-join-list">{movie.assembly!.joins!.map((join) => {
            const next = movie.assembly!.clips?.[join.index + 1] ?? movie.cut[join.index + 1]
            const shot = movie.shots.find((item) => item.shot_id === next?.shot_id)
            const over = join.diff > join.limit
            return <li key={join.index} className={over ? 'is-over' : undefined}><span>{String(join.index + 1).padStart(2, '0')} · {join.at_seconds.toFixed(1)}s</span><span>{join.diff.toFixed(4)} / {join.limit}</span>{over ? <span role="alert">Jump into {shot?.name ?? 'the next shot'} — generate another take of it.</span> : <span className="movie-muted">smooth</span>}</li>
          })}</ol>
        </div>}
        <div className="movie-batch-bar"><div><strong>{clips.length} clips · {duration.toFixed(1)} seconds</strong><span>{assembled ? 'Saved and assembled.' : `${unsaved ? 'Unsaved changes.' : 'Saved.'} Assembling reuses your clips and generates no new video.`}</span></div>
          {!assembled && <Button variant="primary" disabled={busy || assembling || !clips.length || invalid} onClick={() => void saveAndAssemble()}>{assembling ? 'Assembling…' : unsaved ? 'Save and assemble' : 'Assemble movie'}</Button>}</div>
        {fromStory && draft === null && clips.length > 0 && <p className="movie-notice">These are your selected takes in story order. Save and assemble to keep this cut.</p>}
        {invalid && <p className="movie-notice movie-notice--error" role="alert">Some clips use an outdated take or a trim past the clip’s end. Fix or remove them before saving.</p>}
      </div>
      <div className="movie-cut-sound"><MovieSoundtracks movie={movie} busy={busy} run={run} capabilities={capabilities} /></div></div>
      <MovieSequence noun="clip" activeId={active?.id} disabled={busy} keepHint="Save and assemble to keep this order." footer="Saving keeps this order. Your takes are not changed."
        help="Drag clips into order. Click a clip to trim it. With a clip focused, use ↑ or ↓ to move it."
        empty={<p>No clips yet. Choose a take for each shot on the <Link to={`/movies/${movie.movie_id}/story`}>Movie page</Link>.</p>}
        items={sequence.map(({ id, clip }) => { const take = takeOf(clip); const shot = shotOf(clip); const image = take?.opening_asset_key ?? shot?.opening_asset_key ?? take?.final_frame_key; return { id, name: shot?.name ?? 'Missing shot', detail: `${(clip.out_seconds - clip.in_seconds).toFixed(1)}s${problem(clip) ? ` · ${problem(clip)}` : ''}`, image: image ? movieApi.assetUrl(movie.movie_id, image) : undefined } })}
        onMove={move} onOpen={setActiveId}>
        {moveError && <p role="alert" className="movie-notice movie-notice--error">{moveError}</p>}
        {active && <div className="movie-cut-clip" role="group" aria-label={`Cut clip ${activeIndex + 1}`}>
          <div className="movie-section-title"><h4>{String(activeIndex + 1).padStart(2, '0')} · {shotOf(active.clip)?.name ?? 'Missing shot'}</h4><span>{(active.clip.out_seconds - active.clip.in_seconds).toFixed(1)}s</span></div>
          <div className="movie-fields-row"><label className="movie-field">Trim start (seconds)<input disabled={busy} type="number" min={0} max={active.clip.out_seconds} step={0.1} value={active.clip.in_seconds} onChange={(e) => patch(activeIndex, { in_seconds: Number(e.target.value) })} /></label><label className="movie-field">Trim end (seconds)<input disabled={busy} type="number" min={active.clip.in_seconds} max={takeOf(active.clip)?.duration_seconds ?? shotOf(active.clip)?.duration_seconds ?? MAX_SHOT_SECONDS} step={0.1} value={active.clip.out_seconds} onChange={(e) => patch(activeIndex, { out_seconds: Number(e.target.value) })} /></label></div>
          {activeIndex < clips.length - 1 && <label className="movie-field">Transition to next clip<select disabled={busy} value={active.clip.transition_seconds ?? 0} onChange={(event) => patch(activeIndex, { transition_seconds: Number(event.target.value) })}>{[...transitions, ...(transitions.some((item) => item.value === (active.clip.transition_seconds ?? 0)) ? [] : [{ value: active.clip.transition_seconds!, label: `Dissolve · ${active.clip.transition_seconds!.toFixed(2)} seconds` }])].map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select><span className="movie-muted">A dissolve overlaps the two clips.</span></label>}
          <div className="movie-actions"><Button disabled={busy} onClick={() => { edit(sequence.filter((_, position) => position !== activeIndex)); setActiveId(null) }}>Remove clip</Button></div>
        </div>}
        <Button disabled={busy || !selected.data} onClick={() => edit((selected.data ?? []).map((clip) => ({ id: crypto.randomUUID(), clip })))}>Reset to selected takes</Button>
      </MovieSequence>
    </div>}
  </section>
}

export function MovieReleaseWorkspace({ movie, busy, run }: MovieWorkspaceProps) {
  const [name, setName] = useState(`Cut ${movie.releases.length + 1}`)
  const [reviewed, setReviewed] = useState(false)
  const ready = movie.assembly?.status === 'succeeded' && Boolean(movie.assembly.asset_key)
  return <section aria-labelledby="movie-release-title">
    <header className="workspace-heading movie-heading"><div><p className="eyebrow">3 · Release & export</p><h2 id="movie-release-title">Share the finished story</h2><p>Release a reviewed cut. Your original clips and previous releases stay available.</p></div></header>
    <div className="movie-cut-layout"><div className="movie-preview">{ready && movie.assembly?.asset_key ? <video controls playsInline preload="metadata" src={movieApi.assetUrl(movie.movie_id, movie.assembly.asset_key)} aria-label="Review movie for release" /> : <div className="movie-preview-empty"><h3>Assemble your movie first</h3><p>A release needs a finished, reviewed cut.</p><Link className="button" to={`/movies/${movie.movie_id}/review`}>Review movie</Link></div>}</div>
      <form className="movie-panel movie-release-form" onSubmit={async (event) => { event.preventDefault(); if (await run(() => movieApi.release(movie.movie_id, movie.revision, name))) { setReviewed(false); setName(`Cut ${movie.releases.length + 2}`) } }}>
        <h3>Release this cut</h3><label className="movie-field">Release name<input required maxLength={160} value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label className="movie-check"><input type="checkbox" checked={reviewed} disabled={!ready} onChange={(event) => setReviewed(event.target.checked)} />I reviewed the assembled movie and its continuity.</label>
        <Button type="submit" variant="primary" disabled={busy || !ready || !reviewed || !name.trim()}>Release movie</Button>
        <p className="movie-muted">Each release keeps the exact cut and take choices you approved.</p>
      </form></div>
    <div className="movie-section-title"><h3>Released movies</h3><span>{movie.releases.length} releases</span></div>
    {movie.releases.length === 0 && <p className="movie-muted">Your first release will appear here.</p>}
    <div className="movie-releases">{movie.releases.map((release) => <article className="movie-panel" key={release.release_id}><h3>{release.name}</h3><p>From revision {release.source_revision}</p><a className="button" href={movieApi.downloadReleaseUrl(movie.movie_id, release.release_id)}>Download MP4</a></article>)}</div>
  </section>
}

export function MovieHistory({ movie }: { movie: Movie }) {
  const history = useQuery({ queryKey: ['movie-history', movie.movie_id], queryFn: () => movieApi.history(movie.movie_id) })
  return <section aria-labelledby="movie-history-title"><header className="workspace-heading movie-heading"><div><p className="eyebrow">History</p><h2 id="movie-history-title">The story of this movie</h2><p>Saved plans, takes and cuts, preserved across revisions.</p></div></header>
    {history.isPending && <p role="status">Loading history…</p>}{history.error && <p role="alert">History could not be loaded.</p>}
    <ol className="movie-history">{[...(history.data ?? [])].reverse().map((revision) => <li className="movie-panel" key={revision.revision}><strong>Revision {revision.revision}</strong><span>{revision.shots.length} shots · {revision.shots.reduce((sum, shot) => sum + shot.takes.length, 0)} takes · {revision.cut.length} cut clips · {revision.releases.length} releases</span></li>)}</ol>
  </section>
}

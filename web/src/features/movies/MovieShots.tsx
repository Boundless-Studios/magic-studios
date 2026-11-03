/* eslint-disable jsx-a11y/media-has-caption -- User-generated take previews have no caption source. */
import { useEffect, useRef, useState } from 'react'
import { Button } from '../../components/ui/Button'
import { movieApi, selectedTake, takeActive, type MovieShot, type MovieTake } from './movieApi'
import type { MovieWorkspaceProps } from './MovieRoute'

export function MovieShotMedia(props: MovieWorkspaceProps & { shot: MovieShot; pendingImage?: string | null; onImage: (file: File) => void }) {
  const { movie, shot, pendingImage } = props
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const selected = selectedTake(shot)
  const take = shot.takes.find((item) => item.take_id === previewId) ?? (selected && !selected.stale ? selected : undefined) ?? [...shot.takes].reverse().find((item) => item.status === 'succeeded' && !item.stale) ?? selected
  const image = pendingImage || (shot.opening_asset_key ? movieApi.assetUrl(movie.movie_id, shot.opening_asset_key) : null)
  const active = shot.takes.find(takeActive)
  const composition = Object.values(movie.compositions ?? {}).filter((item) => item.shot_id === shot.shot_id).sort((a, b) => (b.created_revision ?? 0) - (a.created_revision ?? 0))[0]
  return <div className="movie-shot-media">
    <div className="movie-shot-stage">{!pendingImage && take?.status === 'succeeded' && take.asset_key && (!take.opening_asset_key || !shot.opening_asset_key || take.opening_asset_key === shot.opening_asset_key) ? <video key={take.asset_key} src={movieApi.assetUrl(movie.movie_id, take.asset_key)} controls playsInline preload="metadata" aria-label={`${shot.name} preview`} /> : image ? <img src={image} alt={`${shot.name} reference`} /> : <p>{shot.continuation_of ? 'Continues from the previous shot’s final frame.' : 'Add a starting image to bring this shot to life.'}</p>}</div>
    {active && <p role="status">{active.status === 'waiting' ? 'Waiting for the previous shot…' : 'Generating this shot…'}</p>}
    {shot.takes.at(-1)?.status === 'failed' && <p role="alert">{shot.takes.at(-1)?.error || 'This take failed. Adjust the prompt or try again.'}</p>}
    <div className="movie-actions">
      <Button type="submit" form="movie-story-form" value={`shot:${shot.shot_id}`} disabled={props.busy || !!active || !props.capabilities?.generation_available || ['generating', 'assembling'].includes(movie.build?.status ?? '')}>{shot.takes.some((item) => item.status === 'succeeded') ? 'Generate another take' : 'Generate this shot'}</Button>
      {take?.status === 'succeeded' && take.asset_key && <Button onClick={() => setPreviewOpen(true)}>Preview take {shot.takes.indexOf(take) + 1}</Button>}
    </div>
    <details className="movie-media-tools"><summary>Starting image & existing clips</summary>
      {!shot.continuation_of && <div className="movie-actions"><label className="movie-upload">Upload starting image<input type="file" aria-label="Upload starting image" accept="image/png,image/jpeg,image/webp" disabled={props.busy} onChange={(event) => { const file = event.target.files?.[0]; if (file) props.onImage(file); event.target.value = '' }} /></label>
        {props.capabilities?.composition_available && <Button type="submit" form="movie-story-form" value={`compose:${shot.shot_id}`} disabled={props.busy || !shot.opening_prompt.trim() || composition?.status === 'pending'}>{shot.opening_asset_key ? 'Compose another starting image' : 'Compose starting image'}</Button>}
      </div>}
      {composition?.status === 'pending' && <p role="status">Composing starting image…</p>}
      {composition?.error && <p role="alert">{composition.error}</p>}
      <ImportClip {...props} busy={props.busy || !movie.shots.some((item) => item.shot_id === shot.shot_id)} />
    </details>
    {shot.takes.length > 0 && <details className="movie-media-tools"><summary>Takes & generation details ({shot.takes.length})</summary>
      <div className="movie-takes">{shot.takes.map((item, index) => <TakeCard key={item.take_id} {...props} take={item} number={index + 1} onPreview={() => { setPreviewId(item.take_id); setPreviewOpen(true) }} />)}</div>
    </details>}
    {previewOpen && take?.asset_key && <TakePreview name={shot.name} src={movieApi.assetUrl(movie.movie_id, take.asset_key)} onClose={() => setPreviewOpen(false)} />}
  </div>
}

function TakeCard({ movie, shot, take, number, onPreview, busy, run }: MovieWorkspaceProps & { shot: MovieShot; take: MovieTake; number: number; onPreview: () => void }) {
  const selected = shot.selected_take_id === take.take_id
  return <div className={`movie-panel movie-take${selected ? ' is-selected' : ''}`} role="group" aria-label={`Take ${number}`}>
    <div className="movie-section-title"><h4>Take {number}</h4><span>{selected ? 'Selected' : take.status}</span></div>
    {take.stale && <div className="movie-notice"><p>The source shot changed. Review continuity before using this take.</p>{take.status === 'succeeded' && <Button disabled={busy} onClick={() => void run(() => movieApi.acceptContinuity(movie.movie_id, shot.shot_id, take.take_id, movie.revision))}>Accept reviewed continuity</Button>}</div>}
    {take.error && <p role="alert">{take.error}</p>}
    {take.job_id && <details className="movie-generation-details"><summary>Generation details</summary><p>Job {take.job_id}</p>{take.completed_by_worker_id && <p>Rendered by {take.completed_by_worker_id}</p>}</details>}
    <div className="movie-actions">{take.status === 'succeeded' && take.asset_key && <Button onClick={onPreview} aria-label={`Preview take ${number} from history`}>Preview this take</Button>}{take.status === 'succeeded' && <Button disabled={busy || selected || take.stale} variant={selected ? 'secondary' : 'primary'} onClick={() => void run(() => movieApi.select(movie.movie_id, shot.shot_id, movie.revision, take.take_id))}>{selected ? 'Selected take' : 'Use this take'}</Button>}{takeActive(take) && <Button disabled={busy} onClick={() => void run(() => movieApi.cancel(movie.movie_id, shot.shot_id, take.take_id, movie.revision))}>Cancel take</Button>}</div>
  </div>
}

function ImportClip({ movie, shot, busy, run }: MovieWorkspaceProps & { shot: MovieShot }) {
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function upload(file?: File) {
    if (!file) return
    if (file.size > 256 * 1024 * 1024) { setError('Choose an MP4 smaller than 256 MB.'); return }
    setUploading(true); setError(null)
    try {
      const target = await movieApi.upload(movie.movie_id, file)
      await run(() => movieApi.importClip(movie.movie_id, shot.shot_id, target.revision, target.asset_key))
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Clip import failed.') }
    finally { setUploading(false) }
  }
  return <div className="movie-import"><label className="movie-upload">{uploading ? 'Importing…' : 'Import existing clip'}<input type="file" accept="video/mp4" aria-label="Import existing clip" disabled={busy || uploading} onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = '' }} /></label>{error && <p role="alert">{error}</p>}</div>
}

function TakePreview({ name, src, onClose }: { name: string; src: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const element = dialog.current
    const trigger = document.activeElement as HTMLElement | null
    element?.showModal()
    return () => { element?.close(); trigger?.focus() }
  }, [])
  return <dialog ref={dialog} className="movie-take-preview" aria-label={`${name} take preview`} onCancel={onClose}>
    <header className="movie-section-title"><h3>{name} · Take preview</h3><Button onClick={onClose}>Close preview</Button></header>
    <video key={src} src={src} controls autoPlay playsInline aria-label={`${name} take playback`} onError={() => setFailed(true)} />
    {failed && <p role="alert">This video could not be loaded. Close the preview and try again.</p>}
  </dialog>
}

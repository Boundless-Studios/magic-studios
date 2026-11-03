/* eslint-disable jsx-a11y/media-has-caption -- Generated camera flights have no caption source. */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { Button } from '../../components/ui/Button'
import { ProjectShell } from '../project/ProjectShell'
import { draftsFrom, flightActive, flightApi, flightTakeActive, type FlightLeg, type LegDraft, type SceneFlight } from './flightApi'
import '../movies/movies.css'

const percent = (value: number | null) => value === null ? 'not measured' : `${(value * 100).toFixed(1)}%`

export function FlightRoute() {
  const { flightId = '' } = useParams()
  const client = useQueryClient()
  const running = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [drafts, setDrafts] = useState<LegDraft[] | null>(null)
  const flight = useQuery({
    queryKey: ['scene-flight', flightId], queryFn: () => flightApi.get(flightId),
    refetchInterval: (query) => !running.current && query.state.data && flightActive(query.state.data) ? 4000 : false,
  })

  async function run(action: () => Promise<SceneFlight>, resetDrafts = false): Promise<boolean> {
    if (running.current) return false
    running.current = true; setBusy(true); setError(null)
    await client.cancelQueries({ queryKey: ['scene-flight', flightId] })
    try {
      const updated = await action()
      client.setQueryData(['scene-flight', flightId], updated)
      if (resetDrafts) setDrafts(null)
      return true
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The scene flight could not be updated.')
      if (failure instanceof ApiError && failure.status === 409 && /changed elsewhere/.test(failure.message)) setConflict(true)
      return false
    } finally { running.current = false; setBusy(false) }
  }

  if (flight.isPending) return <main className="route-state" role="status">Opening scene flight</main>
  if (!flight.data) return <main className="route-state"><div><h1>Scene flight unavailable</h1><p>{flight.error?.message}</p><Link to="/">Return to studio</Link></div></main>
  const data = flight.data
  const legs = drafts ?? draftsFrom(data)
  const dirty = drafts !== null
  const edit = (next: LegDraft[]) => setDrafts(next)

  async function uploadImages(files: FileList | null) {
    if (!files?.length || running.current) return
    running.current = true; setBusy(true); setError(null)
    try {
      for (const file of Array.from(files)) {
        if (file.size > 20 * 1024 * 1024) throw new Error(`${file.name} is larger than 20 MB.`)
        await flightApi.addImage(flightId, file)
      }
      client.setQueryData(['scene-flight', flightId], await flightApi.get(flightId))
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The image could not be added.')
    } finally { running.current = false; setBusy(false) }
  }

  function addLeg() {
    const previous = legs.at(-1)
    const origin = previous?.destination_image_key ?? data.images[0] ?? ''
    const destination = data.images.find((key) => key !== origin) ?? ''
    edit([...legs, { leg_id: `leg-${crypto.randomUUID().slice(0, 8)}`, origin_image_key: origin, destination_image_key: destination, prompt: '', origin_name: previous?.destination_name ?? '', destination_name: '', negative_prompt: '', duration_seconds: data.recipe.default_duration_seconds, name: '' }])
  }

  const allSelected = data.legs.length > 0 && data.legs.every((leg) => leg.takes.some((take) => take.take_id === leg.selected_take_id && take.status === 'succeeded' && !take.stale))
  return <ProjectShell
    project={{ id: flightId, name: data.name, saveState: busy ? 'Saving' : dirty ? 'Unsaved changes' : 'Saved' }}
    workflow={{ name: 'Scene Flight', destinations: [{ label: 'Flight', path: `/flights/${flightId}` }], releaseHint: '', exportHint: '' }}
    actions={<div className="project-header__actions">
      <Button disabled={busy || dirty || !allSelected} variant="primary" onClick={() => void run(() => flightApi.chain(flightId, data.revision, crypto.randomUUID()))}>Chain flight</Button>
    </div>}
  >
    <div className="movie-workspace flight-workspace">
      {error && <div className="movie-notice movie-notice--error" role="alert">{error}{conflict && <button className="button" onClick={() => window.location.reload()}>Reload latest flight</button>}</div>}
      {!data.generation_available && <p className="movie-notice" role="status">This server has no GPU executor that can pin a clip's last frame, so legs can be planned but not generated here.</p>}
      <section className="movie-panel" aria-labelledby="flight-images-title">
        <div className="movie-section-title"><h2 id="flight-images-title">Location images</h2><span>{data.recipe.width}×{data.recipe.height} · {data.recipe.fps} fps · {data.recipe.steps} steps</span></div>
        <p>Each leg starts exactly on its origin image and ends on its destination image. 16:9 images are cropped slightly to the {data.recipe.width}×{data.recipe.height} canvas.</p>
        <label className="movie-upload">Add images<input type="file" multiple accept="image/png,image/jpeg,image/webp" aria-label="Add location images" disabled={busy} onChange={(event) => { void uploadImages(event.target.files); event.target.value = '' }} /></label>
        <ol className="flight-images">{data.images.map((key, index) => <li key={key}><img src={flightApi.imageUrl(flightId, key)} alt={`Location ${index + 1}`} /><span>Image {index + 1}</span></li>)}</ol>
      </section>
      <section className="movie-panel" aria-labelledby="flight-legs-title">
        <div className="movie-section-title"><h2 id="flight-legs-title">Legs</h2><span>{dirty ? 'Unsaved changes' : `${data.legs.length} saved`}</span></div>
        {legs.map((leg, index) => <LegEditor key={leg.leg_id} index={index} leg={leg} images={data.images} flightId={flightId} recipe={data.recipe} busy={busy}
          onChange={(next) => edit(legs.map((item, at) => at === index ? next : item))}
          onRemove={() => edit(legs.filter((_, at) => at !== index))} />)}
        <div className="movie-actions">
          <Button disabled={busy || data.images.length < 2 || legs.length >= data.recipe.max_legs} onClick={addLeg}>{legs.length ? 'Add next leg' : 'Add a leg'}</Button>
          <Button variant="primary" disabled={busy || !dirty} onClick={() => void run(() => flightApi.setLegs(flightId, data.revision, legs), true)}>Save legs</Button>
          {dirty && <Button disabled={busy} onClick={() => setDrafts(null)}>Discard changes</Button>}
        </div>
        {data.images.length < 2 && <p>Add at least two images to plan a leg.</p>}
      </section>
      {data.legs.map((leg) => <LegTakes key={leg.leg_id} flight={data} leg={leg} busy={busy || dirty} run={run} />)}
      {data.chain && <section className="movie-panel" aria-labelledby="flight-chain-title">
        <div className="movie-section-title"><h2 id="flight-chain-title">Chained flight</h2><span>{data.chain.status}</span></div>
        {data.chain.error && <p role="alert">{data.chain.error}</p>}
        {data.chain.video_key && <>
          <video key={data.chain.video_key} src={flightApi.chainVideoUrl(flightId)} controls playsInline preload="metadata" aria-label="Chained flight" />
          <ul className="flight-joins">{data.chain.joins.map((join) => <li key={join.index}>Join {join.index + 1} at {join.at_seconds.toFixed(2)} s: difference {percent(join.diff)}{join.diff > join.limit ? ' (visible jump)' : ''}</li>)}</ul>
          <a className="button" href={flightApi.chainVideoUrl(flightId, true)} download>Download chained flight</a>
        </>}
      </section>}
    </div>
  </ProjectShell>
}

function LegEditor({ index, leg, images, flightId, recipe, busy, onChange, onRemove }: { index: number; leg: LegDraft; images: string[]; flightId: string; recipe: SceneFlight['recipe']; busy: boolean; onChange: (leg: LegDraft) => void; onRemove: () => void }) {
  const id = `leg-${leg.leg_id}`
  const imageOptions = images.map((key, at) => <option key={key} value={key}>Image {at + 1}</option>)
  const named = (patch: Partial<LegDraft>) => onChange({ ...leg, ...patch, name: '' })
  return <fieldset className="movie-panel flight-leg" disabled={busy}>
    <legend>Leg {index + 1}{leg.name ? ` · ${leg.name}` : ''}</legend>
    <div className="flight-leg__frames">
      {[['origin', 'Origin', leg.origin_image_key], ['destination', 'Destination', leg.destination_image_key]].map(([field, label, key]) => <div key={field} className="movie-field">
        <label htmlFor={`${id}-${field}`}>{label} image</label>
        <select id={`${id}-${field}`} value={key} onChange={(event) => onChange({ ...leg, [`${field}_image_key`]: event.target.value })}><option value="">Choose…</option>{imageOptions}</select>
        {key && <img src={flightApi.imageUrl(flightId, key)} alt={`${label} for leg ${index + 1}`} />}
      </div>)}
    </div>
    <div className="movie-fields-row">
      <div className="movie-field"><label htmlFor={`${id}-on`}>Origin place name</label><input id={`${id}-on`} value={leg.origin_name} onChange={(event) => named({ origin_name: event.target.value })} /></div>
      <div className="movie-field"><label htmlFor={`${id}-dn`}>Destination place name</label><input id={`${id}-dn`} value={leg.destination_name} onChange={(event) => named({ destination_name: event.target.value })} /></div>
    </div>
    <div className="movie-field"><label htmlFor={`${id}-prompt`}>Camera direction</label>
      <textarea id={`${id}-prompt`} rows={3} maxLength={1600} value={leg.prompt} placeholder="Leave empty to compose the standard flight wording from the place names." onChange={(event) => onChange({ ...leg, prompt: event.target.value })} /></div>
    <div className="movie-fields-row">
      <div className="movie-field"><label htmlFor={`${id}-avoid`}>Avoid</label><input id={`${id}-avoid`} maxLength={400} value={leg.negative_prompt} onChange={(event) => onChange({ ...leg, negative_prompt: event.target.value })} /></div>
      <div className="movie-field movie-field--short"><label htmlFor={`${id}-seconds`}>Seconds</label><input id={`${id}-seconds`} type="number" min={recipe.min_duration_seconds} max={recipe.max_duration_seconds} step={recipe.duration_step_seconds} value={leg.duration_seconds} onChange={(event) => onChange({ ...leg, duration_seconds: Number(event.target.value) })} /></div>
    </div>
    <div className="movie-actions"><Button onClick={onRemove}>Remove leg</Button></div>
  </fieldset>
}

function LegTakes({ flight, leg, busy, run }: { flight: SceneFlight; leg: FlightLeg; busy: boolean; run: (action: () => Promise<SceneFlight>) => Promise<boolean> }) {
  const request = useRef<string | null>(null)
  const active = leg.takes.find(flightTakeActive)
  const selected = leg.takes.find((take) => take.take_id === leg.selected_take_id)
  const latest = leg.takes.at(-1)
  async function generate() {
    request.current ??= crypto.randomUUID()
    if (await run(() => flightApi.generate(flight.flight_id, leg.leg_id, flight.revision, request.current!))) request.current = null
  }
  return <section className="movie-panel flight-takes" aria-label={`${leg.name} takes`}>
    <div className="movie-section-title"><h3>{leg.name}</h3><span>{active ? 'Generating…' : selected ? 'Take selected' : 'No take yet'}</span></div>
    <div className="flight-takes__stage">
      {selected?.video_key ? <video key={selected.take_id} src={flightApi.takeVideoUrl(flight.flight_id, leg.leg_id, selected.take_id)} controls playsInline preload="metadata" aria-label={`${leg.name} selected take`} />
        : <div className="flight-takes__pins"><img src={flightApi.imageUrl(flight.flight_id, leg.origin_image_key)} alt={`${leg.name} first frame`} /><span aria-hidden="true">→</span><img src={flightApi.imageUrl(flight.flight_id, leg.destination_image_key)} alt={`${leg.name} last frame`} /></div>}
    </div>
    {active && <p role="status">Generating take {leg.takes.indexOf(active) + 1} on the GPU. This page keeps checking.</p>}
    {latest?.status === 'failed' && <p role="alert">{latest.error || 'The last take failed. Try again.'}</p>}
    <div className="movie-actions">
      <Button variant="primary" disabled={busy || !!active || !flight.generation_available} onClick={() => void generate()}>{leg.takes.some((take) => take.status === 'succeeded') ? 'Re-roll this leg' : 'Generate this leg'}</Button>
    </div>
    {leg.takes.length > 0 && <ol className="movie-takes">{leg.takes.map((take, number) => {
      const isSelected = take.take_id === leg.selected_take_id
      return <li key={take.take_id} className={`movie-take${isSelected ? ' is-selected' : ''}`}>
        <div className="movie-section-title"><h4>Take {number + 1}</h4><span>{isSelected ? 'Selected' : take.stale ? 'Out of date' : take.status}</span></div>
        <p>Seed {take.seed ?? '—'} · {take.width}×{take.height} · {take.length_frames ?? '—'} frames{take.status === 'succeeded' ? ` · ${take.duration_seconds.toFixed(2)} s` : ''}</p>
        {take.status === 'succeeded' && <p>First frame vs origin: {percent(take.first_frame_diff)} · last frame vs destination: {percent(take.last_frame_diff)}</p>}
        {take.error && <p role="alert">{take.error}</p>}
        <div className="movie-actions">
          {take.status === 'succeeded' && !take.stale && <Button disabled={busy || isSelected} onClick={() => void run(() => flightApi.select(flight.flight_id, leg.leg_id, flight.revision, take.take_id))}>{isSelected ? 'Selected take' : 'Use this take'}</Button>}
          {take.status === 'succeeded' && <a className="button" href={flightApi.takeVideoUrl(flight.flight_id, leg.leg_id, take.take_id, true)} download>Download</a>}
          {flightTakeActive(take) && <Button disabled={busy} onClick={() => void run(() => flightApi.cancel(flight.flight_id, leg.leg_id, take.take_id, flight.revision))}>Cancel take</Button>}
        </div>
      </li>
    })}</ol>}
  </section>
}

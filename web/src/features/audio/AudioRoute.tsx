/* eslint-disable jsx-a11y/media-has-caption -- Sound effects, beds and music cues have no caption source. */
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { Button } from '../../components/ui/Button'
import { ProjectShell } from '../project/ProjectShell'
import { audioApi, draftFrom, kept, type AudioCapabilities, type AudioSet, type Candidate, type Cue, type CueInput, type CueKind, type FreeSound, type LicencePolicy, type SetDraft } from './audioApi'
import '../movies/movies.css'
import './audio.css'

const KINDS: { value: CueKind; label: string }[] = [
  { value: 'sfx', label: 'Sound effect' }, { value: 'ambience', label: 'Ambience' },
  { value: 'music', label: 'Music' }, { value: 'line', label: 'Voice line' },
]
const seconds = (value: number | null | undefined) => value == null ? 'length unknown' : `${value.toFixed(1)} s`
const allowed = (candidate: Candidate, policy: LicencePolicy) =>
  candidate.licence.source === 'upload' || candidate.licence.spdx === 'CC0-1.0' || (policy === 'cc0_cc_by' && candidate.licence.spdx.startsWith('CC-BY'))

export function AudioRoute() {
  const { setId = '' } = useParams()
  const client = useQueryClient()
  const running = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState(false)
  const [draft, setDraft] = useState<SetDraft | null>(null)
  const audioSet = useQuery({ queryKey: ['audio-set', setId], queryFn: () => audioApi.get(setId) })
  const capabilities = useQuery({ queryKey: ['audio-capabilities'], queryFn: audioApi.capabilities })

  async function run(action: () => Promise<AudioSet>, resetDraft = false): Promise<boolean> {
    if (running.current) return false
    running.current = true; setBusy(true); setError(null)
    try {
      const updated = await action()
      client.setQueryData(['audio-set', setId], updated)
      void client.invalidateQueries({ queryKey: ['audio-readiness', setId] })
      if (resetDraft) setDraft(null)
      return true
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The audio set could not be updated.')
      if (failure instanceof ApiError && failure.status === 409 && /changed elsewhere/.test(failure.message)) setConflict(true)
      return false
    } finally { running.current = false; setBusy(false) }
  }

  if (audioSet.isPending) return <main className="route-state" role="status">Opening audio set</main>
  if (!audioSet.data) return <main className="route-state"><div><h1>Audio set unavailable</h1><p>{audioSet.error?.message}</p><Link to="/">Return to studio</Link></div></main>
  const data = audioSet.data
  const caps = capabilities.data
  const current = draft ?? draftFrom(data)
  const dirty = draft !== null
  const edit = (patch: Partial<SetDraft>) => setDraft({ ...current, ...patch })
  const editCue = (index: number, patch: Partial<CueInput>) => edit({ cues: current.cues.map((cue, at) => at === index ? { ...cue, ...patch } : cue) })
  const addCue = () => edit({ cues: [...current.cues, { cue_id: `cue-${crypto.randomUUID().slice(0, 8)}`, name: '', kind: 'sfx', prompt: '', text: '', target_seconds: null, loop: false, keep: 1 }] })

  return <ProjectShell
    project={{ id: setId, name: data.name, saveState: busy ? 'Saving' : dirty ? 'Unsaved changes' : 'Saved' }}
    workflow={{ name: 'Audio', destinations: [{ label: 'Audio', path: `/audio/${setId}` }], releaseHint: '', exportHint: '' }}
    // Releases live on the page itself; the shell's Export/Release pair does not apply.
    actions={<div className="project-header__actions" />}
  >
    <div className="movie-workspace audio-workspace">
      {error && <div className="movie-notice movie-notice--error" role="alert">{error}{conflict && <button className="button" onClick={() => window.location.reload()}>Reload latest audio set</button>}</div>}
      {caps && !caps.free_sounds_available && <p className="movie-notice" role="status">Free-sound search is not set up on this server yet. You can still upload your own files.</p>}
      {caps && !caps.processing_available && <p className="movie-notice" role="status">Your files are kept exactly as uploaded or retrieved: loudness levelling and measurements are not available for this account yet.</p>}
      <section className="movie-panel" aria-labelledby="audio-set-title">
        <div className="movie-section-title"><h2 id="audio-set-title">Cues</h2><span>{dirty ? 'Unsaved changes' : `${data.cues.length} saved`}</span></div>
        <p>Each cue is one sound you need. Gather candidates for it from free libraries or your own files, keep the good ones, and choose the variants to ship.</p>
        <fieldset className="audio-policy" disabled={busy}>
          <legend>Free-sound licences</legend>
          <label><input type="radio" name="licence-policy" checked={current.licence_policy === 'cc0'} onChange={() => edit({ licence_policy: 'cc0' })} />CC0 only<small>Nothing you ship needs a credit.</small></label>
          <label><input type="radio" name="licence-policy" checked={current.licence_policy === 'cc0_cc_by'} onChange={() => edit({ licence_policy: 'cc0_cc_by' })} />CC0 and CC-BY<small>CC-BY sounds need a credit wherever you use them; releases write the credits for you.</small></label>
        </fieldset>
        {current.cues.map((cue, index) => <fieldset key={cue.cue_id} className="audio-cue-editor" disabled={busy}>
          <legend>Cue {index + 1}</legend>
          <div className="movie-fields-row">
            <label className="movie-field">Name<input value={cue.name} onChange={(event) => editCue(index, { name: event.target.value })} aria-label={`Cue ${index + 1} name`} /></label>
            <label className="movie-field movie-field--short">Kind<select value={cue.kind} onChange={(event) => editCue(index, { kind: event.target.value as CueKind })} aria-label={`Cue ${index + 1} kind`}>{KINDS.map((kind) => <option key={kind.value} value={kind.value}>{kind.label}</option>)}</select></label>
            <label className="movie-field movie-field--short">Variants<input type="number" min={1} max={16} value={cue.keep} onChange={(event) => editCue(index, { keep: Math.max(1, Math.min(16, Number(event.target.value) || 1)) })} aria-label={`Cue ${index + 1} variants`} /></label>
          </div>
          <label className="movie-field">What it sounds like<input value={cue.prompt} onChange={(event) => editCue(index, { prompt: event.target.value })} aria-label={`Cue ${index + 1} description`} placeholder="glass crack, short, dry" /></label>
          {cue.kind === 'line' && <label className="movie-field">Words<input value={cue.text} onChange={(event) => editCue(index, { text: event.target.value })} aria-label={`Cue ${index + 1} words`} /></label>}
          <div className="movie-actions">
            <label className="audio-inline"><input type="checkbox" checked={cue.loop} onChange={(event) => editCue(index, { loop: event.target.checked })} />Loops</label>
            <Button onClick={() => edit({ cues: current.cues.filter((_, at) => at !== index) })}>Remove cue</Button>
          </div>
        </fieldset>)}
        <div className="movie-actions">
          <Button disabled={busy || current.cues.length >= (caps?.max_cues ?? 64)} onClick={addCue}>{current.cues.length ? 'Add another cue' : 'Add a cue'}</Button>
          <Button variant="primary" disabled={busy || !dirty || current.cues.some((cue) => !cue.name.trim())} onClick={() => void run(() => audioApi.update(setId, data.revision, current), true)}>Save cues</Button>
          {dirty && <Button disabled={busy} onClick={() => setDraft(null)}>Discard changes</Button>}
        </div>
      </section>
      {data.cues.map((cue) => <CueBoard key={cue.cue_id} audioSet={data} cue={cue} caps={caps} busy={busy || dirty} run={run} />)}
      {data.cues.length > 0 && <ReleasePanel audioSet={data} busy={busy || dirty} run={run} />}
    </div>
  </ProjectShell>
}

function CueBoard({ audioSet, cue, caps, busy, run }: { audioSet: AudioSet; cue: Cue; caps?: AudioCapabilities; busy: boolean; run: (action: () => Promise<AudioSet>) => Promise<boolean> }) {
  const id = audioSet.set_id
  const candidates = audioSet.candidates.filter((candidate) => candidate.cue_id === cue.cue_id)
  const full = candidates.length >= (caps?.max_candidates_per_cue ?? 16)
  const [query, setQuery] = useState(cue.prompt || cue.name)
  const [maxSeconds, setMaxSeconds] = useState(cue.target_seconds ? Math.min(600, Math.ceil(cue.target_seconds * 2)) : 30)
  const [results, setResults] = useState<FreeSound[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
  const imported = new Set(candidates.filter((c) => c.origin === 'freesound').map((c) => c.licence.source_id))

  async function search() {
    setSearching(true); setSearchError(null)
    try { setResults(await audioApi.search(id, query.trim(), maxSeconds)) } catch (failure) {
      setSearchError(failure instanceof Error ? failure.message : 'The search could not be completed.')
    } finally { setSearching(false) }
  }

  function toggleShip(candidate: Candidate, ship: boolean) {
    const next = ship ? [...cue.selected, candidate.candidate_id] : cue.selected.filter((c) => c !== candidate.candidate_id)
    void run(() => audioApi.select(id, cue.cue_id, audioSet.revision, next))
  }

  return <section className="movie-panel audio-cue" aria-label={`${cue.name} candidates`}>
    <div className="movie-section-title"><h2>{cue.name}</h2><span>{cue.selected.length} of {cue.keep} chosen · {candidates.length} candidate{candidates.length === 1 ? '' : 's'}</span></div>
    {cue.prompt && <p>{cue.prompt}</p>}
    <div className="audio-gather">
      {caps?.free_sounds_available && <form className="audio-search" onSubmit={(event) => { event.preventDefault(); if (query.trim()) void search() }}>
        <label className="movie-field">Search free sounds<input value={query} onChange={(event) => setQuery(event.target.value)} aria-label={`Search free sounds for ${cue.name}`} /></label>
        <label className="movie-field movie-field--short">Up to (s)<input type="number" min={1} max={600} value={maxSeconds} onChange={(event) => setMaxSeconds(Math.max(1, Math.min(600, Number(event.target.value) || 30)))} aria-label={`Longest result for ${cue.name}`} /></label>
        <Button type="submit" disabled={searching || !query.trim()}>{searching ? 'Searching…' : 'Search'}</Button>
      </form>}
      <label className="movie-upload">Upload a file<input type="file" accept="audio/wav,audio/x-wav,audio/mpeg,.wav,.mp3" aria-label={`Upload a file for ${cue.name}`} disabled={busy || full}
        onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void run(() => audioApi.addUpload(id, cue.cue_id, file)) }} /></label>
    </div>
    {searchError && <p role="alert">{searchError}</p>}
    {results && <div className="audio-results" aria-label={`Free sounds for ${cue.name}`}>
      <h3>Free sounds</h3>
      {results.length === 0 && <p>No sounds with an allowed licence matched. Try other words or a longer length.</p>}
      <ul>{results.map((sound) => <li key={sound.source_id}>
        <audio src={sound.preview_url} controls preload="none" aria-label={`Preview ${sound.name}`} />
        <div><strong>{sound.name}</strong><span>by {sound.author} · {seconds(sound.duration_seconds)} · <LicenceBadge spdx={sound.spdx} /> · <a href={sound.source_url} target="_blank" rel="noreferrer">Freesound #{sound.source_id}</a></span></div>
        <Button disabled={busy || full || imported.has(sound.source_id)} onClick={() => void run(() => audioApi.importSound(id, cue.cue_id, audioSet.revision, sound.source_id))}>{imported.has(sound.source_id) ? 'Added' : 'Add as candidate'}</Button>
      </li>)}</ul>
    </div>}
    {candidates.length > 0 && <ul className="audio-board">
      {candidates.map((candidate, index) => <CandidateTile key={candidate.candidate_id} audioSet={audioSet} cue={cue} candidate={candidate} index={index} caps={caps} busy={busy} run={run} onShip={toggleShip} />)}
    </ul>}
  </section>
}

function LicenceBadge({ spdx }: { spdx: string }) {
  const label = spdx === 'LicenseRef-Owner-Upload' ? 'Your upload' : spdx
  return <span className={`audio-licence${spdx.startsWith('CC-BY') ? ' audio-licence--credit' : ''}`}>{label}</span>
}

function CandidateTile({ audioSet, cue, candidate, index, caps, busy, run, onShip }: { audioSet: AudioSet; cue: Cue; candidate: Candidate; index: number; caps?: AudioCapabilities; busy: boolean; run: (action: () => Promise<AudioSet>) => Promise<boolean>; onShip: (candidate: Candidate, ship: boolean) => void }) {
  const id = audioSet.set_id
  const [note, setNote] = useState(candidate.review?.note ?? '')
  const label = `${cue.name} candidate ${index + 1}`
  const selected = cue.selected.includes(candidate.candidate_id)
  const shippable = kept(candidate) && allowed(candidate, audioSet.licence_policy)
  const name = typeof candidate.provenance.name === 'string' ? candidate.provenance.name : typeof candidate.provenance.filename === 'string' ? candidate.provenance.filename : `Candidate ${index + 1}`
  const m = candidate.measured
  const verdict = candidate.review ? `${candidate.review.outcome === 'keep' ? 'Kept' : 'Rejected'}${candidate.review.actor_kind === 'api_key' ? ' by an API key' : ''}` : 'Not reviewed'
  return <li className={`audio-tile${selected ? ' audio-tile--selected' : ''}${candidate.review?.outcome === 'reject' ? ' audio-tile--rejected' : ''}`} aria-label={label}>
    <audio src={audioApi.assetUrl(id, candidate.rendition_key ?? candidate.asset_key)} controls preload="metadata" aria-label={`Play ${label}`} />
    <div className="audio-tile__facts">
      <strong>{name}</strong>
      <span>{seconds(candidate.duration_seconds)}{m && m.integrated_lufs !== null ? ` · ${m.integrated_lufs} LUFS` : ''}{m && m.true_peak_dbtp !== null ? ` · peak ${m.true_peak_dbtp} dBTP` : ''}{m && m.centroid_hz !== null ? ` · centre ${Math.round(m.centroid_hz)} Hz` : ''}</span>
      <span><LicenceBadge spdx={candidate.licence.spdx} />{candidate.licence.author && ` by ${candidate.licence.author}`}{candidate.licence.source_url && <> · <a href={candidate.licence.source_url} target="_blank" rel="noreferrer">source</a></>}{candidate.licence.variant === 'preview' && ' · Freesound preview'}</span>
      {candidate.rendition_key && <span>Measured on the original. You hear the levelled copy (−16 LUFS); both ship.</span>}
      {!allowed(candidate, audioSet.licence_policy) && <span role="note">This set's licence setting no longer allows this sound.</span>}
    </div>
    <label className="movie-field">Note<input value={note} onChange={(event) => setNote(event.target.value)} aria-label={`Note for ${label}`} disabled={busy} /></label>
    <div className="movie-actions">
      <span className="audio-verdict">{verdict}</span>
      <Button disabled={busy} variant={candidate.review?.outcome === 'keep' ? 'primary' : undefined} onClick={() => void run(() => audioApi.review(id, candidate.candidate_id, audioSet.revision, 'keep', note, candidate.sha256))}>Keep</Button>
      <Button disabled={busy} onClick={() => void run(() => audioApi.review(id, candidate.candidate_id, audioSet.revision, 'reject', note, candidate.sha256))}>Reject</Button>
      <label className="audio-inline"><input type="checkbox" checked={selected} disabled={busy || !shippable || (!selected && cue.selected.length >= cue.keep)} onChange={(event) => onShip(candidate, event.target.checked)} />Ship{selected ? ` as variant ${cue.selected.indexOf(candidate.candidate_id) + 1}` : ''}</label>
    </div>
    {candidate.licence.variant === 'preview' && caps?.replace_original_available && <label className="movie-upload">Replace with the original<input type="file" accept="audio/wav,audio/x-wav,audio/mpeg,.wav,.mp3" aria-label={`Original for ${label}`} disabled={busy}
      onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void run(() => audioApi.replaceOriginal(id, candidate.candidate_id, file)) }} /></label>}
  </li>
}

function ReleasePanel({ audioSet, busy, run }: { audioSet: AudioSet; busy: boolean; run: (action: () => Promise<AudioSet>) => Promise<boolean> }) {
  const id = audioSet.set_id
  const readiness = useQuery({ queryKey: ['audio-readiness', id, audioSet.revision], queryFn: () => audioApi.readiness(id) })
  const [name, setName] = useState('')
  const ready = readiness.data?.ready ?? false
  return <section className="movie-panel" aria-labelledby="audio-release-title">
    <div className="movie-section-title"><h2 id="audio-release-title">Release</h2><span>{audioSet.releases.length} release{audioSet.releases.length === 1 ? '' : 's'}</span></div>
    <p>A release freezes the chosen variants into a zip with a manifest of every file's licence, source and history, and a credits file. It never changes afterwards.</p>
    {readiness.data && !ready && <ul className="audio-problems">{readiness.data.problems.map((problem, at) => <li key={at}>{problem.cue_id ? `${audioSet.cues.find((c) => c.cue_id === problem.cue_id)?.name ?? problem.cue_id}: ` : ''}{problem.message}</li>)}</ul>}
    {readiness.data?.attribution_required && <p role="note">This release includes CC-BY sounds. Credit them as CREDITS.md says wherever you use them.</p>}
    {readiness.data && readiness.data.previews > 0 && <p role="note">{readiness.data.previews} chosen file{readiness.data.previews === 1 ? ' is a Freesound preview' : 's are Freesound previews'}, not the original recording.</p>}
    <form className="movie-fields-row" onSubmit={(event) => { event.preventDefault(); void run(() => audioApi.release(id, audioSet.revision, name.trim())).then((ok) => { if (ok) setName('') }) }}>
      <label className="movie-field">Release name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="v1" /></label>
      <Button type="submit" variant="primary" disabled={busy || !ready || !name.trim()}>Release audio</Button>
    </form>
    {audioSet.releases.length > 0 && <ul className="audio-releases">{[...audioSet.releases].reverse().map((release) => <li key={release.release_id}>
      <strong>{release.name}</strong><span>{release.file_count} file{release.file_count === 1 ? '' : 's'}{release.attribution_required ? ' · needs credits' : ''} · {new Date(release.created_at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</span>
      <a className="button" href={audioApi.releaseUrl(id, release.release_id)} download>Download {release.name}</a>
    </li>)}</ul>}
  </section>
}

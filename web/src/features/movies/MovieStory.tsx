import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '../../components/ui/Button'
import { MAX_SHOT_SECONDS, movieApi, type DialogueLine, type Movie, type ShotDefinition } from './movieApi'
import type { MovieWorkspaceProps } from './MovieRoute'
import { MovieCast } from './MovieCast'
import { MovieShotMedia } from './MovieShots'
import { MovieSequence } from './MovieSequence'
import { useDraftRevision } from './useDraftRevision'

// Beats past the shot's end (after shortening it) are dropped on save; the editor flags them first.
const shotFields = ({ shot_id, name, prompt, opening_prompt, duration_seconds, continuation_of, dialogue, soundscape, camera, still, beats }: ShotDefinition): ShotDefinition => ({ shot_id, name, prompt, opening_prompt, duration_seconds, continuation_of, dialogue: dialogue ?? [], soundscape: soundscape ?? '', camera: camera ?? '', still: still ?? '', beats: (beats ?? []).filter((beat) => beat.at_seconds < duration_seconds) })
const newShot = (shot_id: string, name: string): ShotDefinition => ({ shot_id, name, prompt: '', opening_prompt: '', duration_seconds: 4, continuation_of: null, dialogue: [], soundscape: '', camera: '', still: '', beats: [] })
const storyFields = (movie: Movie) => ({ compute_target: movie.compute_target ?? 'shared', brief: movie.brief, style: movie.style, batch_size: movie.batch_size, shots: movie.shots.map(shotFields) })

export function MovieStory({ movie, capabilities, busy, run }: MovieWorkspaceProps) {
  const buildRequest = useRef<string | null>(null)
  const storyboardRequest = useRef<string | null>(null)
  const compositionRequest = useRef<{ shot: string; id: string } | null>(null)
  const shotRequest = useRef<{ shot: string; id: string } | null>(null)
  const building = ['generating', 'assembling'].includes(movie.build?.status ?? '')
  const [activeShotId, setActiveShotId] = useState(movie.shots[0]?.shot_id ?? '')
  const [brief, setBrief] = useState(movie.brief)
  const [style, setStyle] = useState(movie.style)
  const [computeTarget, setComputeTarget] = useState<'shared' | 'my_machines'>(movie.compute_target ?? 'shared')
  const [batch, setBatch] = useState(movie.batch_size)
  const [shots, setShots] = useState<ShotDefinition[]>(movie.shots.map(shotFields))
  const cast = movie.characters ?? []
  const voiced = cast.filter((c) => c.voice_asset_key)
  const [referencesOpen, setReferencesOpen] = useState(movie.shots.length === 0)
  const [reference, setReference] = useState<File | null>(null)
  const [referenceShotId, setReferenceShotId] = useState<string | null>(null)
  const [referencePreview, setReferencePreview] = useState<string | null>(null)
  const [sequence, setSequence] = useState<Record<string, File>>({})
  const [sequencePreviews, setSequencePreviews] = useState<Record<string, string>>({})
  const [savingSequence, setSavingSequence] = useState(false)
  useEffect(() => {
    const previews = Object.fromEntries(Object.entries(sequence).map(([id, file]) => [id, URL.createObjectURL(file)]))
    setSequencePreviews(previews)
    return () => Object.values(previews).forEach((url) => URL.revokeObjectURL(url))
  }, [sequence])
  function addSequence(files: File[]) {
    if (!files.length) return
    if (shots.length + files.length > 40) { setReferenceError('A movie can have up to 40 shots.'); return }
    if (files.some((file) => !['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 20_000_000)) {
      setReferenceError('Choose PNG, JPEG or WebP images smaller than 20 MB each.'); return
    }
    const additions = files.map((file, index) => newShot(crypto.randomUUID(), file.name.replace(/\.[^.]+$/, '').slice(0, 160) || `Shot ${shots.length + index + 1}`))
    setShots([...shots, ...additions]); setActiveShotId(additions[0].shot_id)
    setSequence({ ...sequence, ...Object.fromEntries(additions.map((shot, index) => [shot.shot_id, files[index]])) })
    setReferenceError(null); setSaved(false)
  }
  function moveShot(index: number, destination: number) {
    const reordered = [...shots]
    const [moving] = reordered.splice(index, 1)
    reordered.splice(destination, 0, moving)
    if (reordered.some((shot, i) => shot.continuation_of && !reordered.slice(0, i).some((previous) => previous.shot_id === shot.continuation_of))) {
      setReferenceError('Keep continuation shots after the shots they continue.'); return false
    }
    setShots(reordered); setSaved(false); setReferenceError(null); return true
  }
  const [referenceError, setReferenceError] = useState<string | null>(null)
  useEffect(() => {
    if (!reference) return
    const url = URL.createObjectURL(reference)
    setReferencePreview(url)
    return () => URL.revokeObjectURL(url)
  }, [reference])
  const openingKey = movie.shots.find((shot) => shot.shot_id === shots[0]?.shot_id)?.opening_asset_key
  const startingImage = reference ? referencePreview : openingKey ? movieApi.assetUrl(movie.movie_id, openingKey) : null
  function chooseReference(file?: File) {
    if (!file) return
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 20_000_000) {
      setReferenceError('Choose a PNG, JPEG or WebP image smaller than 20 MB.')
      return
    }
    setReferenceError(null); setReference(file); setSaved(false)
    const id = shots[0]?.shot_id ?? crypto.randomUUID()
    setReferenceShotId(id)
    if (!shots.length) setShots([newShot(id, 'Shot 1')])
  }
  const [saved, setSaved] = useState(false)
  const [shotCount, setShotCount] = useState(4)
  const [targetSeconds, setTargetSeconds] = useState<number | ''>('')
  const [planning, setPlanning] = useState(false)
  const [planError, setPlanError] = useState<string | null>(null)
  const revision = useDraftRevision(storyFields(movie), movie.revision)
  const total = shots.reduce((sum, shot) => sum + shot.duration_seconds, 0)
  function edit(id: string, patch: Partial<ShotDefinition>) { setSaved(false); setShots((current) => current.map((shot) => shot.shot_id === id ? { ...shot, ...patch } : shot)) }
  function editLine(id: string, index: number, patch: Partial<DialogueLine> | null) {
    const shot = shots.find((item) => item.shot_id === id)
    if (!shot) return
    const lines = [...(shot.dialogue ?? [])]
    if (patch === null) lines.splice(index, 1); else lines[index] = { ...lines[index], ...patch }
    edit(id, { dialogue: lines })
  }
  async function submit(event: FormEvent) {
    event.preventDefault()
    const intent = (event.nativeEvent as SubmitEvent).submitter?.getAttribute('value')
    const generate = intent === 'generate'
    const storyboard = intent === 'storyboard'
    const composeShot = intent?.startsWith('compose:') ? intent.slice(8) : null
    const generateShot = intent?.startsWith('shot:') ? intent.slice(5) : null
    const invalid = shots.find((shot) => !shot.name.trim() || !shot.prompt.trim() || !Number.isFinite(shot.duration_seconds) || shot.duration_seconds < (capabilities?.min_duration_seconds ?? 1.5) || shot.duration_seconds > (capabilities?.max_duration_seconds ?? MAX_SHOT_SECONDS))
    if (invalid) { setActiveShotId(invalid.shot_id); setReferenceError(`Complete the name, action and duration for ${invalid.name || 'this shot'} before saving.`); return }
    const unspoken = shots.find((shot) => shot.dialogue?.some((line) => !line.text.trim() || !voiced.some((c) => c.character_id === line.character_id)))
    if (unspoken) { setActiveShotId(unspoken.shot_id); setReferenceError(`Give every line in ${unspoken.name || 'this shot'} words and a character with a saved voice.`); return }
    setReferenceError(null)
    setSavingSequence(true)
    try {
    setSaved(false)
    const storySaved = await run(async () => {
      const result = await movieApi.story(movie.movie_id, { compute_target: computeTarget, expected_revision: revision.expectedRevision, brief, style, batch_size: batch, shots: shots.map(shotFields) })
      revision.committed(storyFields(result), result.revision)
      return result
    })
    if (!storySaved) return
    if (reference && referenceShotId && shots.some((shot) => shot.shot_id === referenceShotId)) {
      const imageSaved = await run(async () => {
        const target = await movieApi.upload(movie.movie_id, reference)
        const result = await movieApi.opening(movie.movie_id, referenceShotId, target.revision, target.asset_key)
        revision.committed(storyFields(result), result.revision)
        setReference(null)
        return result
      })
      if (!imageSaved) return
    }
    for (const shot of shots) {
      const file = sequence[shot.shot_id]
      if (!file) continue
      if (!await run(async () => {
        const target = await movieApi.upload(movie.movie_id, file)
        const result = await movieApi.opening(movie.movie_id, shot.shot_id, target.revision, target.asset_key)
        revision.committed(storyFields(result), result.revision)
        setSequence((current) => { const next = { ...current }; delete next[shot.shot_id]; return next })
        return result
      })) return
    }
    setSaved(true)
    if (storyboard) {
      const current = await movieApi.get(movie.movie_id)
      const blank = current.shots.find((shot) => !shot.opening_prompt.trim())
      if (blank) { setActiveShotId(blank.shot_id); setReferenceError(`Write an opening frame direction for ${blank.name} before storyboarding.`); return }
      storyboardRequest.current ??= crypto.randomUUID()
      if (await run(() => movieApi.storyboard(movie.movie_id, current.revision, storyboardRequest.current!))) storyboardRequest.current = null
    }
    if (composeShot) {
      const current = await movieApi.get(movie.movie_id)
      if (compositionRequest.current?.shot !== composeShot) compositionRequest.current = { shot: composeShot, id: crypto.randomUUID() }
      if (await run(() => movieApi.compose(movie.movie_id, composeShot, current.revision, compositionRequest.current!.id))) compositionRequest.current = null
    }
    if (generate || generateShot) {
      const current = await movieApi.get(movie.movie_id)
      const missing = current.shots.find((shot) => (!generateShot || shot.shot_id === generateShot) && !shot.continuation_of && !shot.opening_asset_key)
      if (missing) { setActiveShotId(missing.shot_id); setReferenceError(`Add a starting image for ${missing.name}, or compose one under Starting image & existing clips.`); return }
      if (generateShot) {
        if (shotRequest.current?.shot !== generateShot) shotRequest.current = { shot: generateShot, id: crypto.randomUUID() }
        if (await run(() => movieApi.generate(movie.movie_id, { expected_revision: current.revision, shot_ids: [generateShot], select_when_ready: true, request_id: shotRequest.current!.id }))) shotRequest.current = null
      } else {
        buildRequest.current ??= crypto.randomUUID()
        if (await run(() => movieApi.build(movie.movie_id, current.revision, buildRequest.current!))) buildRequest.current = null
      }
    }
    } catch (error) { setReferenceError(error instanceof Error ? error.message : 'The movie could not be saved.') }
    finally { setSavingSequence(false) }
  }
  return <section aria-labelledby="movie-story-title">
    <header className="movie-story-toolbar"><div className="movie-toolbar-title"><h2 id="movie-story-title">Movie</h2><span>{shots.length} shots · {total.toFixed(1)}s{saved ? ' · Saved' : ''}</span></div><div className="movie-actions">
      {capabilities?.composition_available && <Button form="movie-story-form" type="submit" value="storyboard" disabled={busy || building || savingSequence || planning || !shots.length || Object.values(movie.compositions ?? {}).some((item) => item.status === 'pending')} title="Compose one frame per shot from the cast portraits, each continuing the previous frame. The frames become the shots' images.">Storyboard</Button>}
      <Button form="movie-story-form" type="submit" value="generate" variant="primary" disabled={busy || building || savingSequence || planning || !shots.length || !capabilities?.generation_available}>{building ? 'Making movie…' : savingSequence ? 'Saving…' : 'Generate movie'}</Button>
      {!building && movie.assembly?.status === 'succeeded' && <Link className="button" to={`/movies/${movie.movie_id}/review`}>Review movie →</Link>}
    </div></header>
    {building && <p role="status" className="movie-build-progress">{movie.build?.status === 'assembling' ? 'Stitching your movie…' : 'Generating your movie in batches…'} Keep this workspace open; reopening resumes progress.</p>}
    {movie.build?.error && <p role="alert" className="movie-notice">{movie.build.error}</p>}
    {saved && <span role="status" className="sr-only">Movie saved.</span>}
    {referenceError && <p role="alert" className="movie-notice">{referenceError}</p>}
    <form id="movie-story-form" onSubmit={submit} className="movie-story-layout">
      <aside className="movie-panel movie-story-brief">
        <div className="movie-section-title"><h3>The creative brief</h3><Button type="submit" title="Keep your plan for later. Generating also saves it." disabled={busy || savingSequence || planning}>Save draft</Button></div>
        <details className="movie-starting-reference" open={referencesOpen} onToggle={(event) => setReferencesOpen(event.currentTarget.open)}><summary>Reference images · upload or replace</summary>
          <h4>Starting reference image</h4>
          <p>Start with your own image. It becomes the first shot’s opening frame; add the action below, then save your story.</p>
          {startingImage && <img className="movie-reference-preview" src={startingImage} alt="Starting reference for the first shot" />}
          <label className="movie-upload">{startingImage ? 'Replace reference image' : 'Upload reference image'}<input type="file" accept="image/png,image/jpeg,image/webp" aria-label="Upload starting reference image" disabled={busy || savingSequence || planning} onChange={(event) => { chooseReference(event.target.files?.[0]); event.target.value = '' }} /></label>
          <label className="movie-upload">Add image sequence<input type="file" multiple accept="image/png,image/jpeg,image/webp" aria-label="Add image sequence" disabled={busy || savingSequence || planning} onChange={(event) => { addSequence(Array.from(event.target.files ?? [])); event.target.value = '' }} /></label>
          <p className="movie-muted">PNG, JPEG or WebP · up to 20 MB each. Select multiple images to append one shot per image, then arrange the shots below.</p>
          {reference && <p role="status">{reference.name} · saves with your story</p>}
        </details>
        <label className="movie-field">Movie brief<textarea value={brief} onChange={(e) => { setBrief(e.target.value); setSaved(false) }} rows={3} maxLength={12000} placeholder="What happens, who matters, and how should the movie feel?" /></label>
        <label className="movie-field">Visual direction<textarea value={style} onChange={(e) => { setStyle(e.target.value); setSaved(false) }} rows={2} maxLength={1000} placeholder="Light, palette, characters and details to preserve across shots" /></label>
        <label className="movie-field">GPU network<select value={computeTarget} onChange={(event) => { setComputeTarget(event.target.value as 'shared' | 'my_machines'); setSaved(false) }}><option value="shared">Shared GPU network</option><option value="my_machines">My attached machines</option></select></label><p>{computeTarget === 'my_machines' ? 'New work waits for a compatible machine attached to your account.' : 'New work uses available shared GPU workers.'}</p>
        <div className="movie-budget"><span className="eyebrow">Generation budget</span><label className="movie-field">Clips per batch<input type="number" min={1} max={capabilities?.max_batch_size ?? 4} required value={batch} onChange={(e) => { setBatch(Number(e.target.value)); setSaved(false) }} /></label><p>Independent shots can use different GPU workers. Continuations wait for the selected previous take.</p></div>
      </aside>
      <div className="movie-story-plan">

        {capabilities?.planner_available && shots.length === 0 && <div className="movie-panel"><h3>{capabilities.director_available ? 'Turn your script into a cast and shots' : 'Turn your brief into shots'}</h3><p>{capabilities.director_available ? 'Paste a synopsis, screenplay or session transcript in the brief. The director drafts the cast (looks, voices) and the shots with their lines; nothing generates until you choose Generate movie.' : 'Get an editable plan with separate opening-frame and motion directions. Nothing generates until you choose Generate movie.'}</p><div className="movie-fields-row"><label className="movie-field">{capabilities.director_available ? 'Shots at most' : 'Number of shots'}<input type="number" min={1} max={capabilities.director_available ? 40 : 20} value={shotCount} onChange={(event) => setShotCount(Number(event.target.value))} /></label>{capabilities.director_available && <label className="movie-field movie-field--short">Target length (seconds)<input type="number" min={10} max={600} step={5} value={targetSeconds} placeholder="all of it" onChange={(event) => setTargetSeconds(event.target.value === '' ? '' : Number(event.target.value))} /></label>}<Button disabled={busy || savingSequence || planning || !brief.trim() || shotCount < 1 || shotCount > 40} onClick={async () => {
          setPlanning(true); setPlanError(null)
          try {
            const suggestion = await movieApi.plan(movie.movie_id, { expected_revision: movie.revision, brief, style, shot_count: shotCount, ...(targetSeconds === '' ? {} : { target_seconds: targetSeconds }) })
            if (suggestion.characters?.length) {
              const saved = await run(() => movieApi.characters(movie.movie_id, movie.revision, suggestion.characters!))
              if (!saved) return
            }
            if (suggestion.style && !style.trim()) setStyle(suggestion.style)
            setShots(suggestion.shots.map(shotFields)); setSaved(false)
          }
          catch (error) { setPlanError(error instanceof Error ? error.message : 'The shot plan could not be suggested.') }
          finally { setPlanning(false) }
        }}>{planning ? 'Planning…' : capabilities.director_available ? 'Break down the script' : 'Suggest shot plan'}</Button></div>{planError && <p role="alert">{planError}</p>}</div>}
        {shots.length === 0 && <div className="movie-empty"><span className="movie-empty-mark" aria-hidden="true">01</span><h3>Start with one strong moment</h3><p>Give each shot one clear action. Add a new scene for parallel work, or continue a shot to preserve its final frame.</p></div>}
        {shots.map((shot, index) => (shot.shot_id === (shots.some((item) => item.shot_id === activeShotId) ? activeShotId : shots[0]?.shot_id)) && <fieldset className="movie-panel movie-shot-definition" id={`shot-definition-${shot.shot_id}`} key={shot.shot_id} disabled={busy || savingSequence}>
          <legend>Shot {String(index + 1).padStart(2, '0')}</legend>
          <MovieShotMedia key={shot.shot_id} movie={movie} capabilities={capabilities} busy={busy || savingSequence} run={run} shot={{ opening_asset_key: null, opening_approved: false, takes: [], selected_take_id: null, ...movie.shots.find((item) => item.shot_id === shot.shot_id), ...shot }} onImage={(file) => {
            if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 20_000_000) { setReferenceError('Choose a PNG, JPEG or WebP image smaller than 20 MB.'); return }
            setSequence((current) => ({ ...current, [shot.shot_id]: file })); setSaved(false); setReferenceError(null)
          }} pendingImage={sequencePreviews[shot.shot_id] ?? (referenceShotId === shot.shot_id && reference ? referencePreview : null)} />
          {sequence[shot.shot_id] && <p role="status">{sequence[shot.shot_id].name} · saves with your story</p>}
          <div className="movie-fields-row"><label className="movie-field">Shot name<input required maxLength={160} value={shot.name} onChange={(e) => edit(shot.shot_id, { name: e.target.value })} /></label><label className="movie-field movie-field--short">Duration (seconds)<input type="number" required min={capabilities?.min_duration_seconds ?? 1.5} max={capabilities?.max_duration_seconds ?? MAX_SHOT_SECONDS} step={capabilities?.duration_step_seconds ?? 0.5} value={shot.duration_seconds} disabled={(shot.dialogue?.length ?? 0) > 0} title={(shot.dialogue?.length ?? 0) > 0 ? 'A dialogue shot lasts as long as its lines need.' : undefined} onChange={(e) => edit(shot.shot_id, { duration_seconds: Number(e.target.value) })} /></label></div>
          <label className="movie-field">Action & camera movement<textarea required rows={3} maxLength={1600} value={shot.prompt} onChange={(e) => edit(shot.shot_id, { prompt: e.target.value })} placeholder="Describe the action during this clip, keeping it achievable in a few seconds." /></label>
          <label className="movie-field">Opening frame direction<textarea rows={2} maxLength={2000} value={shot.opening_prompt} onChange={(e) => edit(shot.shot_id, { opening_prompt: e.target.value })} placeholder="Only what is visible before the action starts: framing, pose, subjects and surroundings." /></label>
          {capabilities?.dialogue_available !== false && <div className="movie-dialogue" role="group" aria-label={`Dialogue for ${shot.name}`}>
            <div className="movie-section-title"><h4>Dialogue</h4><span className="movie-muted">{shot.dialogue?.length ? `Spoken with each character’s own voice · up to ${capabilities?.max_dialogue_seconds ?? 15}s, ${capabilities?.max_speakers_per_shot ?? 2} speakers · ends on the shot’s image` : 'Add lines to make this a spoken shot.'}</span></div>
            {(shot.dialogue ?? []).map((line, lineIndex) => <div className="movie-dialogue-line" key={lineIndex}>
              <label className="movie-field movie-field--short">Character<select required aria-label={`Line ${lineIndex + 1} character`} value={line.character_id} onChange={(e) => editLine(shot.shot_id, lineIndex, { character_id: e.target.value })}><option value="">Choose…</option>{voiced.map((c) => <option key={c.character_id} value={c.character_id}>{c.name}</option>)}</select></label>
              <label className="movie-field">Line<input required maxLength={600} aria-label={`Line ${lineIndex + 1} words`} value={line.text} onChange={(e) => editLine(shot.shot_id, lineIndex, { text: e.target.value })} placeholder="The exact words, as written." /></label>
              <label className="movie-field movie-field--short">Delivery<input maxLength={200} aria-label={`Line ${lineIndex + 1} delivery`} value={line.delivery ?? ''} onChange={(e) => editLine(shot.shot_id, lineIndex, { delivery: e.target.value })} placeholder="quietly" /></label>
              <Button aria-label={`Remove line ${lineIndex + 1}`} onClick={() => editLine(shot.shot_id, lineIndex, null)}>Remove</Button>
            </div>)}
            <div className="movie-actions">
              <Button disabled={!voiced.length || (shot.dialogue?.length ?? 0) >= 12} title={voiced.length ? undefined : 'Save a character with a voice clip first.'} onClick={() => edit(shot.shot_id, { dialogue: [...(shot.dialogue ?? []), { character_id: voiced[0]?.character_id ?? '', text: '', delivery: '' }] })}>+ Add line</Button>
              {!voiced.length && <span className="movie-muted">Add a character with a voice clip under Cast & voices to write dialogue.</span>}
            </div>
            {(shot.dialogue?.length ?? 0) > 0 && <label className="movie-field">Soundscape<input maxLength={400} value={shot.soundscape ?? ''} onChange={(e) => edit(shot.shot_id, { soundscape: e.target.value })} placeholder="Room tone under the voices, e.g. distant engine hum, no music" /></label>}
          </div>}
          <div className="movie-fields-row"><label className="movie-field">Continuity<select value={shot.continuation_of ?? ''} onChange={(e) => edit(shot.shot_id, { continuation_of: e.target.value || null })}><option value="">New scene · can generate independently</option>{shots.slice(0, index).map((previous) => <option key={previous.shot_id} value={previous.shot_id}>Continue from {previous.name}</option>)}</select></label><Button disabled={shots.some((other) => other.continuation_of === shot.shot_id)} title={shots.some((other) => other.continuation_of === shot.shot_id) ? 'Change dependent shots to a new scene before removing this shot.' : undefined} onClick={() => { setSaved(false); setShots(shots.filter((item) => item.shot_id !== shot.shot_id)); setSequence((current) => { const next = { ...current }; delete next[shot.shot_id]; return next }) }}>Remove shot {index + 1}</Button></div>
          {(shot.dialogue?.length ?? 0) === 0 && <details className="movie-motion-direction">
            <summary>Motion direction</summary>
            <div className="movie-fields-row">
              <label className="movie-field">Camera<input maxLength={300} value={shot.camera ?? ''} onChange={(e) => edit(shot.shot_id, { camera: e.target.value })} placeholder="One move, e.g. The camera pushes in with small amplitude at slow speed." /></label>
              <label className="movie-field">Stays still<input maxLength={400} value={shot.still ?? ''} onChange={(e) => edit(shot.shot_id, { still: e.target.value })} placeholder="What in the starting image must not change, e.g. the hull walls and lights" /></label>
            </div>
            {(shot.beats?.length ?? 0) > 0 && <ul className="movie-beats" aria-label={`Timed beats for ${shot.name}`}>
              {(shot.beats ?? []).map((beat, beatIndex) => <li key={beatIndex}>
                <span>{beat.at_seconds}s · {beat.action}</span>
                {beat.at_seconds >= shot.duration_seconds && <span className="movie-muted" role="note"> · past the shot's end, removed when you save</span>}
                <Button aria-label={`Remove beat at ${beat.at_seconds} seconds`} onClick={() => edit(shot.shot_id, { beats: (shot.beats ?? []).filter((_, i) => i !== beatIndex) })}>Remove</Button>
              </li>)}
            </ul>}
          </details>}
        </fieldset>)}
        <Button disabled={busy || savingSequence || planning || shots.length >= 40} onClick={() => { const id = crypto.randomUUID(); setActiveShotId(id); setSaved(false); setShots([...shots, newShot(id, `Shot ${shots.length + 1}`)]) }}>+ Add shot</Button>
        {capabilities?.dialogue_available !== false && <MovieCast movie={movie} capabilities={capabilities} busy={busy || savingSequence || planning} run={run} />}
      </div>
      <MovieSequence activeId={shots.some((shot) => shot.shot_id === activeShotId) ? activeShotId : shots[0]?.shot_id} disabled={busy || savingSequence || planning}
        help="Drag shots into story order. Click a shot to edit it. With a shot focused, use ↑ or ↓ to reorder."
        empty={<p>Add shots or upload an image sequence to start arranging your movie.</p>}
        items={shots.map((shot) => {
          const key = movie.shots.find((item) => item.shot_id === shot.shot_id)?.opening_asset_key
          return { id: shot.shot_id, name: shot.name, detail: `${shot.duration_seconds}s${shot.continuation_of ? ' · Continuation' : ''}`, image: sequencePreviews[shot.shot_id] ?? (referenceShotId === shot.shot_id && reference ? referencePreview ?? undefined : key ? movieApi.assetUrl(movie.movie_id, key) : undefined) }
        })} onMove={moveShot} onOpen={(id) => {
        setActiveShotId(id)
      }} />
    </form>
  </section>
}

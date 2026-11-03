import { useState } from 'react'
import { Button } from '../../components/ui/Button'
import { movieApi, type CharacterDefinition, type Movie } from './movieApi'
import type { MovieWorkspaceProps } from './MovieRoute'

const characterFields = (movie: Movie): CharacterDefinition[] => (movie.characters ?? []).map(({ character_id, name, appearance, voice, possessive, voice_asset_key, audition_line }) => ({ character_id, name, appearance, voice, possessive, voice_asset_key, ...(audition_line ? { audition_line } : {}) }))

/** The cast: who can speak, what they look like, and the clip of their own voice every take is anchored to. */
export function MovieCast({ movie, capabilities, busy, run }: MovieWorkspaceProps) {
  const [cast, setCast] = useState<CharacterDefinition[]>(characterFields(movie))
  const [clips, setClips] = useState<Record<string, File>>({})
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const frozen = Object.fromEntries((movie.characters ?? []).map((c) => [c.character_id, c]))
  const minSeconds = capabilities?.voice_min_seconds ?? 2
  const maxSeconds = capabilities?.voice_max_seconds ?? 15
  const dirty = JSON.stringify(cast) !== JSON.stringify(characterFields(movie)) || Object.keys(clips).length > 0
  function edit(id: string, patch: Partial<CharacterDefinition>) { setSaved(false); setCast((current) => current.map((c) => c.character_id === id ? { ...c, ...patch } : c)) }
  function chooseClip(id: string, file?: File) {
    if (!file) return
    if (!['audio/wav', 'audio/x-wav', 'audio/mpeg'].includes(file.type) || file.size > 50_000_000) { setError('Choose a WAV or MP3 clip smaller than 50 MB.'); return }
    setError(null); setSaved(false); setClips((current) => ({ ...current, [id]: file }))
  }
  async function save() {
    if (cast.some((c) => !c.name.trim())) { setError('Give every character a name.'); return }
    setSaving(true); setError(null)
    try {
      let revision = movie.revision
      const characters = [...cast]
      for (const [id, file] of Object.entries(clips)) {
        const target = await movieApi.upload(movie.movie_id, file)
        revision = target.revision
        const index = characters.findIndex((c) => c.character_id === id)
        if (index >= 0) characters[index] = { ...characters[index], voice_asset_key: target.asset_key }
      }
      const ok = await run(async () => {
        const result = await movieApi.characters(movie.movie_id, revision, characters.map((c) => { if (c.audition_line?.trim()) return c; const rest = { ...c }; delete rest.audition_line; return rest }))
        setCast(characterFields(result))
        return result
      })
      if (!ok) return
      setClips({}); setSaved(true)
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'The cast could not be saved.') }
    finally { setSaving(false) }
  }
  return <section className="movie-panel movie-cast" aria-labelledby="movie-cast-title">
    <div className="movie-section-title"><h3 id="movie-cast-title">Cast & voices</h3><Button disabled={busy || saving || !dirty} onClick={() => void save()}>{saving ? 'Saving…' : 'Save cast'}</Button></div>
    <p className="movie-muted">Each character speaks with their own voice: upload {minSeconds}–{maxSeconds} seconds of them talking alone, no music. Their lines are performed with that voice, lips and speech together. A composed portrait is the reference sheet every storyboard frame draws them from.</p>
    {error && <p role="alert" className="movie-notice movie-notice--error">{error}</p>}
    {saved && <span role="status" className="sr-only">Cast saved.</span>}
    {cast.map((c) => <fieldset className="movie-character" key={c.character_id} disabled={busy || saving}>
      <legend>{c.name || 'New character'}</legend>
      <div className="movie-fields-row">
        <label className="movie-field">Name<input required maxLength={120} value={c.name} onChange={(e) => edit(c.character_id, { name: e.target.value })} /></label>
        <label className="movie-field movie-field--short">Refer to them as<select value={c.possessive} onChange={(e) => edit(c.character_id, { possessive: e.target.value as CharacterDefinition['possessive'] })}><option value="their">they</option><option value="her">she</option><option value="his">he</option></select></label>
      </div>
      <label className="movie-field">Appearance<textarea rows={2} maxLength={600} value={c.appearance} onChange={(e) => edit(c.character_id, { appearance: e.target.value })} placeholder="Face, hair, clothing — what must stay the same in every shot." /></label>
      <label className="movie-field">Voice<input maxLength={300} value={c.voice} onChange={(e) => edit(c.character_id, { voice: e.target.value })} placeholder="e.g. low, unhurried alto with a soft northern accent" /></label>
      <label className="movie-field">Audition line<input maxLength={240} value={c.audition_line ?? ''} onChange={(e) => edit(c.character_id, { audition_line: e.target.value })} placeholder="What they say at the audition (a plain sentence or two, no names)" /></label>
      {frozen[c.character_id]?.portrait_asset_key && <img className="movie-portrait" src={movieApi.assetUrl(movie.movie_id, frozen[c.character_id].portrait_asset_key!)} alt={`${c.name} reference sheet`} />}
      <div className="movie-actions">
        <Button disabled={busy || saving || dirty || !c.appearance.trim() || Object.values(movie.compositions ?? {}).some((item) => item.kind === 'portrait' && item.character_id === c.character_id && item.status === 'pending')} title={dirty ? 'Save the cast first.' : !c.appearance.trim() ? 'Describe the appearance first.' : undefined} onClick={() => void run(() => movieApi.portrait(movie.movie_id, c.character_id, movie.revision, crypto.randomUUID()))}>{frozen[c.character_id]?.portrait_asset_key ? 'Compose another portrait' : 'Compose portrait'}</Button>
        {Object.values(movie.compositions ?? {}).some((item) => item.kind === 'portrait' && item.character_id === c.character_id && item.status === 'pending') && <span role="status">Composing portrait…</span>}
        <Button disabled={busy || saving || dirty || !c.voice.trim() || Object.values(movie.compositions ?? {}).some((item) => item.kind === 'audition' && item.character_id === c.character_id && item.status === 'pending')} title={dirty ? 'Save the cast first.' : !c.voice.trim() ? 'Describe the voice first.' : 'A short solo take from the voice description; its audio becomes the voice reference.'} onClick={() => void run(() => movieApi.audition(movie.movie_id, c.character_id, movie.revision, crypto.randomUUID()))}>{frozen[c.character_id]?.voice_asset_key ? 'Audition again' : 'Audition voice'}</Button>
        {Object.values(movie.compositions ?? {}).some((item) => item.kind === 'audition' && item.character_id === c.character_id && item.status === 'pending') && <span role="status">Auditioning…</span>}
        <label className="movie-upload">{c.voice_asset_key || clips[c.character_id] ? 'Replace voice clip' : 'Upload voice clip'}<input type="file" accept="audio/wav,audio/mpeg,.wav,.mp3" aria-label={`Upload voice clip for ${c.name || 'new character'}`} onChange={(e) => { chooseClip(c.character_id, e.target.files?.[0]); e.target.value = '' }} /></label>
        {clips[c.character_id] ? <span role="status">{clips[c.character_id].name} · saves with the cast</span>
          : frozen[c.character_id]?.voice_asset_key ? <span className="movie-muted">Voice reference · {frozen[c.character_id].voice_seconds?.toFixed(1) ?? '?'} s</span>
          : <span className="movie-muted">No voice yet — this character cannot speak.</span>}
        <Button onClick={() => { setSaved(false); setCast(cast.filter((item) => item.character_id !== c.character_id)); setClips((current) => { const next = { ...current }; delete next[c.character_id]; return next }) }}>Remove {c.name || 'character'}</Button>
      </div>
    </fieldset>)}
    <Button disabled={busy || saving || cast.length >= 8} onClick={() => { setSaved(false); setCast([...cast, { character_id: crypto.randomUUID(), name: '', appearance: '', voice: '', possessive: 'their', voice_asset_key: null }]) }}>+ Add character</Button>
  </section>
}

/* eslint-disable jsx-a11y/media-has-caption -- User-supplied audio cues are being edited; this input does not supply a transcript. */
import { useState } from 'react'
import { Button } from '../../components/ui/Button'
import { movieApi, type AudioTrack } from './movieApi'
import type { MovieWorkspaceProps } from './MovieRoute'
import { useDraftRevision } from './useDraftRevision'

export function MovieSoundtracks({ movie, busy, run }: MovieWorkspaceProps) {
  const [tracks, setTracks] = useState<AudioTrack[]>(movie.soundtracks ?? [])
  const [dirty, setDirty] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const revision = useDraftRevision(movie.soundtracks ?? [], movie.revision)
  function patch(index: number, value: Partial<AudioTrack>) { setDirty(true); setTracks(tracks.map((track, position) => position === index ? { ...track, ...value } : track)) }
  async function upload(file?: File) {
    if (!file) return
    if (file.size > 50 * 1024 * 1024) { setError('Choose an MP3 or WAV smaller than 50 MB.'); return }
    setUploading(true); setError(null)
    try {
      const target = await movieApi.upload(movie.movie_id, file)
      const current = await movieApi.get(movie.movie_id)
      if (!revision.matches(current.soundtracks ?? [])) throw new Error('The soundtrack changed elsewhere. Reload the movie before adding audio.')
      const next = [...tracks, { asset_key: target.asset_key, offset_seconds: 0, trim_start_seconds: 0, gain_db: -7 }]
      if (await run(async () => {
        const result = await movieApi.soundtracks(movie.movie_id, current.revision, next)
        revision.committed(result.soundtracks ?? [], result.revision)
        setTracks(result.soundtracks ?? []); setDirty(false)
        return result
      })) setDirty(false)
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Audio upload failed.') }
    finally { setUploading(false) }
  }
  return <section className="movie-panel movie-soundtracks" aria-labelledby="movie-soundtracks-title"><div className="movie-section-title"><h3 id="movie-soundtracks-title">Music & narration</h3><span>{tracks.length} of 8 cues</span></div><p>Add your score or voiceover. It plays under the takes’ own sound.</p>
    {tracks.map((track, index) => <fieldset key={track.asset_key + index} className="movie-audio-cue" disabled={busy || uploading}><legend>Audio cue {index + 1}</legend><audio controls preload="metadata" src={movieApi.assetUrl(movie.movie_id, track.asset_key)} aria-label={`Audio cue ${index + 1}`} /><div className="movie-fields-row"><label className="movie-field">Start in movie (seconds)<input type="number" min={0} max={3600} step={.1} value={track.offset_seconds} onChange={(e) => patch(index, { offset_seconds: Number(e.target.value) })} /></label><label className="movie-field">Trim audio start (seconds)<input type="number" min={0} max={3600} step={.1} value={track.trim_start_seconds} onChange={(e) => patch(index, { trim_start_seconds: Number(e.target.value) })} /></label><label className="movie-field">Volume (dB)<input type="number" min={-60} max={12} value={track.gain_db} onChange={(e) => patch(index, { gain_db: Number(e.target.value) })} /></label></div>
      <div className="movie-fields-row"><label className="movie-field">End in movie (seconds)<input type="number" min={0} max={3600} step={.1} aria-label={`Audio cue ${index + 1} end`} value={track.end_seconds ?? ''} placeholder="plays to its end" onChange={(e) => patch(index, { end_seconds: e.target.value === '' ? null : Number(e.target.value) })} /></label><label className="movie-field">Fade in (seconds)<input type="number" min={0} max={30} step={.1} value={track.fade_in_seconds ?? 0} onChange={(e) => patch(index, { fade_in_seconds: Number(e.target.value) })} /></label><label className="movie-field">Fade out (seconds)<input type="number" min={0} max={30} step={.1} value={track.fade_out_seconds ?? 0} onChange={(e) => patch(index, { fade_out_seconds: Number(e.target.value) })} /></label></div>
      <div className="movie-fields-row"><label className="movie-field">Swell (dB by the end)<input type="number" min={-30} max={30} value={track.swell_db ?? 0} onChange={(e) => patch(index, { swell_db: Number(e.target.value) })} /></label><label className="movie-field">Swell over (seconds)<input type="number" min={0} max={60} step={.5} value={track.swell_seconds ?? 0} onChange={(e) => patch(index, { swell_seconds: Number(e.target.value) })} /></label></div><p className="movie-muted">A swell rises to its level over the last seconds before the cue ends — a crescendo into what follows. Cues duck under the takes’ dialogue, and the finished movie is mastered to −16 LUFS.</p><Button onClick={() => { setTracks(tracks.filter((_, position) => position !== index)); setDirty(true) }}>Remove audio cue {index + 1}</Button></fieldset>)}
    <div className="movie-actions"><label className="movie-upload">{uploading ? 'Uploading audio…' : 'Add music or narration'}<input type="file" aria-label="Add music or narration" accept="audio/mpeg,audio/wav,.mp3,.wav" disabled={busy || uploading || tracks.length >= 8 || dirty} onChange={(e) => { void upload(e.target.files?.[0]); e.target.value = '' }} /></label><Button disabled={busy || uploading || !dirty} onClick={async () => {
      if (await run(async () => { const result = await movieApi.soundtracks(movie.movie_id, revision.expectedRevision, tracks); revision.committed(result.soundtracks ?? [], result.revision); setTracks(result.soundtracks ?? []); return result })) setDirty(false)
    }}>Save soundtrack</Button></div>{dirty && <p role="status">Save soundtrack changes, then assemble the movie to hear the mix.</p>}{error && <p role="alert">{error}</p>}
  </section>
}

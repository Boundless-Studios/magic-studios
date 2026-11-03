/* eslint-disable jsx-a11y/media-has-caption -- This editor previews the user's own takes before a caption track has been authored. */
import { useEffect, useRef } from 'react'

export interface PlayerClip { name: string; src: string; in_seconds: number; out_seconds: number; mute?: boolean }

/** Plays the clips back to back in sequence order, each within its trim, with
 * hard cuts. A clip the assembly silences plays muted. The assembled movie
 * adds the dissolves and the soundtrack. */
export function MovieClipPlayer({ clips, index, onIndex }: { clips: PlayerClip[]; index: number; onIndex: (index: number) => void }) {
  const video = useRef<HTMLVideoElement>(null)
  const playing = useRef(false)
  // The viewer's own mute, kept across clips the assembly does not silence.
  const viewerMuted = useRef(false)
  const clip = clips[index]
  const src = clip?.src
  const startAt = clip?.in_seconds ?? 0
  const silent = Boolean(clip?.mute)
  useEffect(() => { if (video.current) video.current.muted = silent || viewerMuted.current }, [silent, index])
  useEffect(() => {
    const element = video.current
    if (!element || !src) return
    const start = () => {
      element.currentTime = startAt
      if (playing.current) void element.play()?.catch(() => { playing.current = false })
    }
    // A new source reloads the element (readyState drops to 0); the same take
    // used again only needs a seek.
    if (element.readyState >= HTMLMediaElement.HAVE_METADATA) { start(); return }
    element.addEventListener('loadedmetadata', start, { once: true })
    return () => element.removeEventListener('loadedmetadata', start)
  }, [src, startAt, index])
  function advance() {
    if (index < clips.length - 1) onIndex(index + 1)
    else { playing.current = false; video.current?.pause() }
  }
  // Keep scrubbing inside the trim; past the end stops just short so playback
  // reaches the end and moves on.
  function clamp(element: HTMLVideoElement) {
    if (element.currentTime < clip.in_seconds - 0.01) element.currentTime = clip.in_seconds
    else if (element.currentTime > clip.out_seconds - 0.1) element.currentTime = Math.max(clip.in_seconds, clip.out_seconds - 0.1)
  }
  if (!clip) return null
  return <figure className="movie-clip-player">
    <video ref={video} controls playsInline preload="metadata" src={clip.src} aria-label="Movie preview, clips in order" data-clip-index={index}
      onPlay={(event) => { playing.current = true; if (event.currentTarget.currentTime >= clip.out_seconds - 0.05) event.currentTarget.currentTime = clip.in_seconds }}
      onPause={(event) => { if (!event.currentTarget.ended && event.currentTarget.currentTime < clip.out_seconds - 0.05) playing.current = false }}
      onSeeking={(event) => clamp(event.currentTarget)}
      onVolumeChange={(event) => { if (silent) event.currentTarget.muted = true; else viewerMuted.current = event.currentTarget.muted }}
      onTimeUpdate={(event) => { const element = event.currentTarget; if (element.seeking) return; if (element.currentTime < clip.in_seconds - 0.01) element.currentTime = clip.in_seconds; else if (element.currentTime >= clip.out_seconds - 0.05) advance() }}
      onEnded={advance} />
    <figcaption><span role="status">Clip {index + 1} of {clips.length} · {clip.name}{silent ? ' · silent' : ''}</span><button type="button" className="button" onClick={() => { playing.current = true; if (index === 0) { const element = video.current; if (element) { element.currentTime = clip.in_seconds; void element.play()?.catch(() => { playing.current = false }) } } else onIndex(0) }}>Play all from the start</button></figcaption>
  </figure>
}

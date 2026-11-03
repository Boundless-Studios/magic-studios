import { useEffect, useRef } from 'react'

import { siteMediaUrl } from '../../siteMedia'

/** The 2026-10-18 live scene-flight proof (its note and media are in the private
 * art-archive-2026-11-02 release): Harrowfen to Miretrace, one 5 s leg, shown as generated
 * with its audio track dropped. These media are staged from the pinned site-media release
 * (see `siteMedia.ts`), so only builds with a media base render these demos.
 * The WYRM intro is the v5 cinematic the WYRM page already serves from `/wyrm/media/`. */
const flightFrom = siteMediaUrl('welcome/flight-from.jpg')
const flightPoster = siteMediaUrl('welcome/flight-poster.jpg')
const flightTo = siteMediaUrl('welcome/flight-to.jpg')
const flightVideo = siteMediaUrl('welcome/flight.mp4')
const wyrmCaptions = siteMediaUrl('welcome/wyrm-intro.en.vtt')
const WYRM_INTRO = '/wyrm/media/wyrm-trailer-v5.mp4'
const WYRM_POSTER = '/wyrm/media/trailer-poster.jpg'

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)'

function reducedMotionQuery() {
  return typeof window === 'undefined' ? undefined : window.matchMedia?.(REDUCED_MOTION)
}

export function FlightDemo() {
  const video = useRef<HTMLVideoElement>(null)
  const still = reducedMotionQuery()?.matches === true
  // Turning reduced motion on while the page is open pauses it; the controls can start it again.
  useEffect(() => {
    const query = reducedMotionQuery()
    if (!query?.addEventListener) return
    const pauseIfReduced = (event: MediaQueryListEvent) => { if (event.matches) video.current?.pause() }
    query.addEventListener('change', pauseIfReduced)
    return () => query.removeEventListener('change', pauseIfReduced)
  }, [])
  return (
    <figure className="demo" aria-label="Example scene flight">
      <video
        aria-label="Scene flight from the village street to the marsh, 5 seconds"
        autoPlay={!still}
        className="demo__video"
        controls
        loop
        muted
        playsInline
        poster={flightPoster}
        preload={still ? 'none' : 'metadata'}
        ref={video}
        src={flightVideo}
      />
      <div className="demo__inputs">
        <img src={flightFrom} alt="Start: a muddy village street under a dead tree" loading="lazy" />
        <span aria-hidden="true">→</span>
        <img src={flightTo} alt="End: a hut and a stone well in a misty marsh" loading="lazy" />
      </div>
      <figcaption>Two location images in, one 5-second camera move out.</figcaption>
    </figure>
  )
}

export function VideoDemo() {
  return (
    <figure className="demo" aria-label="Example video">
      <video
        aria-label="WYRM intro, 1 minute 36 seconds"
        className="demo__video"
        controls
        playsInline
        poster={WYRM_POSTER}
        preload="none"
        src={WYRM_INTRO}
      >
        <track default kind="captions" label="English" src={wyrmCaptions} srcLang="en" />
      </video>
      <figcaption>The intro to WYRM, our game in development.</figcaption>
    </figure>
  )
}

import { useEffect, useState } from 'react'

import { siteMediaUrl } from '../../siteMedia'

const sheet = (heading: string) => siteMediaUrl(`welcome/orc-walk-${heading}.webp`)

/** One orc in eight directions, all Studio exports from the project "Orc turntable walk"
 * (2026-10-23). Every view is a frame of one turntable video of the supplied SE image, so the
 * eight share its colours and build. Each view was checked by hand (axe in the right hand,
 * pauldron on the left shoulder, same face) before its walk was generated (standard H3, 20
 * steps) and saved as a 16-frame loop. Sheets are lossy WebP of the exported PNGs, staged from
 * the pinned site-media release under its versioned, immutable `/assets/site-media/<n>/` prefix
 * (see `siteMedia.ts`), so only builds with a media base render this showcase. */
const DIRECTIONS = [
  { heading: 'N', arrow: '↑', name: 'north', sheet: sheet('n'), frameMs: 94 },
  { heading: 'NE', arrow: '↗', name: 'northeast', sheet: sheet('ne'), frameMs: 91 },
  { heading: 'E', arrow: '→', name: 'east', sheet: sheet('e'), frameMs: 94 },
  { heading: 'SE', arrow: '↘', name: 'southeast', sheet: sheet('se'), frameMs: 78 },
  { heading: 'S', arrow: '↓', name: 'south', sheet: sheet('s'), frameMs: 107 },
  { heading: 'SW', arrow: '↙', name: 'southwest', sheet: sheet('sw'), frameMs: 99 },
  { heading: 'W', arrow: '←', name: 'west', sheet: sheet('w'), frameMs: 109 },
  { heading: 'NW', arrow: '↖', name: 'northwest', sheet: sheet('nw'), frameMs: 89 },
] as const
type Direction = (typeof DIRECTIONS)[number]
const FRAMES = 16
const CELL = 256

const BACKDROPS = [
  { id: 'transparent', label: 'Transparent' },
  { id: 'light', label: 'Light' },
  { id: 'dark', label: 'Dark' },
] as const
type Backdrop = (typeof BACKDROPS)[number]['id']

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)'

function reducedMotionQuery() {
  return typeof window === 'undefined' ? undefined : window.matchMedia?.(REDUCED_MOTION)
}

function spriteStyle(direction: Direction) {
  return {
    '--sheet': `url(${direction.sheet})`,
    '--frames': FRAMES,
    '--duration': `${FRAMES * direction.frameMs}ms`,
  } as React.CSSProperties
}

export function SpriteShowcase() {
  const [backdrop, setBackdrop] = useState<Backdrop>('transparent')
  const [selected, setSelected] = useState<Direction>(DIRECTIONS[3])
  const [playing, setPlaying] = useState(() => reducedMotionQuery()?.matches !== true)
  // Turning reduced motion on while the page is open pauses it; turning it off leaves the choice to the visitor.
  useEffect(() => {
    const query = reducedMotionQuery()
    if (!query?.addEventListener) return
    const pauseIfReduced = (event: MediaQueryListEvent) => { if (event.matches) setPlaying(false) }
    query.addEventListener('change', pauseIfReduced)
    return () => query.removeEventListener('change', pauseIfReduced)
  }, [])
  return (
    <figure className={`sprite-showcase sprite-showcase--${backdrop}`} aria-label="Example animation made in Magic Studios">
      <div className="sprite-showcase__stage">
        <div
          className="sprite-showcase__sprite"
          role="img"
          aria-label={`Orc walking ${selected.name}, ${FRAMES} frames`}
          data-playing={playing}
          style={spriteStyle(selected)}
        />
      </div>
      <div className="sprite-showcase__directions" role="group" aria-label="Direction">
        {DIRECTIONS.map((direction) => (
          <button
            key={direction.heading}
            type="button"
            className="sprite-showcase__direction"
            aria-label={`Walk ${direction.name}`}
            aria-pressed={selected.heading === direction.heading}
            onClick={() => setSelected(direction)}
          >
            <span className="sprite-showcase__sprite sprite-showcase__sprite--small" data-playing={playing} style={spriteStyle(direction)} />
            <span aria-hidden="true">{direction.arrow} {direction.heading}</span>
          </button>
        ))}
      </div>
      <div className="sprite-showcase__controls">
        <button type="button" className="button" aria-pressed={!playing} onClick={() => setPlaying(!playing)}>
          {playing ? 'Pause' : 'Play'}
        </button>
        <span role="group" aria-label="Background">
          {BACKDROPS.map(({ id, label }) => (
            <button key={id} type="button" className="button" aria-pressed={backdrop === id} onClick={() => setBackdrop(id)}>{label}</button>
          ))}
        </span>
      </div>
      <figcaption>
        Made in Magic Studios from one image. Studio turned it around to get every direction, then animated each walk: {FRAMES} frames, {CELL} px, transparent.
      </figcaption>
    </figure>
  )
}

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { siteMediaEnabled } from '../../siteMedia'
import { FlightDemo, VideoDemo } from './Demos'
import { SpriteShowcase } from './SpriteShowcase'

/** The front page's four modes. With the hosted media (`siteMedia.ts`) each mode has one real
 * example, in tabs that advance on their own until the visitor picks one, presses Pause, or
 * prefers reduced motion. Hovering or focusing the showcase holds it, and so does a video that
 * is playing, so nothing is cut off mid-play. Public and self-hosted builds have no media, so
 * they list the modes and show the one example that needs none: the agent session. */
export const MODES = [
  { id: 'characters', title: 'Characters', line: 'One image becomes animations in eight directions.' },
  { id: 'movies', title: 'Movies', line: 'Shots from your images, cut together with music and voices.' },
  { id: 'flights', title: 'Scene flights', line: 'A camera move between two images of a place.' },
  { id: 'agents', title: 'Agents', line: 'Your AI agent does all of it through the same API.' },
] as const
type ModeId = (typeof MODES)[number]['id']

export const ADVANCE_MS = 8000
const REDUCED_MOTION = '(prefers-reduced-motion: reduce)'

function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia?.(REDUCED_MOTION)?.matches === true
}

function AgentDemo() {
  return (
    <figure className="agent-demo" aria-label="Example agent session">
      <div className="agent-demo__terminal">
        <p className="agent-demo__label">Connect once</p>
        <pre><code>claude mcp add magic-studios --env MAGIC_STUDIOS_API_KEY=mvs_… -- magic-studios-mcp</code></pre>
        <p className="agent-demo__label">Then ask</p>
        <pre><code>Animate this orc walking in all eight directions, then make a 5-second flight from the village to the marsh.</code></pre>
        <p className="agent-demo__label">Some of the tools it can use</p>
        <pre><code>{'create_project, generate_subject, add_recommended_motions, generate_motions, wait_for_operations,\nsuggest_motion_loop, create_frame_revision, create_release, export_release,\ncreate_movie, build_movie, assemble_movie, create_scene_flight, generate_scene_flight_leg, …'}</code></pre>
      </div>
      <figcaption>
        Every action in the studio is an API call. Use the MCP server with Claude Code, Codex or Claude Desktop, or call the <a className="policy-link" href="/studio/reference">API</a> from any script. <Link className="policy-link" to="/guide#agents">Connect an agent</Link>.
      </figcaption>
    </figure>
  )
}

function ModePanel({ id }: { id: ModeId }) {
  if (id === 'characters') return <SpriteShowcase />
  if (id === 'movies') return <VideoDemo />
  if (id === 'flights') return <FlightDemo />
  return <AgentDemo />
}

export function ModeShowcase() {
  return siteMediaEnabled ? <ModeTabs /> : <ModeList />
}

function ModeList() {
  return (
    <section className="mode-showcase" aria-label="What Magic Studios makes">
      <ul className="mode-showcase__tabs mode-showcase__list" aria-label="Modes">
        {MODES.map((item) => (
          <li key={item.id} className="mode-showcase__tab mode-showcase__mode">
            <strong>{item.title}</strong>
            <span>{item.line}</span>
          </li>
        ))}
      </ul>
      <div className="mode-showcase__panel mode-showcase__panel--fit">
        <AgentDemo />
      </div>
    </section>
  )
}

function ModeTabs() {
  const [selected, setSelected] = useState(0)
  const [rotating, setRotating] = useState(() => !prefersReducedMotion())
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [mediaPlaying, setMediaPlaying] = useState(false)
  const [panelHeight, setPanelHeight] = useState(0)
  const tabs = useRef<Array<HTMLButtonElement | null>>([])
  const panel = useRef<HTMLDivElement>(null)
  const remaining = useRef(ADVANCE_MS)
  const active = rotating && !hovered && !focused && !mediaPlaying

  // A new mode gets the full interval. Declared before the timer so it runs first.
  useEffect(() => { remaining.current = ADVANCE_MS }, [selected])

  // A hold keeps the time already spent, so the paused progress bar still predicts the change.
  useEffect(() => {
    if (!active) return
    const started = Date.now()
    const timer = window.setTimeout(() => setSelected((index) => (index + 1) % MODES.length), remaining.current)
    return () => {
      window.clearTimeout(timer)
      remaining.current = Math.max(0, remaining.current - (Date.now() - started))
    }
  }, [active, selected])

  // The panel keeps the tallest height it has shown, so rotating never shifts the page below it.
  useEffect(() => {
    const element = panel.current
    if (!element || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => setPanelHeight((height) => Math.max(height, element.offsetHeight)))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  // Turning reduced motion on while the page is open stops the rotation for good.
  useEffect(() => {
    const query = typeof window === 'undefined' ? undefined : window.matchMedia?.(REDUCED_MOTION)
    if (!query?.addEventListener) return
    const stopIfReduced = (event: MediaQueryListEvent) => { if (event.matches) setRotating(false) }
    query.addEventListener('change', stopIfReduced)
    return () => query.removeEventListener('change', stopIfReduced)
  }, [])

  // Media events do not bubble, so listen in the capture phase. A muted looping preview
  // (the scene flight) is background motion, not something the visitor is watching.
  useEffect(() => {
    const element = panel.current
    if (!element) return
    const update = (event: Event) => {
      const media = event.target as HTMLMediaElement
      if (media.loop && media.muted) return
      setMediaPlaying(event.type === 'play')
    }
    element.addEventListener('play', update, true)
    element.addEventListener('pause', update, true)
    element.addEventListener('ended', update, true)
    return () => {
      element.removeEventListener('play', update, true)
      element.removeEventListener('pause', update, true)
      element.removeEventListener('ended', update, true)
    }
  }, [])

  const choose = (index: number, focus = false) => {
    setSelected(index)
    setRotating(false)
    setMediaPlaying(false)
    if (focus) tabs.current[index]?.focus()
  }

  const onKeyDown = (event: React.KeyboardEvent) => {
    const moves: Record<string, number> = { ArrowRight: selected + 1, ArrowDown: selected + 1, ArrowLeft: selected - 1, ArrowUp: selected - 1, Home: 0, End: MODES.length - 1 }
    if (!(event.key in moves)) return
    event.preventDefault()
    choose((moves[event.key] + MODES.length) % MODES.length, true)
  }

  const mode = MODES[selected]
  return (
    <section
      className="mode-showcase"
      aria-label="What Magic Studios makes"
      data-rotating={active}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocused(false) }}
    >
      <div className="mode-showcase__tabs" role="tablist" aria-label="Modes">
        {MODES.map((item, index) => (
          <button
            key={item.id}
            ref={(element) => { tabs.current[index] = element }}
            type="button"
            role="tab"
            id={`mode-tab-${item.id}`}
            aria-selected={index === selected}
            aria-controls="mode-panel"
            tabIndex={index === selected ? 0 : -1}
            className="mode-showcase__tab"
            onClick={() => choose(index)}
            onKeyDown={onKeyDown}
          >
            <strong>{item.title}</strong>
            <span>{item.line}</span>
            {index === selected && <span className="mode-showcase__progress" aria-hidden="true" key={selected} style={{ animationDuration: `${ADVANCE_MS}ms` }} />}
          </button>
        ))}
      </div>
      <div
        ref={panel}
        className="mode-showcase__panel"
        role="tabpanel"
        id="mode-panel"
        aria-labelledby={`mode-tab-${mode.id}`}
        style={panelHeight ? { minHeight: panelHeight } : undefined}
      >
        <ModePanel id={mode.id} />
      </div>
      <button type="button" className="button mode-showcase__toggle" onClick={() => setRotating(!rotating)}>
        {rotating ? 'Stop cycling' : 'Cycle examples'}
      </button>
    </section>
  )
}

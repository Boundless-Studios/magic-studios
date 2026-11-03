import { useEffect, useMemo, useRef, useState } from 'react'
import type { MotionCandidate } from '../api/types'

interface SheetPlayerProps {
  name: string
  imageUrl: string
  candidate: MotionCandidate
  className?: string
}

/** Expand ordered frames + per-frame holds into the literal sequence of frame indexes to display. */
function expandedSequence(candidate: MotionCandidate): number[] {
  const frameCount = candidate.metadata?.frame_count ?? 0
  const orderedFrames = candidate.ordered_frames ?? Array.from({ length: frameCount }, (_, index) => index)
  const holds = candidate.frame_holds
  const sequence: number[] = []
  orderedFrames.forEach((frameIndex, position) => {
    const hold = Math.max(1, holds?.[position] ?? 1)
    for (let step = 0; step < hold; step += 1) sequence.push(frameIndex)
  })
  return sequence.length > 0 ? sequence : [0]
}

/** Renders a candidate sprite sheet as a small filmstrip player: measured metadata plus ordered
 * frames/holds/frameDurationMs/flip drive playback, gated by visibility via IntersectionObserver. */
export function SheetPlayer({ name, imageUrl, candidate, className }: SheetPlayerProps) {
  const frameCount = candidate.metadata?.frame_count ?? 1
  const sequence = useMemo(() => expandedSequence(candidate), [candidate])
  const frameDurationMs = candidate.frame_duration_ms
    ?? (candidate.duration_ms && frameCount ? Math.max(1, Math.round(candidate.duration_ms / frameCount)) : 100)
  const flip = candidate.facing === 'left'
  const [step, setStep] = useState(0)
  const [visible, setVisible] = useState(true)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => { setStep(0) }, [candidate.candidate_id])

  useEffect(() => {
    const node = rootRef.current
    if (!node || typeof IntersectionObserver === 'undefined') { setVisible(true); return }
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { threshold: 0.01 })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!visible || sequence.length <= 1) return
    const timer = window.setInterval(() => setStep((current) => (current + 1) % sequence.length), Math.max(1, frameDurationMs))
    return () => window.clearInterval(timer)
  }, [visible, sequence.length, frameDurationMs])

  const boundedStep = step % sequence.length
  const frameIndex = sequence[boundedStep] ?? 0
  const frameWidth = (candidate.metadata?.width ?? frameCount) / frameCount
  const frameHeight = candidate.metadata?.height ?? 1

  return <div
    ref={rootRef}
    className={className ? `sheet-player ${className}` : 'sheet-player'}
    role="img"
    aria-label={`${name}, frame ${boundedStep + 1} of ${sequence.length}`}
    style={{
      transform: flip ? 'scaleX(-1)' : undefined,
    }}
  >
    <svg
      aria-hidden="true"
      focusable="false"
      viewBox={`0 0 ${frameWidth} ${frameHeight}`}
      preserveAspectRatio="xMidYMid meet"
      style={{ display: 'block', width: '100%', height: '100%', overflow: 'hidden' }}
    >
      {/* Clip in frame coordinates: the outer SVG's letterbox area is still
          inside its element bounds, so overflow:hidden there is insufficient. */}
      <svg x={0} y={0} width={frameWidth} height={frameHeight} overflow="hidden">
        <image href={imageUrl} x={-frameIndex * frameWidth} y={0} width={frameWidth * frameCount} height={frameHeight} />
      </svg>
    </svg>
  </div>
}

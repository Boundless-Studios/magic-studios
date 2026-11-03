import { useEffect, useRef, useState } from 'react'

/**
 * Seconds elapsed since `active` most recently became true, ticking once a second while it stays
 * true; `null` while inactive. Used to turn a submit button into its own progress indicator
 * ("Generating…", then "Generating… 42 s" past ten seconds) instead of a separate status line
 * (BOU-3208 operator feedback).
 */
export function useElapsedSeconds(active: boolean): number | null {
  const startedAtRef = useRef<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  if (active && startedAtRef.current === null) startedAtRef.current = Date.now()
  if (!active && startedAtRef.current !== null) startedAtRef.current = null

  useEffect(() => {
    if (!active) return
    const id = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(id)
  }, [active])

  if (!active || startedAtRef.current === null) return null
  return Math.floor((now - startedAtRef.current) / 1000)
}

/** "Generating…" until ten seconds in, then "Generating… 42 s" -- long enough that a bare
 * "Generating…" would otherwise look stalled. */
export function generatingLabel(elapsedSeconds: number | null): string {
  return elapsedSeconds !== null && elapsedSeconds > 10 ? `Generating… ${elapsedSeconds} s` : 'Generating…'
}

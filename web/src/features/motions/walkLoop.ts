/** End is the matching pose in the next stride, excluded from the exported loop. */
export function sampleLoop(start: number, end: number, count: number): number[] {
  if (start < 0 || end <= start || count < 1 || end - start < count) return []
  return Array.from({ length: count }, (_, index) => start + Math.floor(index * (end - start) / count))
}

function distance(a: number[], b: number[]) {
  return a.reduce((sum, value, index) => sum + Math.abs(value - b[index]), 0) / a.length
}

/** Visual suggestion only: constrain stride length and reject motionless matches. */
export function suggestWalkLoop(poses: number[][], fps: number, minimumFrames = 8): { start: number; end: number } | null {
  if (!Number.isFinite(fps) || fps <= 0) return null
  const minimum = Math.max(minimumFrames, Math.round(fps * 0.65))
  const maximum = Math.round(fps * 2)
  let best: { start: number; end: number } | null = null
  let bestScore = Infinity
  for (let start = 0; start < poses.length; start += 1) {
    for (let end = start + minimum; end < Math.min(poses.length - 1, start + maximum + 1); end += 1) {
      const seam = distance(poses[start], poses[end])
      const motion = distance(poses[start], poses[Math.floor((start + end) / 2)])
      if (motion < 0.015 || motion < seam * 1.5) continue
      // Neighbor agreement discourages joining poses moving in opposite directions.
      const score = seam + 0.5 * distance(poses[start + 1], poses[end + 1])
      if (score < bestScore) { bestScore = score; best = { start, end } }
    }
  }
  return best
}

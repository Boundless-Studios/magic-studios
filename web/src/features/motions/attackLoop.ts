/** The server judges attack completeness. A matching recovery is a seam marker and is
 * omitted from loop playback; a one-shot includes its selected final frame. */
export function sampleAttack(start: number, end: number, count: number, complete: boolean): number[] {
  if (!Number.isInteger(start) || !Number.isInteger(end) || !Number.isInteger(count) || count < 2 || end <= start || end - start + (complete ? 0 : 1) < count) return []
  return Array.from({ length: count }, (_, index) => start + Math.floor(index * (end - start) / (complete ? count : count - 1)))
}

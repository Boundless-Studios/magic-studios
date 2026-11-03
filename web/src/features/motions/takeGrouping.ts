import type { MotionCandidate } from '../../api/types'

/** One take of a motion: the primary (original) candidate, plus its keyed alpha sibling when
 * `derive_alpha` produced one right after it (T18/T19). `index` is 1-based, in generation order,
 * counting grouped pairs as a single take. */
export interface TakeGroup {
  index: number
  primary: MotionCandidate
  keyed: MotionCandidate | null
}

/** True when `candidate` is the keyed-alpha sibling immediately derived from `previous`: same
 * clip length, `previous` still carries the chroma backdrop, `candidate` has been keyed, and the
 * recipe records a `derive_alpha` op. The API does not expose the derivation's source candidate
 * id (only the chroma key color), so this positional/duration heuristic is the documented
 * fallback (studio-ux-flow.md section 4) -- it is exact for every path that actually produces a
 * keyed sibling today (`deriveAlpha`, auto-derive-on-settle), which always appends it directly
 * after its source with an unchanged duration. */
function isKeyedSiblingOf(candidate: MotionCandidate, previous: MotionCandidate | undefined): boolean {
  if (!previous) return false
  if (candidate.recipe?.op !== 'derive_alpha') return false
  if (candidate.metadata?.has_alpha !== true) return false
  if (previous.metadata?.has_alpha !== false) return false
  return candidate.duration_ms !== null && candidate.duration_ms === previous.duration_ms
}

/** Groups a motion's candidates into takes for the gallery: a clip and its keyed alpha sibling
 * collapse into one card (Original/Keyed toggle); everything else is its own take. */
export function groupCandidatesIntoTakes(candidates: MotionCandidate[]): TakeGroup[] {
  const groups: TakeGroup[] = []
  for (let i = 0; i < candidates.length; i += 1) {
    const candidate = candidates[i]
    if (isKeyedSiblingOf(candidate, candidates[i - 1])) {
      const last = groups.at(-1)
      if (last && last.primary === candidates[i - 1]) { last.keyed = candidate; continue }
    }
    groups.push({ index: groups.length + 1, primary: candidate, keyed: null })
  }
  return groups
}

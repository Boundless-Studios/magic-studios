import type { CharacterView, Motion, MotionCandidate, SubjectRevision } from '../../api/types'
import { isCandidateStale } from '../motions/motionReadiness'

/** One tile of the Release page's set strip (studio-ux-flow.md section 5): the selected take of
 * a motion, or `null` when the motion has no selected take yet. */
export interface SetMotionItem {
  motionId: string
  name: string
  candidate: MotionCandidate | null
  stale: boolean
}

/** Keep duplicate action names identifiable throughout release review. */
export function setMotionLabel(motion: Motion, views: CharacterView[] = []): string {
  if (!motion.view_id) return motion.name
  const view = views.find((item) => item.view_id === motion.view_id)
  const label = view && view.name !== view.view_id ? `${view.name} (${view.view_id})` : motion.view_id
  return `${label} · ${motion.name}`
}

/** Builds the set strip's tiles from the project's motions, tagging a tile Stale when its
 * selected candidate was made from a subject take other than the one currently chosen
 * (`isCandidateStale`, shared with the Motions page). */
export function buildSetItems(motions: Motion[], currentSubjectRevision: SubjectRevision | null | undefined, views: CharacterView[] = []): SetMotionItem[] {
  return motions.map((motion) => {
    const candidate = motion.selected_candidate_id
      ? motion.candidates.find((item) => item.candidate_id === motion.selected_candidate_id) ?? null
      : null
    return {
      motionId: motion.motion_id,
      name: setMotionLabel(motion, views),
      candidate,
      stale: candidate ? isCandidateStale(candidate, currentSubjectRevision, motion) : false,
    }
  })
}

/** The set strip's one-line summary: "4 of 6 motions selected · 1 stale · 2 findings". Stale and
 * findings clauses are omitted when zero, so a clean set reads as a single short clause. */
export function setSummaryLine(items: SetMotionItem[], findingsCount: number): string {
  const selected = items.filter((item) => item.candidate).length
  const stale = items.filter((item) => item.stale).length
  const parts = [`${selected} of ${items.length} motions selected`]
  if (stale) parts.push(`${stale} stale`)
  if (findingsCount) parts.push(`${findingsCount} finding${findingsCount === 1 ? '' : 's'}`)
  return parts.join(' · ')
}

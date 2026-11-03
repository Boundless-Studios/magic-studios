import type { Motion, Operation, SubjectRevision } from '../../api/types'
import { isCandidateStale } from './motionReadiness'
import { operationForMotion } from './operationSelection'

export type ChipTone = 'neutral' | 'accent' | 'success' | 'warn' | 'danger' | 'grey'

export interface MotionChipInfo {
  label: string
  tone: ChipTone
  /** Present only for an in-flight generation with a known percentage (drives the row's
   * bottom-edge progress bar); indeterminate progress renders the bar without a width. */
  progressPercent: number | null
  /** In-flight rows spin: the pill is the operator's only per-motion progress signal. */
  spinner: boolean
}

/**
 * The motion rail's state-chip vocabulary: "To generate" is neutral, in-flight work ("Queued",
 * "Generating…" / "Generating NN%", "Finishing…") is accent-tinted and spins, "Review" and
 * "Needs attention" are amber, "Selected" is green, "Failed" is red, and "Cancelled" is grey.
 */
export function motionChipInfo(motion: Motion, operations: Operation[], currentSubjectRevision?: SubjectRevision | null): MotionChipInfo {
  const operation = operationForMotion(motion, operations)
  if (operation?.status === 'queued') return { label: 'Queued', tone: 'accent', progressPercent: null, spinner: true }
  if (operation?.status === 'processing') {
    const progress = operation.progress_percent
    return { label: progress !== null ? `Generating ${progress}%` : 'Generating…', tone: 'accent', progressPercent: progress, spinner: true }
  }
  if (operation?.status === 'failed' && !motion.candidates.length) return { label: 'Failed', tone: 'danger', progressPercent: null, spinner: false }
  if (operation?.status === 'cancelled' && !motion.candidates.length) return { label: 'Cancelled', tone: 'grey', progressPercent: null, spinner: false }
  const selected = motion.candidates.find((candidate) => candidate.candidate_id === motion.selected_candidate_id)
  if (selected) {
    if (isCandidateStale(selected, currentSubjectRevision, motion)) return { label: 'Stale', tone: 'grey', progressPercent: null, spinner: false }
    return { label: 'Selected', tone: 'success', progressPercent: null, spinner: false }
  }
  if (motion.candidates.length) {
    if (motion.candidates.every((candidate) => isCandidateStale(candidate, currentSubjectRevision, motion))) return { label: 'Stale', tone: 'grey', progressPercent: null, spinner: false }
    return { label: 'Review', tone: 'warn', progressPercent: null, spinner: false }
  }
  // A completed job whose result has not folded into the manifest yet: still finishing,
  // unless the ledger says settlement is blocked and the operator has to act.
  if (operation?.status === 'completed' && operation.stage !== 'settled') {
    if (operation.settlement_blocked) return { label: 'Needs attention', tone: 'warn', progressPercent: null, spinner: false }
    return { label: 'Finishing…', tone: 'accent', progressPercent: null, spinner: true }
  }
  return { label: 'To generate', tone: 'neutral', progressPercent: null, spinner: false }
}

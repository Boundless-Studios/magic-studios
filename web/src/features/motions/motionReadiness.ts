import type { Motion, MotionCandidate, Operation, SubjectRevision } from '../../api/types'
import { isOperationActive, operationForMotion } from './operationSelection'

/** A candidate is stale once the chosen subject has moved on from the revision it was made
 * from. A candidate with no recorded `subject_revision_id` predates lineage tracking and is
 * never treated as stale (studio-ux-flow.md section 7). */
export function isCandidateStale(candidate: MotionCandidate, currentSubjectRevision: SubjectRevision | null | undefined, motion?: Motion): boolean {
  if (motion?.view_id && motion.spec_digest && candidate.motion_spec_digest !== motion.spec_digest) return true
  if (!candidate.subject_revision_id) return false
  if (!currentSubjectRevision) return false
  return candidate.subject_revision_id !== currentSubjectRevision.revision_id
}

/** The rail checklist's default inclusion: a motion is pre-checked for the next batch when it
 * has no selected take yet and nothing is already in flight for it. */
export function defaultChecked(motion: Motion, operations: Operation[]): boolean {
  return !motion.selected_candidate_id && !isMotionGenerating(motion, operations)
}

/** True while a generation for this motion can still change (queued, running, or unsettled). */
export function isMotionGenerating(motion: Motion, operations: Operation[]): boolean {
  return operations.some((operation) => operation.motion_id === motion.motion_id && isOperationActive(operation))
}

/** The rail's batch progress line while anything is in flight: "Generating 6 motions · 2 done".
 * "Done" approximates settled work as any motion that already has a take. */
export function generationSummary(motions: Motion[], operations: Operation[]): string | null {
  const inFlight = motions.filter((motion) => isMotionGenerating(motion, operations)).length
  if (!inFlight) return null
  const done = motions.filter((motion) => !isMotionGenerating(motion, operations) && motion.candidates.length > 0).length
  const total = inFlight + done
  const line = `Generating ${total} motion${total === 1 ? '' : 's'}`
  return done ? `${line} · ${done} done` : line
}

/** How many of the ticked motions have a job still in flight. */
export function inFlightCheckedCount(motions: Motion[], operations: Operation[], checkedIds: Set<string>): number {
  return motions.filter((motion) => checkedIds.has(motion.motion_id) && isMotionGenerating(motion, operations)).length
}

/** The batch button's label, honest about what the click will actually do: the in-flight count
 * while a batch runs (not the ticked count), "Regenerate" once every ticked motion already has
 * a take, and "Generate" otherwise. */
export function batchButtonLabel(motions: Motion[], operations: Operation[], checkedIds: Set<string>): string {
  const inFlight = inFlightCheckedCount(motions, operations, checkedIds)
  if (inFlight) return `Generating ${inFlight}…`
  const checked = motions.filter((motion) => checkedIds.has(motion.motion_id))
  const verb = checked.length && checked.every((motion) => motion.candidates.length > 0) ? 'Regenerate' : 'Generate'
  return `${verb} ${checked.length} motion${checked.length === 1 ? '' : 's'}`
}

/** Motions dispatched in the last batch that have since produced a new take. Their tick has done
 * its job, so the workspace drops it; a motion that failed produced nothing and stays ticked.
 * `dispatched` maps motion_id to its take count at dispatch time, so a regeneration only counts
 * as settled once a *new* take lands. */
export function settledSinceDispatch(motions: Motion[], operations: Operation[], dispatched: Record<string, number>): string[] {
  return motions
    .filter((motion) => motion.motion_id in dispatched
      && !isMotionGenerating(motion, operations)
      && motion.candidates.length > dispatched[motion.motion_id])
    .map((motion) => motion.motion_id)
}

/** True when the motion's takes were all rendered from a spec that has since been edited -- the
 * "what happens" line changed under them. Advisory only: it never blocks a generation. */
export function hasTakesFromOlderSpec(motion: Motion): boolean {
  if (!motion.spec_digest || !motion.candidates.length) return false
  return motion.candidates.every((candidate) => Boolean(candidate.motion_spec_digest) && candidate.motion_spec_digest !== motion.spec_digest)
}

/** The rail's one-line readiness summary: "4 of 6 selected · 1 to review". */
export function readinessSummary(motions: Motion[], operations: Operation[], currentSubjectRevision?: SubjectRevision | null): string {
  if (!motions.length) return ''
  const selectedCandidate = (motion: Motion) => motion.candidates.find((candidate) => candidate.candidate_id === motion.selected_candidate_id)
  const isCurrent = (motion: Motion, candidate: MotionCandidate) => !isCandidateStale(candidate, currentSubjectRevision, motion)
  const selected = motions.filter((motion) => {
    const candidate = selectedCandidate(motion)
    return candidate && isCurrent(motion, candidate)
  }).length
  const stale = motions.filter((motion) => {
    const candidate = selectedCandidate(motion)
    return candidate ? !isCurrent(motion, candidate) : motion.candidates.length > 0 && motion.candidates.every((take) => !isCurrent(motion, take))
  }).length
  const toReview = motions.filter((motion) => {
    const candidate = selectedCandidate(motion)
    return (!candidate || !isCurrent(motion, candidate)) && motion.candidates.some((take) => isCurrent(motion, take)) && !operationForMotion(motion, operations)
  }).length
  const parts = [`${selected} of ${motions.length} selected`]
  if (stale) parts.push(`${stale} stale`)
  if (toReview) parts.push(`${toReview} to review`)
  return parts.join(' · ')
}

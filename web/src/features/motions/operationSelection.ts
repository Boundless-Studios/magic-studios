import type { Motion, Operation } from '../../api/types'

/**
 * An operation counts as active work on a motion only while it can still change: still
 * queued/processing, or completed but not yet folded into the manifest (`stage !== 'settled'`).
 * A completed-and-settled job is history -- it must not keep a motion looking "in flight" (the
 * rail chip, the Generate button's disabled state, and the readiness line all depend on this).
 */
export function isOperationActive(operation: Operation): boolean {
  if (operation.status === 'queued' || operation.status === 'processing') return true
  return operation.status === 'completed' && operation.stage !== 'settled'
}

/** A motion needs generating when nothing has settled for it yet and nothing is in flight. */
export function needsGeneration(motion: Motion, operations: Operation[]): boolean {
  return (
    motion.candidates.length === 0 &&
    !operations.some((operation) => operation.motion_id === motion.motion_id && isOperationActive(operation))
  )
}

/** Live integration work wins over history; an integrated candidate wins over old history. */
export function operationForMotion(motion: Motion, operations: Operation[]): Operation | undefined {
  const matching = operations.filter((operation) => operation.motion_id === motion.motion_id)
  const integrationRelevant = matching.filter(isOperationActive)
  if (integrationRelevant.length) return integrationRelevant.at(-1)
  if (motion.candidates.length) return undefined
  return matching.at(-1)
}

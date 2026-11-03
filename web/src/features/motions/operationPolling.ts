import type { Operation } from '../../api/types'

/** A completed job that has not yet been folded into the manifest (candidate/subject revision). */
export function needsSettlement(operation: Operation): boolean {
  return operation.status === 'completed' && operation.stage === 'awaiting_settlement'
}

/** An operation that can still change: still running, or completed but not yet folded
 * into the manifest. A pool-executed job's status itself only ever advances inside
 * reconcile (BOU-3208), so "in flight" must include queued/processing too, not just
 * completed-but-unsettled -- otherwise a pool job sits at queued/processing forever
 * because the plain listing never asks the server to check the pool. */
function isInFlight(operation: Operation): boolean {
  return operation.status === 'queued' || operation.status === 'processing' || needsSettlement(operation)
}

/** 1.5 s: fast enough that the local backend's one-second jobs still show their in-flight state;
 * the stabilised `select` in queries.ts is what keeps each poll cheap for the page. */
export const IN_FLIGHT_POLL_MS = 1500

/**
 * Poll only while something can still change: a job in flight, or a finished job that has not been
 * settled. A settled completed job is history and must not keep the page polling.
 */
export function operationRefetchInterval(operations: Operation[] | undefined): typeof IN_FLIGHT_POLL_MS | false {
  return operations?.some(isInFlight) ? IN_FLIGHT_POLL_MS : false
}

export interface OperationsApi {
  operations: (projectId: string) => Promise<Operation[]>
  reconcileActivity: (projectId: string) => Promise<Operation[]>
}

/**
 * While anything is in flight (queued, processing, or completed-but-unsettled), reconciles --
 * which syncs pool status and settles completed jobs on the server -- and returns its operations,
 * so a pool-executed job (whose status only ever advances inside reconcile) is never stuck. Once
 * nothing is in flight, this is just the plain operations listing.
 */
export async function pollProjectOperations(
  projectId: string,
  api: OperationsApi,
  onSettled?: () => void,
): Promise<Operation[]> {
  const operations = await api.operations(projectId)
  if (!operations.some(isInFlight)) return operations
  const reconciled = await api.reconcileActivity(projectId)
  onSettled?.()
  return reconciled
}

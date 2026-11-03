/** Shaping for the header's GPU status control: pool telemetry into strips, and the
 * project's own operations into drawer rows. */
import type { Operation, PoolWorker } from '../../api/types'

export type StripState = 'idle' | 'busy' | 'draining' | 'offline'

export interface GpuStrip {
  key: string
  workerId: string
  label?: string | null
  gpuName: string
  state: StripState
  utilizationPct: number | null
  memoryUsedMb: number | null
  memoryTotalMb: number | null
  busyJobId: string | null
}

/** Keep the full cluster visible, including disconnected workers and missing telemetry. */
export function stripsFromWorkers(workers: PoolWorker[]): GpuStrip[] {
  return workers
    .flatMap((worker) => {
      const state: StripState = worker.status === 'offline' ? 'offline' : worker.status === 'busy' ? 'busy' : worker.status === 'draining' ? 'draining' : 'idle'
      if (!worker.gpus.length) {
        return [{ key: worker.worker_id, workerId: worker.worker_id, label: worker.label, gpuName: 'GPU', state, utilizationPct: null, memoryUsedMb: null, memoryTotalMb: null, busyJobId: worker.busy_job_id }]
      }
      return worker.gpus.map((gpu) => ({
        key: `${worker.worker_id}:${gpu.index}`,
        workerId: worker.worker_id, label: worker.label,
        gpuName: gpu.name,
        state,
        utilizationPct: gpu.utilization_pct,
        memoryUsedMb: gpu.memory_used_mb,
        memoryTotalMb: gpu.memory_total_mb,
        busyJobId: worker.busy_job_id,
      }))
    })
}

export function memoryLabel(usedMb: number | null, totalMb: number | null): string | null {
  if (usedMb === null || totalMb === null) return null
  const gb = (value: number) => (value / 1024).toFixed(1)
  return `${gb(usedMb)} / ${gb(totalMb)} GB`
}

export function elapsedLabel(since: string | null | undefined, now: number): string | null {
  if (!since) return null
  const started = Date.parse(since)
  if (Number.isNaN(started)) return null
  const seconds = Math.max(0, Math.round((now - started) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

export interface ActivityRow {
  key: string
  operation: Operation
  /** Every attempt in a collapsed failure run, newest first; a single-attempt row has one. */
  attempts: Operation[]
}

const timestampOf = (operation: Operation) => operation.updated_at ?? operation.created_at ?? null

/** Consecutive failures of the same motion collapse into one row, so a motion that failed
 * five times reads as one problem rather than five identical cards. */
export function groupActivityRows(newestFirst: Operation[]): ActivityRow[] {
  const rows: ActivityRow[] = []
  for (const operation of newestFirst) {
    const previous = rows.at(-1)
    const collapsible = operation.status === 'failed' && operation.motion_id !== null
    if (
      previous &&
      collapsible &&
      previous.operation.status === 'failed' &&
      previous.operation.motion_id === operation.motion_id
    ) {
      previous.attempts.push(operation)
      continue
    }
    rows.push({ key: operation.job_id, operation, attempts: [operation] })
  }
  return rows
}

export function attemptRangeLabel(attempts: Operation[]): string | null {
  const stamps = attempts.map(timestampOf).filter((stamp): stamp is string => stamp !== null).sort()
  const first = stamps[0]
  const last = stamps.at(-1)
  if (!first || !last) return null
  const time = (value: string) => new Date(value).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
  return first === last ? time(first) : `${time(first)} – ${time(last)}`
}

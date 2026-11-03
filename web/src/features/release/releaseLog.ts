import type { ExportRecord, ReleaseDelivery } from '../../api/types'

/**
 * The Release page's per-release log (studio-ux-flow.md section 5): "created 8:52 · exported ·
 * delivered to Gaia". A delivery already carries its own server timestamp (`delivered_at`), but
 * neither `Release` nor `ExportRecord` carries a `created_at` from the backend yet (they are
 * synchronous, immutable records with no timeline of their own) -- so "created" and "exported"
 * are recorded locally, the moment this session performs them, a session-local record for the
 * same reason. A reload starts a release's local log over; the delivered-at half of the
 * timeline always survives one, straight from the server.
 */

export type ReleaseLogEventType = 'created' | 'exported' | 'delivered'

export interface ReleaseLogEvent {
  type: ReleaseLogEventType
  at: string
  label: string
}

interface ExportEntry {
  record: ExportRecord
  at: string
}

interface ReleaseRecord {
  createdAt: string | null
  exports: ExportEntry[]
}

const records = new Map<string, ReleaseRecord>()

function recordFor(releaseId: string): ReleaseRecord {
  let record = records.get(releaseId)
  if (!record) {
    record = { createdAt: null, exports: [] }
    records.set(releaseId, record)
  }
  return record
}

export function recordReleaseCreated(releaseId: string, at: string = new Date().toISOString()): void {
  recordFor(releaseId).createdAt = at
}

export function recordExportCreated(releaseId: string, record: ExportRecord, at: string = new Date().toISOString()): void {
  const entry = recordFor(releaseId)
  if (entry.exports.some((item) => item.record.export_id === record.export_id)) return
  entry.exports.push({ record, at })
}

export function localExportsFor(releaseId: string): ExportRecord[] {
  return (records.get(releaseId)?.exports ?? []).map((entry) => entry.record)
}

export function resetReleaseLog(): void {
  records.clear()
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** A target's display label, resolved by the caller (which has the `/studio/targets` data);
 * this module only knows the target's system id. */
export function releaseLogEvents(releaseId: string, deliveries: readonly ReleaseDelivery[], targetLabel: (system: string) => string): ReleaseLogEvent[] {
  const record = records.get(releaseId)
  const events: ReleaseLogEvent[] = []
  if (record?.createdAt) events.push({ type: 'created', at: record.createdAt, label: 'created' })
  for (const entry of record?.exports ?? []) events.push({ type: 'exported', at: entry.at, label: 'exported' })
  for (const delivery of deliveries) {
    if (delivery.status === 'delivered' && delivery.delivered_at) {
      events.push({ type: 'delivered', at: delivery.delivered_at, label: `delivered to ${targetLabel(delivery.target)}` })
    }
  }
  return events.sort((a, b) => a.at.localeCompare(b.at))
}

/** The collapsed one-line summary: the first event carries a time, the rest read as plain
 * labels ("created 8:52 · exported · delivered to Gaia"). */
export function releaseLogSummary(events: readonly ReleaseLogEvent[]): string {
  if (!events.length) return 'No activity recorded yet'
  return events.map((event, index) => (index === 0 ? `${event.label} ${formatTime(event.at)}` : event.label)).join(' · ')
}

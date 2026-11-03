import { useMutation, useQueryClient } from '@tanstack/react-query'
import { forwardRef, useState } from 'react'
import { studioApi } from '../../api/client'
import type { ExportRecord, Release, ReleaseDelivery, TargetInfo } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { saveFromUrl } from '../../lib/saveFile'
import { localExportsFor, recordExportCreated, releaseLogEvents, releaseLogSummary } from './releaseLog'

export interface ExportProfileOption { id: string; label: string; description: string }

function DeliveryRow({ target, delivery, onDeliver, pending }: { target: TargetInfo; delivery: ReleaseDelivery | undefined; onDeliver: () => void; pending: boolean }) {
  if (!delivery) return <Button disabled={pending} onClick={onDeliver}>{pending ? 'Delivering…' : `Deliver to ${target.display_name}`}</Button>
  if (delivery.status === 'delivered') return <div className="notice" role="status"><p>Delivered to {target.display_name}: {delivery.summary}</p></div>
  if (delivery.status === 'failed') return <p role="alert">Delivery to {target.display_name} failed: {delivery.error ?? 'Unknown error'} <Button disabled={pending} onClick={onDeliver} variant="secondary">{pending ? 'Retrying…' : 'Retry'}</Button></p>
  return <p className="notice" role="status">Delivering to {target.display_name}…</p>
}

/** The signed URL is fetched on click, not on mount: a five-minute grant fetched when the card
 * rendered is routinely expired by the time anyone presses it. */
function ExportRow({ record, label }: { record: ExportRecord; label: string }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function download() {
    setBusy(true)
    setError(null)
    try { saveFromUrl((await studioApi.exportDownload(record.export_id)).upload_url) }
    catch (failure) { setError(failure instanceof Error ? failure.message : 'The download could not be prepared.') }
    finally { setBusy(false) }
  }
  return <div className="release-record" key={record.export_id}>
    <strong>{label}</strong>
    <small>{Math.ceil(record.size_bytes / 1024)} KB</small>
    <Button disabled={busy} onClick={download}>{busy ? 'Preparing…' : `Download ${label} package`}</Button>
    {error && <p className="notice" role="alert">{error}</p>}
  </div>
}

/**
 * One release in the Release page's list (studio-ux-flow.md section 5): its exports, one
 * "Deliver to {target}" per configured target, a Retry on a failed delivery, and a collapsed
 * release log line. `forwardRef` lets the header's "Export" deep link scroll the newest
 * release's export controls into view.
 */
export const ReleaseCard = forwardRef<HTMLElement, {
  projectId: string
  release: Release
  targets: TargetInfo[] | undefined
  profiles: ExportProfileOption[]
  defaultProfile: string
}>(function ReleaseCard({ projectId, release, targets, profiles, defaultProfile }, ref) {
  const client = useQueryClient()
  const [profile, setProfile] = useState(defaultProfile)
  const [logExpanded, setLogExpanded] = useState(false)
  const [deliveringTarget, setDeliveringTarget] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The releases list only ever refetches on the project's own poll cadence, so a delivery this
  // session just performed is held here until that catches up -- the same reason `releaseLog`
  // keeps "created"/"exported" locally.
  const [localDeliveries, setLocalDeliveries] = useState<ReleaseDelivery[]>([])

  async function refreshReleases() { await client.invalidateQueries({ queryKey: ['releases', projectId] }) }

  const exportMutation = useMutation({
    mutationFn: () => studioApi.createExport(release.release_id, release.source_revision, profile),
    onSuccess: async (record) => { recordExportCreated(release.release_id, record); setError(null); await refreshReleases() },
    onError: (mutationError) => setError(mutationError instanceof Error ? mutationError.message : 'The export could not be completed.'),
  })
  const deliverMutation = useMutation({
    mutationFn: (target: string) => studioApi.retryDelivery(release.release_id, target),
    onMutate: (target: string) => setDeliveringTarget(target),
    onSuccess: async (delivery) => { setError(null); setLocalDeliveries((current) => [...current.filter((item) => item.target !== delivery.target), delivery]); await refreshReleases() },
    onError: (mutationError) => setError(mutationError instanceof Error ? mutationError.message : 'That delivery could not be completed.'),
    onSettled: () => setDeliveringTarget(null),
  })

  const deliveries = [...release.deliveries.filter((item) => !localDeliveries.some((local) => local.target === item.target)), ...localDeliveries]
  const exports = localExportsFor(release.release_id)
  const configuredTargets = (targets ?? []).filter((target) => target.delivery_configured)
  const events = releaseLogEvents(release.release_id, deliveries, (system) => targets?.find((target) => target.system === system)?.display_name ?? system)

  return <section aria-label={release.name} className="release-card" ref={ref}>
    <strong>{release.name}</strong>
    <small>revision {release.source_revision}</small>
    {error && <p className="notice" role="status">{error}</p>}

    <div className="release-record">
      <label>Export profile<select aria-label="Export profile" onChange={(event) => setProfile(event.target.value)} value={profile}>
        {profiles.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
      </select></label>
      <p>{profiles.find((option) => option.id === profile)?.description}</p>
      <Button onClick={() => exportMutation.mutate()} variant="primary">{exportMutation.isPending ? 'Packaging…' : 'Request export'}</Button>
      {exports.map((record) => <ExportRowWithDownload key={record.export_id} profiles={profiles} record={record} />)}
    </div>

    {configuredTargets.map((target) => {
      const delivery = deliveries.find((item) => item.target === target.system)
      return <DeliveryRow delivery={delivery} key={target.system} onDeliver={() => deliverMutation.mutate(target.system)} pending={deliveringTarget === target.system} target={target} />
    })}

    <button aria-expanded={logExpanded} className="motion-log__summary" onClick={() => setLogExpanded((value) => !value)} type="button">{releaseLogSummary(events)}</button>
    {logExpanded && <ol className="motion-log__list">
      {events.map((event, index) => <li className="motion-log__entry" key={index}><div><strong>{event.label}</strong><time dateTime={event.at}>{new Date(event.at).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</time></div></li>)}
    </ol>}
  </section>
})

function ExportRowWithDownload({ record, profiles }: { record: ExportRecord; profiles: ExportProfileOption[] }) {
  const label = profiles.find((option) => option.id === record.profile_name)?.label ?? record.profile_name
  return <ExportRow label={label} record={record} />
}

import { afterEach, expect, it } from 'vitest'
import type { ExportRecord, ReleaseDelivery } from '../../api/types'
import { localExportsFor, recordExportCreated, recordReleaseCreated, releaseLogEvents, releaseLogSummary, resetReleaseLog } from './releaseLog'

afterEach(() => resetReleaseLog())

const exportRecord: ExportRecord = { export_id: 'export-1', release_id: 'release-1', profile_name: 'png-json', profile_version: '1', artifact_key: 'e.zip', sha256: 'e', size_bytes: 1024 }
const targetLabel = (system: string) => (system === 'gaia' ? 'Gaia' : system)

it('orders created, exported, and a server-timestamped delivery into one timeline', () => {
  recordReleaseCreated('release-1', '2026-01-01T08:52:00Z')
  recordExportCreated('release-1', exportRecord, '2026-01-01T08:55:00Z')
  const delivered: ReleaseDelivery = { release_id: 'release-1', target: 'gaia', status: 'delivered', delivered_at: '2026-01-01T09:00:00Z', summary: 'ok', details: {}, error: null, retryable: false }
  const events = releaseLogEvents('release-1', [delivered], targetLabel)
  expect(events.map((event) => event.label)).toEqual(['created', 'exported', 'delivered to Gaia'])
})

it('omits a failed or pending delivery from the timeline', () => {
  recordReleaseCreated('release-1')
  const failed: ReleaseDelivery = { release_id: 'release-1', target: 'gaia', status: 'failed', delivered_at: null, summary: '', details: {}, error: 'nope', retryable: true }
  expect(releaseLogEvents('release-1', [failed], targetLabel)).toHaveLength(1)
})

it('summarizes with a time on the first event only', () => {
  recordReleaseCreated('release-1', '2026-01-01T08:52:00Z')
  recordExportCreated('release-1', exportRecord, '2026-01-01T08:55:00Z')
  const summary = releaseLogSummary(releaseLogEvents('release-1', [], targetLabel))
  expect(summary).toMatch(/^created \d{1,2}:\d{2}.*(AM|PM)? · exported$/)
})

it('reports no activity for a release with an empty log', () => {
  expect(releaseLogSummary(releaseLogEvents('release-1', [], targetLabel))).toBe('No activity recorded yet')
})

it('deduplicates a repeated export id', () => {
  recordExportCreated('release-1', exportRecord)
  recordExportCreated('release-1', exportRecord)
  expect(localExportsFor('release-1')).toHaveLength(1)
})

import { useQuery } from '@tanstack/react-query'
import { memo, useCallback, useRef, useState } from 'react'
import { studioApi } from '../../api/client'
import type { MotionGenerationSettings, Operation } from '../../api/types'
import { entryTimestamp, shortTime } from './logFormatting'

/** One shared empty array so an empty ledger does not re-render the memoised list. */
const EMPTY_OPERATIONS: Operation[] = []

function settingsSummary(settings: MotionGenerationSettings | null | undefined): string | null {
  if (!settings) return null
  return `${settings.preset} · ${settings.steps} steps · seed ${settings.seed} · ${settings.width}×${settings.height} · ${settings.duration_seconds}s`
}

function outcomeLabel(operation: Operation): string {
  if (operation.status === 'failed') return 'Failed'
  if (operation.status === 'cancelled') return 'Cancelled'
  if (operation.stage === 'awaiting_settlement') return 'Ready'
  if (operation.status === 'queued') return 'Queued'
  if (operation.status === 'processing') return 'Running'
  return operation.status === 'completed' ? 'Complete' : 'Cancelled'
}

/** The entry list is the expensive half of the log: up to 20 rows, each formatting a date. It only
 * changes when the ledger refetches, so it is memoised away from the workspace's own re-renders. */
const MotionLogEntries = memo(function MotionLogEntries({ motionId, operations, onRegenerateWithSettings }: {
  motionId: string
  operations: Operation[]
  onRegenerateWithSettings: (settings: MotionGenerationSettings) => void
}) {
  return <ol className="motion-log__list" aria-label={`${motionId} activity`}>
    {operations.map((operation) => <li key={operation.job_id} className={operation.status === 'failed' ? 'motion-log__entry motion-log__entry--failed' : 'motion-log__entry'}>
      <div><strong>{outcomeLabel(operation)}</strong>{operation.updated_at && <time dateTime={operation.updated_at}>{entryTimestamp(operation.updated_at)}</time>}</div>
      {settingsSummary(operation.generation_settings) && <p className="motion-log__settings">{settingsSummary(operation.generation_settings)}</p>}
      {operation.failure_explanation && <p className="motion-log__failure">{operation.failure_explanation}</p>}
      {operation.generation_settings && <button type="button" onClick={() => onRegenerateWithSettings(operation.generation_settings!)}>{operation.generation_settings.provider_parameters?.execution_provider === 'higgsfield' ? 'Regenerate on Higgsfield with these settings (billed to your key)' : 'Regenerate with these settings'}</button>}
    </li>)}
  </ol>
})

/** The motion log (studio-ux-flow.md section 4): collapsed to "N generations · last H:MM"; expanded,
 * every generation attempt for this motion from the durable activity ledger (`activityPage`
 * filtered server-side by `motion_id`), each with a "Regenerate with these settings" seed. The
 * ledger only ever records generation attempts today -- edits/selections are not yet activity
 * entries, so this stays a generation log rather than claiming coverage it doesn't have. */
export function MotionLog({ projectId, motionId, onRegenerateWithSettings }: {
  projectId: string
  motionId: string
  onRegenerateWithSettings: (settings: MotionGenerationSettings) => void
}) {
  // Open by default: the history of a motion is part of the page, not a drawer to discover.
  const [expanded, setExpanded] = useState(true)
  const activity = useQuery({
    queryKey: ['activity', projectId, 'motion-log', motionId],
    queryFn: () => studioApi.activityPage(projectId, { motionId, limit: 20 }),
  })
  const operations = activity.data?.operations ?? EMPTY_OPERATIONS
  const last = operations[0]
  // The workspace hands a fresh closure every render; a ref keeps the memoised list's prop stable.
  const latestRegenerate = useRef(onRegenerateWithSettings)
  latestRegenerate.current = onRegenerateWithSettings
  const regenerate = useCallback((settings: MotionGenerationSettings) => latestRegenerate.current(settings), [])
  const summaryLine = activity.isPending
    ? 'History · loading…'
    : activity.isError
    ? 'History · could not load · click to retry'
    : operations.length
    ? `History · ${operations.length} generation${operations.length === 1 ? '' : 's'}${last?.updated_at || last?.created_at ? ` · last ${shortTime((last.updated_at ?? last.created_at)!)}` : ''}`
    : 'History · no generations yet'
  return <section className="motion-log" aria-label="Motion log">
    <button className="motion-log__summary" type="button" aria-expanded={expanded} onClick={() => { if (activity.isError) void activity.refetch(); else setExpanded((value) => !value) }}>{summaryLine}</button>
    {expanded && <>
      {!operations.length && <p className="motion-log__empty">Nothing generated for this motion yet.</p>}
      <MotionLogEntries motionId={motionId} operations={operations} onRegenerateWithSettings={regenerate} />
    </>}
  </section>
}

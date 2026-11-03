import { useQuery } from '@tanstack/react-query'
import { memo, useCallback, useRef, useState } from 'react'
import { studioApi } from '../../api/client'
import type { Operation } from '../../api/types'
import { entryTimestamp, shortTime } from '../motions/logFormatting'

/** One shared empty array so an empty ledger does not re-render the memoised list. */
const EMPTY_OPERATIONS: Operation[] = []

function outcomeLabel(operation: Operation): string {
  if (operation.status === 'failed') return 'Failed'
  if (operation.status === 'cancelled') return 'Cancelled'
  if (operation.stage === 'awaiting_settlement') return 'Ready'
  if (operation.status === 'queued') return 'Queued'
  if (operation.status === 'processing') return 'Running'
  return operation.status === 'completed' ? 'Complete' : 'Cancelled'
}

/** Up to 20 rows, each formatting a date: memoised so the subject workspace's own re-renders
 * (a keystroke, an operations poll) do not redo the list. */
const SubjectLogEntries = memo(function SubjectLogEntries({ operations, onTryAgain }: { operations: Operation[]; onTryAgain: () => void }) {
  return <ol className="motion-log__list" aria-label="Subject activity">
    {operations.map((operation) => <li key={operation.job_id} className={operation.status === 'failed' ? 'motion-log__entry motion-log__entry--failed' : 'motion-log__entry'}>
      <div><strong>{outcomeLabel(operation)}</strong>{operation.updated_at && <time dateTime={operation.updated_at}>{entryTimestamp(operation.updated_at)}</time>}</div>
      {operation.prompt && <p className="motion-log__settings">{operation.prompt}</p>}
      {operation.failure_explanation && <p className="motion-log__failure">{operation.failure_explanation}</p>}
      <button type="button" onClick={onTryAgain}>Try again with these settings</button>
    </li>)}
  </ol>
})

/**
 * The subject log (studio-ux-flow.md section 3): collapsed to "N generations · last H:MM";
 * expanded, every subject generation attempt from the durable activity ledger (`activityPage`
 * filtered server-side with the `motion_id=subject` sentinel -- section 7). A `generate_subject`
 * job never carries `prompt`/`generation_settings` in the API today (only `GENERATE_MOTION` jobs
 * do), so an entry's direction is shown only when present and omitted otherwise, per spec; "Try
 * again with these settings" re-runs generation with the current references and direction.
 */
export function SubjectLog({ projectId, onTryAgain }: { projectId: string; onTryAgain: () => void }) {
  const [expanded, setExpanded] = useState(false)
  const activity = useQuery({
    queryKey: ['activity', projectId, 'subject-log'],
    queryFn: () => studioApi.activityPage(projectId, { motionId: 'subject', limit: 20 }),
  })
  const operations = activity.data?.operations ?? EMPTY_OPERATIONS
  const last = operations[0]
  // The workspace hands a fresh closure every render; a ref keeps the memoised list's prop stable.
  const latestTryAgain = useRef(onTryAgain)
  latestTryAgain.current = onTryAgain
  const tryAgain = useCallback(() => latestTryAgain.current(), [])
  const summaryLine = operations.length
    ? `${operations.length} generation${operations.length === 1 ? '' : 's'}${last?.updated_at || last?.created_at ? ` · last ${shortTime((last.updated_at ?? last.created_at)!)}` : ''}`
    : 'No generations yet'
  return <section className="motion-log subject-log" aria-label="Subject log">
    <button className="motion-log__summary" type="button" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{summaryLine}</button>
    {expanded && <>
      {!operations.length && <p className="motion-log__empty">Nothing generated for the subject yet.</p>}
      <SubjectLogEntries operations={operations} onTryAgain={tryAgain} />
    </>}
  </section>
}

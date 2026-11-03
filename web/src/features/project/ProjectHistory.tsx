import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { studioApi } from '../../api/client'
import { operationLabel, type MotionGenerationSettings, type Operation } from '../../api/types'

type HistoryTab = 'revisions' | 'operations'

function settingsSummary(settings: MotionGenerationSettings | null | undefined): string | null {
  if (!settings) return null
  return `${settings.preset} · ${settings.steps} steps · seed ${settings.seed} · ${settings.width}×${settings.height} · ${settings.duration_seconds}s`
}

function operationStatusLabel(operation: Operation): string {
  if (operation.status === 'failed') return 'Failed'
  if (operation.status === 'cancelled') return 'Cancelled'
  if (operation.stage === 'awaiting_settlement') return 'Ready'
  if (operation.status === 'queued') return 'Queued'
  if (operation.status === 'processing') return 'Running'
  return operation.status === 'completed' ? 'Complete' : 'Cancelled'
}

export function ProjectHistory({ projectId }: { projectId: string }) {
  const [tab, setTab] = useState<HistoryTab>('revisions')
  const history = useQuery({ queryKey: ['history', projectId], queryFn: () => studioApi.history(projectId) })
  return <section className="workspace" aria-labelledby="history-title">
    <header className="workspace-heading">
      <div><p className="eyebrow">5 · History</p><h2 id="history-title">Every saved change</h2><p>Newest first: manifest revisions, generation failures that need attention, and the full operation ledger.</p></div>
    </header>
    <div role="tablist" aria-label="History views" className="history-tabs">
      <button role="tab" type="button" aria-selected={tab === 'revisions'} onClick={() => setTab('revisions')}>Revisions</button>
      <button role="tab" type="button" aria-selected={tab === 'operations'} onClick={() => setTab('operations')}>Operations</button>
    </div>
    {tab === 'revisions' ? <RevisionsTab history={history.isPending ? undefined : (history.data ?? null)} /> : <OperationsTab projectId={projectId} />}
  </section>
}

function RevisionsTab({ history }: { history: Awaited<ReturnType<typeof studioApi.history>> | null | undefined }) {
  if (history === undefined) return <p role="status">Opening history</p>
  if (history === null) return <p>History unavailable</p>
  const entries = [...history].reverse()
  return entries.length
    ? <ol className="history-list" aria-label="Project history">{entries.map((entry, index) => <li key={`${entry.revision ?? 'op'}-${entry.timestamp ?? index}`} className={entry.failed ? 'history-entry history-entry--failed' : 'history-entry'}>
        <div><strong>{entry.summary}</strong>{entry.failed && <span className="history-entry__status">Failed</span>}</div>
        <div className="history-entry__meta">
          {entry.revision !== null && <span>Revision {entry.revision}</span>}
          {entry.timestamp && <time dateTime={entry.timestamp}>{new Date(entry.timestamp).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' })}</time>}
        </div>
      </li>)}</ol>
    : <p className="history-empty">No history yet.</p>
}

/** The durable, paginated operation ledger (T14): every generation attempt with its
 * settled settings and prompt, newest first, "Load more" walking the cursor back. */
function OperationsTab({ projectId }: { projectId: string }) {
  const navigate = useNavigate()
  const [operations, setOperations] = useState<Operation[] | null>(null)
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)

  useEffect(() => {
    let active = true
    setOperations(null)
    studioApi.activityPage(projectId).then((page) => {
      if (!active) return
      setOperations(page.operations)
      setNextCursor(page.next_cursor)
    })
    return () => { active = false }
  }, [projectId])

  async function loadMore() {
    if (!nextCursor) return
    setLoadingMore(true)
    const page = await studioApi.activityPage(projectId, { cursor: nextCursor })
    setOperations((current) => [...(current ?? []), ...page.operations])
    setNextCursor(page.next_cursor)
    setLoadingMore(false)
  }

  function regenerate(operation: Operation) {
    if (!operation.motion_id || !operation.generation_settings) return
    navigate(`/projects/${projectId}/motions`, {
      state: { seedSettings: { motionId: operation.motion_id, settings: operation.generation_settings } },
    })
  }

  if (operations === null) return <p role="status">Opening operations</p>
  if (!operations.length) return <p className="history-empty">No operations yet.</p>
  return <>
    <ol className="operation-list" aria-label="Project operations">{operations.map((operation) => {
      const summary = settingsSummary(operation.generation_settings)
      const label = operationLabel(operation)
      return <li key={operation.job_id} className={operation.status === 'failed' ? 'operation-entry operation-entry--failed' : 'operation-entry'}>
        <div><strong>{label}</strong><span>{operationStatusLabel(operation)}</span></div>
        <p className="operation-stage">{operation.stage.replaceAll('_', ' ')}{operation.progress_percent !== null ? ` · ${operation.progress_percent}%` : ''}</p>
        {summary && <p className="operation-settings">{summary}</p>}
        {operation.failure_explanation && <p className="operation-failure">{operation.failure_explanation}</p>}
        {operation.motion_id && operation.generation_settings && <button type="button" onClick={() => regenerate(operation)}>Regenerate with these settings</button>}
      </li>
    })}</ol>
    {nextCursor && <button type="button" onClick={loadMore} disabled={loadingMore}>Load more</button>}
  </>
}

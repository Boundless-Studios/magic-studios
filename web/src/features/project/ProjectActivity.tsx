import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { studioApi } from '../../api/client'
import { isSubjectOperation, isTurnaroundOperation, operationLabel, type Operation, type PoolQueueResponse, type PoolWorker } from '../../api/types'
import { projectKey, useProject, useProjectOperations } from '../../api/queries'
import { attemptRangeLabel, elapsedLabel, groupActivityRows, memoryLabel, stripsFromWorkers } from './gpuStatus'
import type { GpuStrip } from './gpuStatus'

const live = (operation: Operation) => ['queued', 'processing'].includes(operation.status)

function mergeByJob(ledger: Operation[], fresh: Operation[]): Operation[] {
  const byJob = new Map(ledger.map((operation) => [operation.job_id, operation]))
  for (const operation of fresh) byJob.set(operation.job_id, operation)
  return [...byJob.values()]
}

export function ProjectActivity({ projectId, operations: suppliedOperations, workers: suppliedWorkers, queue: suppliedQueue }: { projectId: string; operations?: Operation[]; workers?: PoolWorker[]; queue?: PoolQueueResponse }) {
  const [open, setOpen] = useState(false)
  const [now, setNow] = useState(() => Date.now())
  const [expanded, setExpanded] = useState<string | null>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLElement>(null)
  const navigate = useNavigate(); const queryClient = useQueryClient(); const project = useProject(projectId)
  // The same ['operations', projectId] poller every workspace shares (BOU-3208): reconciles
  // while anything is in flight, whether or not this panel is open, so a candidate lands in the
  // manifest without the user ever opening it.
  const activity = useProjectOperations(projectId, { enabled: suppliedOperations === undefined })
  const poolInterval = open ? 5000 : 30000
  const workersQuery = useQuery({ queryKey: ['pool-workers'], queryFn: studioApi.poolWorkers, enabled: suppliedWorkers === undefined, refetchInterval: poolInterval, retry: false })
  // The pool-wide queue spans every tenant's work and answers only cluster admins; everyone
  // else counts their own queued operations below.
  const clusterAdmin = workersQuery.data?.cluster_admin === true
  const queueQuery = useQuery({ queryKey: ['pool-queue'], queryFn: studioApi.poolQueue, enabled: suppliedQueue === undefined && clusterAdmin, refetchInterval: poolInterval, retry: false })
  const operations = suppliedOperations ?? activity.data ?? []
  // The operations feed only carries work that is still relevant (in flight or just settled), so
  // once a project is quiet it is empty; the counts come from the durable ledger, with the live
  // feed layered on top because it is the fresher of the two for anything still moving.
  const ledger = useQuery({
    queryKey: ['activity', projectId, 'badge'],
    queryFn: () => studioApi.activityPage(projectId, { limit: 50 }),
    enabled: suppliedOperations === undefined,
    retry: false,
  })
  const counted = mergeByJob(ledger.data?.operations ?? [], operations)
  const workers = suppliedWorkers ?? workersQuery.data?.workers ?? []
  const strips = stripsFromWorkers(workers)
  const queue = suppliedQueue ?? queueQuery.data
  const stale = suppliedWorkers === undefined && (workersQuery.isError || queueQuery.isError)
  const running = counted.filter((item) => item.status === 'processing').length; const failed = counted.filter((item) => item.status === 'failed').length
  const queued = queue?.queued ?? counted.filter((item) => item.status === 'queued').length
  useEffect(() => { if (open) closeRef.current?.focus() }, [open])
  useEffect(() => {
    if (!open) return
    const timer = window.setInterval(() => setNow(Date.now()), 5000)
    const dismiss = (event: KeyboardEvent | MouseEvent) => {
      if (event instanceof KeyboardEvent && event.key === 'Escape') { setOpen(false); buttonRef.current?.focus() }
      else if (event instanceof KeyboardEvent && event.key === 'Tab' && panelRef.current) {
        const focusable = [...panelRef.current.querySelectorAll<HTMLElement>('button, summary, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
          .filter((element) => !element.hasAttribute('disabled'))
        const first = focusable[0]; const last = focusable.at(-1)
        if (!first || !last) return
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
      }
      else if (event instanceof MouseEvent && panelRef.current && !panelRef.current.contains(event.target as Node) && !buttonRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', dismiss); document.addEventListener('mousedown', dismiss)
    return () => { window.clearInterval(timer); document.removeEventListener('keydown', dismiss); document.removeEventListener('mousedown', dismiss) }
  }, [open])
  async function retry(operation: Operation) {
    if (!operation.motion_id || !project.data) { navigate(`/projects/${projectId}/subject`); setOpen(false); return }
    await studioApi.generateMotion(projectId, operation.motion_id, project.data.revision)
    await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }); await queryClient.invalidateQueries({ queryKey: projectKey(projectId) })
  }
  async function cancelOne(operation: Operation) {
    if (!operation.motion_id) return
    await studioApi.cancelMotionOperation(projectId, operation.motion_id, operation.job_id)
    await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }); await queryClient.invalidateQueries({ queryKey: ['operations', projectId] })
  }
  async function cancelAll() {
    await studioApi.cancelAllOperations(projectId)
    await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }); await queryClient.invalidateQueries({ queryKey: ['operations', projectId] })
  }
  function busyLabel(strip: GpuStrip): string {
    if (!strip.busyJobId) return 'Idle'
    const operation = counted.find((item) => item.job_id === strip.busyJobId)
    if (!operation) return 'Busy on another project'
    const name = operationLabel(operation, humanize)
    const elapsed = elapsedLabel(operation.created_at ?? operation.updated_at, now)
    return elapsed ? `${name} · ${elapsed}` : name
  }
  // Rows share the counts' source: the live feed alone is empty once a project is quiet.
  const recent = counted
    // Only completed rows age out (design §6); live and failed work stays until acted on.
    .filter((operation) => { if (operation.status !== 'completed') return true; const at = Date.parse(operation.updated_at ?? operation.created_at ?? ''); return Number.isNaN(at) || now - at < 24 * 60 * 60 * 1000 })
    .sort((a, b) => (b.updated_at ?? b.created_at ?? '').localeCompare(a.updated_at ?? a.created_at ?? ''))
  const rows = groupActivityRows(recent)
  return <div className="project-activity">
    <button ref={buttonRef} className="button gpu-status-trigger" aria-expanded={open} aria-controls="activity-panel" aria-label={`GPU status, ${running} running, ${queued} queued, ${failed} failed`} onClick={() => setOpen(!open)}>
      <span className={`gpu-strips${stale ? ' gpu-strips--stale' : ''}`}>
        {strips.length ? strips.map((strip) => <GpuStripView key={strip.key} strip={strip} />) : <span className="gpu-strip gpu-strip--empty">No GPU online</span>}
        {stale && <span className="gpu-strips__stale">stale</span>}
      </span>
      <span className="gpu-pills">
        <span className="gpu-pill">{running} running</span>
        <span className="gpu-pill">{queued} queued</span>
        {failed > 0 && <span className="gpu-pill gpu-pill--danger">{failed} failed</span>}
      </span>
    </button>
    {open && <section ref={panelRef} id="activity-panel" className="activity-panel" aria-labelledby="activity-title" aria-modal="true" role="dialog">
      <header><h2 id="activity-title">GPU status</h2>{running > 0 && <button className="button" onClick={cancelAll}>Cancel all running</button>}<button className="activity-panel__close" ref={closeRef} aria-label="Close activity" onClick={() => { setOpen(false); buttonRef.current?.focus() }}>×</button></header>
      <ul className={`gpu-strips gpu-strips--drawer${stale ? ' gpu-strips--stale' : ''}`}>
        {strips.length ? strips.map((strip) => <li key={strip.key}><GpuStripView strip={strip} /><span className="gpu-strip__busy">{busyLabel(strip)}</span></li>) : <li><span className="gpu-strip gpu-strip--empty">No GPU online</span></li>}
      </ul>
      {rows.length ? <ol>{rows.map((row) => {
        const operation = row.operation
        const turnaround = isTurnaroundOperation(operation)
        const label = operationLabel(operation, humanize)
        const subject = !turnaround && (isSubjectOperation(operation) || !operation.motion_id)
        const timestamp = operation.updated_at ?? operation.created_at
        const grouped = row.attempts.length > 1
        const range = grouped ? attemptRangeLabel(row.attempts) : null
        return <li key={row.key}><div><strong>{label}</strong><span>{grouped ? `Failed ${row.attempts.length}×` : stateLabel(operation)}</span></div>
          {grouped ? range && <p className="gpu-strip__busy">{range}</p> : timestamp && <time dateTime={timestamp}>Updated {new Date(timestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</time>}
          {operation.progress_percent !== null ? <progress aria-label={`${label} progress`} max={100} value={operation.progress_percent}>{operation.progress_percent}%</progress> : live(operation) ? <div aria-label={`${label} in progress`} className="activity-indeterminate" role="progressbar" /> : null}
          {operation.failure_explanation && <p>{operation.failure_explanation}</p>}
          {operation.status === 'failed' && operation.retryable && !turnaround && <button onClick={() => retry(operation)}>{subject ? 'Open Subject' : `Retry ${label}`}</button>}
          {grouped && <button onClick={() => setExpanded(expanded === row.key ? null : row.key)}>{expanded === row.key ? 'Hide the attempts' : `Show the ${row.attempts.length} attempts`}</button>}
          {grouped && expanded === row.key && <ul className="gpu-attempts">{row.attempts.map((attempt) => <li key={attempt.job_id}>{attempt.job_id}{attempt.updated_at ? ` · ${new Date(attempt.updated_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}` : ''}</li>)}</ul>}
          {operation.status === 'completed' && !turnaround && (subject
            ? <Link to={`/projects/${projectId}/subject`} onClick={() => setOpen(false)}>Go to Subject</Link>
            : <Link to={`/projects/${projectId}/motions`} state={{ selectMotionId: operation.motion_id }} onClick={() => setOpen(false)}>{`Go to ${label}`}</Link>)}
          {!subject && !turnaround && live(operation) && <button onClick={() => cancelOne(operation)}>{`${operation.status === 'processing' ? 'Interrupt' : 'Cancel'} ${label}`}</button>}
        </li>
      })}</ol> : <p>No recent activity.</p>}
      <footer className="activity-panel__footer"><span>Newest first · last 24 h</span><Link to={`/projects/${projectId}/history`} onClick={() => setOpen(false)}>Full activity log</Link></footer>
    </section>}
  </div>
}

export function GpuStripView({ strip }: { strip: GpuStrip }) {
  const memory = memoryLabel(strip.memoryUsedMb, strip.memoryTotalMb)
  return <span className="gpu-strip" title={`${strip.workerId} · ${strip.gpuName} · ${strip.state}`}>
    <span className={`gpu-strip__dot gpu-strip__dot--${strip.state}`} aria-hidden="true" />
    <span className="gpu-strip__id">{strip.label && strip.label !== 'Unnamed machine' ? strip.label : strip.workerId.length > 20 ? strip.workerId.slice(0, 8) : strip.workerId}</span>
    {strip.gpuName !== 'GPU' && <span className="gpu-strip__memory">{strip.gpuName.replace(/^NVIDIA (GeForce )?/, '')}</span>}
    {strip.state === 'offline' ? <span className="gpu-strip__memory">Offline</span> : strip.gpuName === 'GPU' ? <span className="gpu-strip__memory">Connected · GPU not reported</span> : <>
      <span className="gpu-strip__bar"><span className="gpu-strip__fill" style={{ width: `${strip.utilizationPct ?? 0}%` }} /></span>
      <span className="gpu-strip__pct">{strip.utilizationPct === null ? '—' : `${strip.utilizationPct}%`}</span>
      <span className="gpu-strip__memory">{memory ?? 'Memory unavailable'}</span>
    </>}
  </span>
}

function humanize(value: string) { return value.replaceAll(/[-_]/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase()) }
function stateLabel(operation: Operation) { if (operation.status === 'failed') return 'Failed'; if (operation.stage === 'awaiting_settlement') return 'Ready'; if (operation.status === 'queued') return 'Queued'; if (operation.status === 'processing') return 'Running'; return operation.status === 'completed' ? 'Complete' : 'Cancelled' }

import { memo, useCallback, useMemo, useRef, useState } from 'react'
import type { Motion, MotionCandidate, SubjectRevision } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { CandidateDownloadMenu, CandidateMedia } from './candidateMedia'
import { isCandidateStale } from './motionReadiness'
import { groupCandidatesIntoTakes } from './takeGrouping'

function findingsChips(candidate: MotionCandidate): { text: string; blocking: boolean }[] {
  return (candidate.assessment?.findings ?? []).map((finding) => ({ text: finding.message, blocking: finding.severity === 'blocking' }))
}

function sourceTag(candidate: MotionCandidate): string | null {
  if (candidate.source === 'upload') return 'Uploaded'
  if (candidate.ordered_frames !== null) return 'Edited'
  return null
}

const TakeCard = memo(function TakeCard({ motion, take, currentSubjectRevision, onSelect, selecting, onEditFrames, onRegenerateStale, projectId }: {
  motion: Motion
  take: { index: number; primary: MotionCandidate; keyed: MotionCandidate | null }
  currentSubjectRevision: SubjectRevision | null | undefined
  onSelect: (candidateId: string) => void
  selecting: boolean
  onEditFrames: (candidate: MotionCandidate) => void
  onRegenerateStale: () => void
  projectId: string
}) {
  const [showKeyed, setShowKeyed] = useState(false)
  const active = showKeyed && take.keyed ? take.keyed : take.primary
  const stale = isCandidateStale(active, currentSubjectRevision, motion)
  const isSelected = motion.selected_candidate_id === active.candidate_id
  const tag = sourceTag(active)
  const findings = useMemo(() => findingsChips(active), [active])
  return <li className="take-card" aria-label={`Take ${take.index}`}>
    <CandidateMedia projectId={projectId} candidate={active} name={`${motion.name} take ${take.index}`} />
    <div className="take-card__meta">
      <strong>Take {take.index}</strong>
      {active.duration_ms !== null && <span>{(active.duration_ms / 1000).toFixed(1)}s</span>}
      {isSelected && <span className="take-card__badge">Selected</span>}
      {stale && <span className="take-card__badge take-card__badge--stale">Stale</span>}
      {tag && <span className="take-card__tag">{tag}</span>}
    </div>
    {take.keyed && <div className="take-card__toggle" role="group" aria-label={`Take ${take.index} variant`}>
      <button type="button" aria-pressed={!showKeyed} onClick={() => setShowKeyed(false)}>With background</button>
      <button type="button" aria-pressed={showKeyed} onClick={() => setShowKeyed(true)}>Transparent</button>
    </div>}
    {findings.length > 0 && <ul className="take-card__findings" aria-label={`Take ${take.index} findings`}>
      {findings.map((finding, index) => <li key={index} className={finding.blocking ? 'finding-chip finding-chip--blocking' : 'finding-chip finding-chip--advisory'}>{finding.text}</li>)}
    </ul>}
    <div className="take-card__actions">
      {stale
        ? <Button onClick={onRegenerateStale}>{motion.view_id ? 'Regenerate for the current view' : 'Regenerate for the chosen subject'}</Button>
        : <Button title="Use this animation for release and export" onClick={() => onSelect(active.candidate_id)} disabled={isSelected || selecting}>Use animation</Button>}
      <Button aria-label="Edit frames" title="Edit frames" onClick={() => onEditFrames(active)}>✂</Button>
      <CandidateDownloadMenu projectId={projectId} motionId={motion.motion_id} candidateId={active.candidate_id} />
    </div>
  </li>
})

/** The Takes zone (studio-ux-flow.md section 4, "Set-first generation"): a gallery of take
 * cards, a clip grouped with its keyed alpha sibling on one card with an background toggle.
 * Stale takes (made from a subject the project has moved past) cannot be selected and offer a
 * single-motion regenerate instead. */
export function TakesZone({ projectId, motion, currentSubjectRevision, onSelect, selecting, onEditFrames, onRegenerateStale, generating, settlementBlocked, onRetrySettlement, retryingSettlement }: {
  projectId: string
  motion: Motion
  currentSubjectRevision: SubjectRevision | null | undefined
  onSelect: (candidateId: string) => void
  selecting: boolean
  onEditFrames: (candidate: MotionCandidate) => void
  onRegenerateStale: () => void
  /** True while this motion has a job in flight -- the gallery shows a pending take card. */
  generating: boolean
  /** Why a finished render has not become a take yet, straight from the job ledger. */
  settlementBlocked?: string | null
  onRetrySettlement?: () => void
  retryingSettlement?: boolean
}) {
  const takes = useMemo(() => groupCandidatesIntoTakes(motion.candidates), [motion.candidates])
  const [retried, setRetried] = useState(false)
  // The workspace re-creates these three closures on every one of its renders (a keystroke in the
  // Generate zone, a poll), which would defeat `memo` on every take card. Route them through a ref
  // so each card sees one stable identity and only re-renders when its own take changes.
  const handlers = useRef({ onSelect, onEditFrames, onRegenerateStale })
  handlers.current = { onSelect, onEditFrames, onRegenerateStale }
  const selectCandidate = useCallback((candidateId: string) => handlers.current.onSelect(candidateId), [])
  const editFrames = useCallback((candidate: MotionCandidate) => handlers.current.onEditFrames(candidate), [])
  const regenerateStale = useCallback(() => handlers.current.onRegenerateStale(), [])
  const blocked = settlementBlocked ? <div className="motion-zone__blocked" role="status">
    <p>{`This render finished but could not become a take: ${settlementBlocked}`}</p>
    <div className="motion-zone__blocked-actions">
      <Button onClick={() => { setRetried(true); onRetrySettlement?.() }} disabled={retryingSettlement}>Retry</Button>
      {retried && !retryingSettlement && <Button onClick={regenerateStale}>Regenerate</Button>}
    </div>
  </div> : null
  const pending = generating && <li className="take-card take-card--generating" aria-label="Generating take">
    <div className="take-card__preview"><span className="button-spinner" aria-hidden="true" />Generating take…</div>
  </li>
  if (!takes.length) {
    return <section className="motion-zone motion-zone--takes" aria-label="Takes">
      <h4>Takes</h4>
      {blocked}
      {generating ? <ul className="take-grid">{pending}</ul> : !blocked && <p className="motion-zone__empty">No takes yet -- generate to create the first one.</p>}
    </section>
  }
  return <section className="motion-zone motion-zone--takes" aria-label="Takes">
    <h4>Takes</h4>
    {blocked}
    <ul className="take-grid">
      {takes.map((take) => <TakeCard key={take.primary.candidate_id} motion={motion} take={take} currentSubjectRevision={currentSubjectRevision} onSelect={selectCandidate} selecting={selecting} onEditFrames={editFrames} onRegenerateStale={regenerateStale} projectId={projectId} />)}
      {pending}
    </ul>
  </section>
}

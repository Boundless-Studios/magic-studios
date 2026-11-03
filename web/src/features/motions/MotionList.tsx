import type { ChangeEvent, KeyboardEvent } from 'react'
import { useAssetPreview } from '../../api/queries'
import type { Motion, MotionCandidate, Operation, SubjectRevision } from '../../api/types'
import { motionChipInfo } from './motionChip'
import { railThumbFrameStyle } from './motionRailThumb'
import { operationForMotion } from './operationSelection'

/**
 * The motion rail (D3): a `listbox` of rows, set-first (studio-ux-flow.md section 4). Each row
 * carries a checkbox for inclusion in the next batch (clicking it toggles inclusion without
 * changing which motion is open in the main column), a live 40x40 thumbnail, the motion name
 * (clicking it selects the motion for the main column), and a semantic state chip (`motionChip`).
 * ArrowUp/ArrowDown move selection with a roving tabindex; Enter/Space activate the focused row.
 */
export function MotionList({
  projectId, motions, operations, selectedId, onSelect, onRetry, checkedIds, onToggleChecked, currentSubjectRevision,
}: {
  projectId: string
  motions: Motion[]
  operations: Operation[]
  selectedId: string | null
  onSelect: (id: string) => void
  onRetry?: (id: string) => void
  /** Which motions are included in the next "Generate N motions" batch. */
  checkedIds: Set<string>
  onToggleChecked: (id: string) => void
  currentSubjectRevision?: SubjectRevision | null
}) {
  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>, index: number) {
    if (event.key !== 'ArrowUp' && event.key !== 'ArrowDown') return
    event.preventDefault()
    const nextIndex = event.key === 'ArrowDown' ? Math.min(index + 1, motions.length - 1) : Math.max(index - 1, 0)
    if (nextIndex === index) return
    const nextMotion = motions[nextIndex]
    if (!nextMotion) return
    onSelect(nextMotion.motion_id)
    const nextRow = event.currentTarget.parentElement?.querySelectorAll<HTMLElement>('[role="option"]')[nextIndex]
    nextRow?.focus()
  }

  return <div className="motion-rail" aria-label="Motions" role="listbox">
    {motions.map((motion, index) => {
      const chip = motionChipInfo(motion, operations, currentSubjectRevision)
      const selected = selectedId === motion.motion_id
      const operation = operationForMotion(motion, operations)
      const running = chip.spinner
      const checked = checkedIds.has(motion.motion_id)
      return <div
        aria-label={`${motion.name} ${chip.label}`}
        aria-selected={selected}
        className={`motion-rail__row${selected ? ' is-selected' : ''}`}
        key={motion.motion_id}
        onClick={() => onSelect(motion.motion_id)}
        onKeyDown={(event) => {
          handleKeyDown(event, index)
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(motion.motion_id) }
        }}
        role="option"
        tabIndex={selected ? 0 : -1}
      >
        <input
          aria-label={`Include ${motion.name} in the next batch`}
          checked={checked}
          className="motion-rail__checkbox"
          onChange={(event: ChangeEvent<HTMLInputElement>) => { event.stopPropagation(); onToggleChecked(motion.motion_id) }}
          onClick={(event) => event.stopPropagation()}
          type="checkbox"
        />
        <MotionRailThumb motion={motion} projectId={projectId} generating={chip.spinner} />
        <span className="motion-rail__body">
          <span className="motion-rail__name">{motion.name}</span>
          {/* The chip is the ONLY state text a row shows -- a second raw stage line ("queued",
              "generating") used to overflow the row's fixed height and overlap the next
              motion's name. It doubles as the live region for a running row (progress itself
              lives in the bottom-edge bar, not text). */}
          <span aria-live={running ? 'polite' : undefined} className={`chip chip--${chip.tone}`} role={running ? 'status' : undefined}>
            {chip.spinner && <span className="button-spinner" aria-hidden="true" />}{chip.label}
          </span>
          {chip.tone === 'danger' && <small className="motion-rail__failure">{operation?.failure_explanation ?? 'Generation failed before a result was created.'}</small>}
        </span>
        {chip.tone === 'danger' && onRetry && operation?.retryable && <button
          aria-label={`Retry ${motion.name}`}
          className="motion-rail__retry"
          onClick={(event) => { event.stopPropagation(); onRetry(motion.motion_id) }}
          type="button"
        >↻</button>}
        {chip.progressPercent !== null && <span className="motion-rail__progress"><span style={{ width: `${chip.progressPercent}%` }} /></span>}
      </div>
    })}
  </div>
}

function MotionRailThumb({ motion, projectId, generating }: { motion: Motion; projectId: string; generating: boolean }) {
  const candidate: MotionCandidate | undefined = motion.candidates.find((item) => item.candidate_id === motion.selected_candidate_id) ?? motion.candidates.at(-1)
  const preview = useAssetPreview(projectId, candidate?.artifact_key, { enabled: candidate?.metadata?.media_kind === 'sheet' })
  // A spinner replaces the initial badge while work is in flight: the row must read as busy at a glance.
  const initial = generating
    ? <span className="motion-rail__thumb motion-rail__thumb--generating" aria-hidden="true"><span className="button-spinner" /></span>
    : <span className="motion-rail__thumb" aria-hidden="true">{motion.name.charAt(0).toUpperCase()}</span>
  if (!candidate) return initial
  if (candidate.metadata?.media_kind === 'sheet' && preview.data) {
    return <span className={`motion-rail__thumb${generating ? ' motion-rail__thumb--generating' : ''}`}>
      <span aria-label={`${motion.name} thumbnail`} className="motion-rail__thumb-frame" role="img" style={railThumbFrameStyle(preview.data.upload_url, candidate.metadata)} />
      {generating && <span className="motion-rail__thumb-spinner"><span className="button-spinner" aria-hidden="true" /></span>}
    </span>
  }
  return initial
}

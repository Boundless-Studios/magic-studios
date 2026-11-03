import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { studioApi } from '../../api/client'
import { projectKey, useProjectMutation } from '../../api/queries'
import type { ProjectResponse, SubjectRevision, SubjectRevisionEdit } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { generatingLabel } from '../../components/ui/useElapsedSeconds'
import { saveFromUrl } from '../../lib/saveFile'
import { subjectFindingAdvice } from './subjectFindings'
import { decideSubjectApproval, motionsBoundLabel, sourceLabel, takeNumber } from './subjectTakes'

function SubjectTakeThumb({ projectId, revision, name }: { projectId: string; revision: SubjectRevision; name: string }) {
  const preview = useQuery({ queryKey: ['asset-preview', projectId, revision.artifact_key], queryFn: () => studioApi.preview(projectId, revision.artifact_key) })
  return <div className="subject-take-card__thumb checkerboard">{preview.data ? <img alt={name} src={preview.data.upload_url} /> : <small>{preview.isPending ? 'Loading preview' : 'Preview unavailable'}</small>}</div>
}

/** Signs the take on click (the attachment-disposition endpoint, not the inline preview one)
 * and saves it without leaving the page. */
function SubjectDownloadButton({ projectId, revision }: { projectId: string; revision: SubjectRevision }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function download() {
    setBusy(true)
    setError(null)
    try { saveFromUrl((await studioApi.assetDownload(projectId, revision.artifact_key)).upload_url) }
    catch (failure) { setError(failure instanceof Error ? failure.message : 'The download could not be prepared.') }
    finally { setBusy(false) }
  }
  return <>
    <Button disabled={busy} onClick={download}>{busy ? 'Preparing…' : 'Download'}</Button>
    {error && <p className="notice" role="alert">{error}</p>}
  </>
}

/**
 * `useProjectMutation`'s own retry (on a 409) invalidates and refetches the project query, then
 * resubmits the exact same request-building function -- so that function must read the revision
 * from the live cache at call time, not from a value closed over when the function was created.
 * A `project` prop or a `useProject` snapshot from render is exactly such a closed-over value: it
 * cannot change mid-retry no matter how fresh the query becomes. Reading straight off the
 * `QueryClient` is the one thing that does.
 */
function latestRevision(client: ReturnType<typeof useQueryClient>, projectId: string, fallback: number): number {
  return client.getQueryData<ProjectResponse>(projectKey(projectId))?.revision ?? fallback
}

function ChosenCard({ projectId, project, revision, revisions, onCorrectSubmit, correcting, onApproved, onReject, rejecting }: {
  projectId: string
  project: ProjectResponse
  revision: SubjectRevision
  revisions: SubjectRevision[]
  onCorrectSubmit: (revision: SubjectRevision, direction: string) => Promise<unknown>
  correcting: boolean
  onApproved: () => void
  onReject: (revision: SubjectRevision) => void
  rejecting: boolean
}) {
  const subject = project.manifest.subject
  const client = useQueryClient()
  const [correctingOpen, setCorrectingOpen] = useState(false)
  const [correctionText, setCorrectionText] = useState('')
  const [correctionError, setCorrectionError] = useState<string | null>(null)
  // Set once this card's own Approve click lands, so the success state (and the delayed
  // hand-off to Motions) shows immediately without waiting on a project refetch.
  const [justApproved, setJustApproved] = useState(false)
  const approve = useProjectMutation(projectId, () => studioApi.approveSubject(projectId, latestRevision(client, projectId, project.revision)))
  // "Fit to frame" / "Clean up": the same framing and cleanup passes the generation pipeline
  // runs, re-applied to this take, landing as a new selected take.
  const fix = useProjectMutation(projectId, (edit: SubjectRevisionEdit) => studioApi.reframeSubjectRevision(projectId, revision.revision_id, latestRevision(client, projectId, project.revision), edit))
  const approved = subject.approval.state === 'approved' || subject.approval.state === 'waived' || justApproved
  const number = takeNumber(revisions, revision.revision_id)
  const hasFindings = Boolean(subject.assessment?.findings.length)
  const boundLabel = motionsBoundLabel(project.manifest.motions, revision.revision_id)

  // Approve must communicate its result unmistakably (operator, 2026-09-05): success shows a
  // green state block with "Continue to Motions" immediately, then hands off automatically
  // after a beat so the operator sees the confirmation before the page changes; a failure shows
  // the server's own message inline with Retry, never silently.
  async function approveAndContinue() {
    try {
      await approve.mutateAsync()
      setJustApproved(true)
      window.setTimeout(onApproved, 800)
    } catch { /* approve.error renders the inline failure below */ }
  }

  // "Correct with feedback" runs in one step (operator, 2026-09-05): the inline form on this
  // card calls generate directly with this take as the reference, and the shared operations
  // poll (in the parent workspace) then shows the running placeholder card exactly as any other
  // generation does -- no separate press elsewhere is needed.
  async function submitCorrection() {
    setCorrectionError(null)
    try {
      await onCorrectSubmit(revision, correctionText.trim())
      setCorrectingOpen(false)
      setCorrectionText('')
    } catch (error) {
      setCorrectionError(error instanceof Error ? error.message : 'Correction could not be started.')
    }
  }

  return <article className="subject-take-card subject-take-card--chosen" aria-label={`Take ${number}, chosen`}>
    <SubjectTakeThumb projectId={projectId} revision={revision} name={`Take ${number}`} />
    <div className="subject-take-card__meta">
      <strong>Take {number} · {revision.width} × {revision.height} · {sourceLabel(revision.source)}</strong>
      <span className="subject-take-card__badge">Chosen</span>
    </div>
    {boundLabel && <p className="subject-take-card__bound">{boundLabel}</p>}
    {!subject.assessment && <div className="subject-take-card__findings"><h4>Ready when you are</h4><p>Generation creates the canonical transparent PNG used by every motion.</p></div>}
    {subject.assessment && !approved && <div className="subject-take-card__findings">
      <h4>Ready for approval</h4>
      <p>{subject.assessment.width} × {subject.assessment.height} · {hasFindings ? `${subject.assessment.findings.length} thing${subject.assessment.findings.length === 1 ? '' : 's'} to check` : 'Everything looks good'}</p>
      {/* Plain sentence plus the button that fixes it -- never a finding code, and the correction
          box is prefilled with a human instruction (operator, 2026-09-06). */}
      {hasFindings && <ul className="take-card__findings" aria-label="Subject findings">
        {subject.assessment.findings.map((finding) => {
          const advice = subjectFindingAdvice(finding)
          return <li key={finding.action_id ?? finding.code} className="finding-chip finding-chip--warning">
            <span>{advice.sentence}</span>
            <Button disabled={fix.isPending || correcting} onClick={() => {
              if (advice.action === 'correct') { setCorrectionText(advice.correctionPrefill); setCorrectingOpen(true); return }
              fix.mutateAsync(advice.action).catch(() => undefined)
            }}>{advice.actionLabel}</Button>
          </li>
        })}
      </ul>}
      {fix.error instanceof Error && <p className="notice" role="alert">{fix.error.message}</p>}
    </div>}
    {approved && <div className="subject-take-card__findings subject-take-card__findings--success">
      <h4>Subject approved ✓</h4>
      <p className="subject-take-card__approved">Pinned for motion work.</p>
      <Button onClick={onApproved} variant="primary">Continue to Motions</Button>
    </div>}
    {approve.error instanceof Error && <div className="subject-take-card__findings subject-take-card__findings--error" role="alert">
      <h4>Approval failed</h4>
      <p>{approve.error.message}</p>
      <Button onClick={approveAndContinue}>Retry</Button>
    </div>}
    {correctingOpen && <div className="subject-take-card__correct">
      <label>What should change?<input
        onChange={(event) => setCorrectionText(event.target.value)}
        onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); submitCorrection() } }}
        placeholder="e.g. make the cloak green, remove the hat, more margin around the character"
        value={correctionText}
      /></label>
      {correctionError && <p className="notice" role="alert">{correctionError}</p>}
      <div className="subject-take-card__actions">
        <Button disabled={correcting} onClick={submitCorrection} variant="primary">{correcting ? <><span className="button-spinner" aria-hidden="true" /><span role="status">Correcting…</span></> : 'Correct'}</Button>
        <Button disabled={correcting} onClick={() => { setCorrectingOpen(false); setCorrectionError(null) }}>Cancel</Button>
      </div>
    </div>}
    <div className="subject-take-card__actions">
      {!correctingOpen && <Button onClick={() => setCorrectingOpen(true)}>Correct with feedback</Button>}
      <SubjectDownloadButton projectId={projectId} revision={revision} />
      <Button disabled={rejecting} onClick={() => onReject(revision)}>Reject</Button>
      {/* "Whenever approval is pending" (studio-ux-flow.md section 3): shown for any not-yet-
          approved revision that has something to approve, matching the backend's own requirement
          that approval needs a settled assessment. Quality findings are warnings, never blockers
          (operator decision, 2026-09-05): this is always enabled once there is something to
          approve, whatever findings the assessment carries -- only an in-flight approval disables it. */}
      {subject.assessment && !approved && <Button disabled={approve.isPending} onClick={approveAndContinue} variant="primary">{approve.isPending ? <><span className="button-spinner" aria-hidden="true" /><span role="status">Approving…</span></> : 'Approve subject'}</Button>}
    </div>
    {approve.notice && <p className="notice" role="status">{approve.notice}</p>}
  </article>
}

function OtherTakeCard({ projectId, revision, revisions, onChoose, choosing, onReject, rejecting }: {
  projectId: string
  revision: SubjectRevision
  revisions: SubjectRevision[]
  onChoose: (revision: SubjectRevision) => void
  choosing: boolean
  onReject: (revision: SubjectRevision) => void
  rejecting: boolean
}) {
  const number = takeNumber(revisions, revision.revision_id)
  const findingsCount = revision.assessment?.findings.length ?? 0
  return <li className="subject-take-card" aria-label={`Take ${number}`}>
    <SubjectTakeThumb projectId={projectId} revision={revision} name={`Take ${number}`} />
    <div className="subject-take-card__meta">
      <strong>Take {number}</strong>
      <span>{revision.width} × {revision.height}</span>
      <span className="take-card__tag">{sourceLabel(revision.source)}</span>
      {revision.assessment && <span>{findingsCount} finding{findingsCount === 1 ? '' : 's'}</span>}
    </div>
    <div className="subject-take-card__actions">
      <Button variant="primary" onClick={() => onChoose(revision)} disabled={choosing}>Use this subject</Button>
      <Button disabled={rejecting} onClick={() => onReject(revision)}>Reject</Button>
    </div>
  </li>
}

function RejectedTakeCard({ projectId, revision, revisions, onRestore, restoring }: {
  projectId: string
  revision: SubjectRevision
  revisions: SubjectRevision[]
  onRestore: (revision: SubjectRevision) => void
  restoring: boolean
}) {
  const number = takeNumber(revisions, revision.revision_id)
  return <li className="subject-take-card subject-take-card--rejected" aria-label={`Take ${number}, rejected`}>
    <SubjectTakeThumb projectId={projectId} revision={revision} name={`Take ${number}`} />
    <div className="subject-take-card__meta">
      <strong>Take {number}</strong>
      <span>{revision.width} × {revision.height}</span>
      <span className="take-card__tag">Rejected</span>
    </div>
    <div className="subject-take-card__actions">
      <Button disabled={restoring} onClick={() => onRestore(revision)}>Restore</Button>
    </div>
  </li>
}

/**
 * The Subject takes gallery (studio-ux-flow.md section 3): every subject revision, newest first,
 * with the chosen one pinned above the rest, followed by a "Previous subjects" section for
 * everything else (operator, 2026-09-05) so the space below the Chosen card is never empty.
 * Quality findings are warnings, never blockers (operator decision, 2026-09-05, revising
 * decision 1 of section 9): choosing a take always auto-approves it once it has a settled
 * assessment, whatever findings it carries. A running generation shows as a placeholder card
 * in Previous subjects too, with live progress.
 */
export function SubjectTakesGallery({ projectId, project, activeGeneration, elapsedSeconds, onCorrectSubmit, correcting, onApproved }: {
  projectId: string
  project: ProjectResponse
  activeGeneration: boolean
  elapsedSeconds: number | null
  onCorrectSubmit: (revision: SubjectRevision, direction: string) => Promise<unknown>
  correcting: boolean
  onApproved: () => void
}) {
  const subject = project.manifest.subject
  const revisions = subject.revisions ?? (subject.current_revision ? [subject.current_revision] : [])
  const client = useQueryClient()
  // Set from inside the mutation function on the invocation that actually lands the approval, so
  // `chooseAndContinue` can tell -- after `mutateAsync` resolves -- whether *this* choice was the
  // one that auto-approved (decision 1, section 9) and only then hand off to `onApproved`.
  const autoApproved = useRef(false)
  const choose = useProjectMutation(projectId, async (revision: SubjectRevision) => {
    autoApproved.current = false
    const afterSelect = await studioApi.selectSubjectRevision(projectId, revision.revision_id, latestRevision(client, projectId, project.revision))
    const decision = decideSubjectApproval(afterSelect.manifest.subject.assessment, afterSelect.manifest.subject.approval.state)
    if (decision !== 'auto_approve') return afterSelect
    const result = await studioApi.approveSubject(projectId, afterSelect.revision)
    autoApproved.current = true
    return result
  })
  async function chooseAndContinue(revision: SubjectRevision) {
    // A failure is already surfaced through `choose.error`/`choose.notice` below -- this only
    // needs to keep the rejection from reaching the click handler as an unhandled promise.
    try { await choose.mutateAsync(revision) } catch { return }
    if (autoApproved.current) onApproved()
  }
  const reject = useProjectMutation(projectId, (revision: SubjectRevision) => studioApi.rejectSubjectRevision(projectId, revision.revision_id, latestRevision(client, projectId, project.revision)))
  const restore = useProjectMutation(projectId, (revision: SubjectRevision) => studioApi.restoreSubjectRevision(projectId, revision.revision_id, latestRevision(client, projectId, project.revision)))
  // The click handlers only keep a failed mutation from escaping as an unhandled rejection --
  // the message itself renders from `reject.error`/`restore.error` below.
  function rejectRevision(revision: SubjectRevision) { reject.mutateAsync(revision).catch(() => undefined) }
  function restoreRevision(revision: SubjectRevision) { restore.mutateAsync(revision).catch(() => undefined) }
  const current = subject.current_revision
  const rejected = revisions.filter((revision) => revision.lifecycle === 'rejected')
  const others = revisions.filter((revision) => revision.revision_id !== current?.revision_id && revision.lifecycle !== 'rejected')
  return <section className="subject-takes" aria-labelledby="subject-takes-title">
    <div className="subject-takes__heading">
      <h3 id="subject-takes-title">Subject takes</h3>
    </div>
    {/* The running take is the first card in the gallery, so pressing Generate produces something
        visible right next to the button that started it (operator, 2026-09-06). */}
    {activeGeneration && <article className="subject-take-card subject-take-card--placeholder" role="status" aria-label="Generating a new take">
      <div className="subject-take-card__thumb checkerboard subject-take-card__thumb--placeholder"><span className="button-spinner" aria-hidden="true" /></div>
      <div className="subject-take-card__meta"><strong>{generatingLabel(elapsedSeconds)}</strong></div>
    </article>}
    {choose.notice && <p className="notice" role="status">{choose.notice}</p>}
    {choose.error instanceof Error && <p className="notice" role="alert">{choose.error.message}</p>}
    {reject.notice && <p className="notice" role="status">{reject.notice}</p>}
    {reject.error instanceof Error && <p className="notice" role="alert">{reject.error.message}</p>}
    {restore.error instanceof Error && <p className="notice" role="alert">{restore.error.message}</p>}
    {current && <ChosenCard correcting={correcting} onApproved={onApproved} onCorrectSubmit={onCorrectSubmit} onReject={rejectRevision} project={project} projectId={projectId} rejecting={reject.isPending} revision={current} revisions={revisions} />}
    {!current && others.length > 0 && <p className="subject-takes__hint">Choose one to continue</p>}
    {!current && others.length === 0 && <p className="subject-takes__hint">No subject yet · generate one to continue</p>}
    <section aria-labelledby="previous-subjects-title" className="subject-previous">
      <h3 id="previous-subjects-title">Previous subjects</h3>
      {others.length === 0
        ? <p className="subject-takes__hint">No other subjects yet · generate or correct to create one</p>
        : <ul className="take-grid subject-take-grid" aria-label="Other subject takes">
          {others.map((revision) => <OtherTakeCard key={revision.revision_id} projectId={projectId} revision={revision} revisions={revisions} onChoose={chooseAndContinue} choosing={choose.isPending} onReject={rejectRevision} rejecting={reject.isPending} />)}
        </ul>}
      {rejected.length > 0 && <details className="subject-rejected">
        <summary>Rejected takes ({rejected.length})</summary>
        <ul className="take-grid subject-take-grid" aria-label="Rejected subject takes">
          {rejected.map((revision) => <RejectedTakeCard key={revision.revision_id} projectId={projectId} revision={revision} revisions={revisions} onRestore={restoreRevision} restoring={restore.isPending} />)}
        </ul>
      </details>}
    </section>
  </section>
}

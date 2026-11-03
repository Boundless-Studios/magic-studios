import { useEffect, useState } from 'react'
import type { Motion, MotionGenerationSettings, MotionReference, Operation } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { generatingLabel } from '../../components/ui/useElapsedSeconds'
import { CandidateUpload } from './CandidateUpload'
import { GenerationSettings, type MotionSpecPatch } from './GenerationSettings'
import { actionLineFor } from './motionPresets'
import { hasTakesFromOlderSpec } from './motionReadiness'

/**
 * The Generate zone (studio-ux-flow.md section 4, zone 1 of 3): duration, the existing advanced
 * settings, the Generate/Regenerate button as its own progress indicator, Cancel while running,
 * and an inline reveal for uploading a clip that already exists. Collapses to a one-line summary
 * once the motion has takes; a running job forces it back open.
 */
export function GenerateZone({
  motion, generationSettings, onGenerationSettingsChange, onUploadReference, onPatchMotion,
  onGenerate, onCancel, operation, generating, elapsedSeconds, generatePending, cancelPending,
  projectId, expectedRevision,
}: {
  motion: Motion
  generationSettings: MotionGenerationSettings
  onGenerationSettingsChange: (value: MotionGenerationSettings) => void
  onUploadReference: (file: File) => Promise<MotionReference>
  onPatchMotion: (patch: MotionSpecPatch) => void | Promise<void>
  /** Dispatches with whatever "What happens" line is on screen, edited or not. */
  onGenerate: (action: string) => void
  onCancel: () => void
  operation?: Operation
  generating: boolean
  elapsedSeconds: number | null
  generatePending: boolean
  cancelPending: boolean
  projectId: string
  expectedRevision: number
}) {
  const [saveError, setSaveError] = useState<string | null>(null)
  async function persist(patch: MotionSpecPatch) {
    setSaveError(null)
    try { await onPatchMotion(patch) }
    catch (reason) { setSaveError(reason instanceof Error ? reason.message : 'Could not save motion settings. Try again.') }
  }
  const hasTakes = motion.candidates.length > 0
  const [manuallyExpanded, setManuallyExpanded] = useState(false)
  const [showUpload, setShowUpload] = useState(false)
  // The zone stays mounted while the workspace switches motions, so the draft line belongs to
  // whichever motion is current.
  const savedAction = actionLineFor(motion)
  const motionId = motion.motion_id
  const [draft, setDraft] = useState(() => ({ motionId, base: savedAction, value: savedAction }))
  useEffect(() => {
    setDraft((current) => current.motionId !== motionId
      ? { motionId, base: savedAction, value: savedAction }
      : { ...current, base: savedAction, value: current.value === current.base ? savedAction : current.value })
  }, [motionId, savedAction])
  // Polling refreshes the motion object, but must not refill a cleared or edited field.
  const action = draft.motionId === motionId ? draft.value : savedAction
  const expanded = generating || !hasTakes || manuallyExpanded
  const buttonLabel = operation?.status === 'failed' ? 'Retry' : hasTakes ? 'Regenerate' : 'Generate'

  if (!expanded) {
    return <section className="motion-zone motion-zone--generate motion-zone--collapsed" aria-label="Generate">
      <h4>Generate</h4>
      <p className="motion-zone__summary">{motion.candidates.length} take{motion.candidates.length === 1 ? '' : 's'} so far</p>
      <Button onClick={() => setManuallyExpanded(true)}>Expand</Button>
    </section>
  }

  return <section className="motion-zone motion-zone--generate" aria-label="Generate">
    <div className="motion-zone__heading">
      <h4>Generate</h4>
      {hasTakes && <Button onClick={() => setManuallyExpanded(false)} disabled={generating}>Collapse</Button>}
    </div>
    {saveError && <p role="alert">{saveError}</p>}
    <label className="motion-zone__action">What happens
      <input
        aria-label={`What happens in ${motion.name}`}
        onBlur={() => { if (action.trim() && action.trim() !== actionLineFor(motion)) void persist({ action: action.trim() }) }}
        onChange={(event) => setDraft({ motionId, base: savedAction, value: event.target.value })}
        type="text"
        value={action}
      />
    </label>
    {hasTakesFromOlderSpec(motion) && <p className="motion-zone__older-spec">Takes below were made from an earlier description.</p>}
    <GenerationSettings value={generationSettings} onChange={onGenerationSettingsChange} motion={motion} onUploadReference={onUploadReference} onPatchMotion={persist} />
    <div className="motion-zone__actions">
      {operation?.status === 'failed' && !operation.retryable
        ? <p className="operation-failure" role="alert">{operation.failure_explanation ?? `${motion.name} could not be generated.`}</p>
        : <Button onClick={() => onGenerate(action.trim())} disabled={generatePending || generating}>{generating ? <><span className="button-spinner" aria-hidden="true" />{generatingLabel(elapsedSeconds)}</> : `${buttonLabel} ${motion.name}`}</Button>}
      {operation && ['queued', 'processing'].includes(operation.status) && <Button onClick={onCancel} disabled={cancelPending}>{operation.status === 'processing' ? 'Interrupt' : 'Cancel'}</Button>}
    </div>
    {operation && ['queued', 'processing'].includes(operation.status) && operation.generation_settings?.provider_parameters?.execution_provider === 'higgsfield'
      && <p className="motion-zone__note">Higgsfield can only cancel a request it has not started. A started request is still billed to your Higgsfield account, and Studio will not collect its result.</p>}
    {operation?.status === 'failed' && operation.retryable && <p className="operation-failure" role="alert">{operation.failure_explanation ?? `${motion.name} could not be generated.`}</p>}
    <button className="disclosure" type="button" aria-expanded={showUpload} onClick={() => setShowUpload((value) => !value)}>or upload a clip you already have</button>
    {showUpload && <CandidateUpload projectId={projectId} motionId={motion.motion_id} expectedRevision={expectedRevision} />}
  </section>
}

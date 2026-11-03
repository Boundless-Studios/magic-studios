import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ApiError, sentenceCase, studioApi, type MotionDefinition } from '../../api/client'
import { DEFAULT_MOTION_GENERATION_SETTINGS, type Motion, type MotionCandidate, type MotionGenerationSettings, type MotionReference, type ViewId } from '../../api/types'
import { useProject, useProjectMutation, useProjectOperations } from '../../api/queries'
import { Button } from '../../components/ui/Button'
import { useElapsedSeconds } from '../../components/ui/useElapsedSeconds'
import { CharacterViews } from './CharacterViews'
import { ComputeChoice } from './ComputeChoice'
import { choiceOfSettings, choiceUsable, defaultComputeChoice, settingsForChoice, type ComputeChoiceValue } from './computeChoiceModel'
import type { MotionSpecPatch } from './GenerationSettings'
import { uniqueMotionId } from './characterViewModel'
import { MotionList } from './MotionList'
import { GenerateZone } from './GenerateZone'
import { groupCandidatesIntoTakes } from './takeGrouping'
import { TakesZone } from './TakesZone'
import { FramesZone } from './FramesZone'
import { MotionLog } from './MotionLog'
import { SubjectStrip } from './SubjectStrip'
import { MOTION_PRESETS, RECOMMENDED_PRESET_SET, actionLineFor } from './motionPresets'
import { holdForFrame, type FrameDraft } from './frameDraft'
import { batchButtonLabel, defaultChecked, generationSummary, inFlightCheckedCount, readinessSummary, settledSinceDispatch } from './motionReadiness'
import { isOperationActive, operationForMotion } from './operationSelection'

/** A transient 4-second status toast, replacing a persistent "Subject approved and saved" banner. */
function Toast({ message }: { message: string | null }) {
  if (!message) return null
  return <div className="status-toast" role="status" aria-live="polite">{message}</div>
}

export function MotionWorkspace({ projectId }: { projectId: string }) {
  const location = useLocation()
  const project = useProject(projectId); const operations = useProjectOperations(projectId); const queryClient = useQueryClient()
  const machineConfig = useQuery({ queryKey: ['generation-config'], queryFn: studioApi.getGenerationConfig, retry: false, staleTime: 30_000 })
  const [computeChoice, setComputeChoice] = useState<ComputeChoiceValue | null>(null)
  const activeComputeChoice = computeChoice ?? defaultComputeChoice(machineConfig.data)
  // A public account with no usable route: dispatch would only be refused.
  const batchComputeBlocked = !choiceUsable(machineConfig.data, activeComputeChoice)
  const generationDefaults = settingsForChoice(machineConfig.data, activeComputeChoice,
    machineConfig.data?.settings ?? machineConfig.data?.default_settings ?? DEFAULT_MOTION_GENERATION_SETTINGS)

  const [selectedViewId, setSelectedViewId] = useState<ViewId | null>(null)
  const [viewSaving, setViewSaving] = useState(false)
  const [bodyReferenceByView, setBodyReferenceByView] = useState<Record<string, string>>({})
  const [selectedId, setSelectedId] = useState<string | null>(null); const [adding, setAdding] = useState(false); const [customMode, setCustomMode] = useState(false)
  const [checkedOverrides, setCheckedOverrides] = useState<Record<string, boolean>>({})
  // motion_id -> take count at dispatch, so a settled motion can drop its own tick (see
  // `settledSinceDispatch`). `batchDraft` holds the confirm sheet's editable lines while open.
  const [dispatched, setDispatched] = useState<Record<string, number>>({})
  const [batchDraft, setBatchDraft] = useState<Record<string, string> | null>(null)
  const [generationSettingsByMotion, setGenerationSettingsByMotion] = useState<Record<string, MotionGenerationSettings>>({})
  /** History-seeded settings also move the workspace route, and drafts made for the other route
   * are dropped, so the picker, single generation and the batch always send to the same place. */
  function seedMotionSettings(motionId: string, settings: MotionGenerationSettings) {
    setComputeChoice(choiceOfSettings(settings))
    setGenerationSettingsByMotion({ [motionId]: settings })
  }
  const [editingCandidate, setEditingCandidate] = useState<{ motionId: string; candidateId: string } | null>(null)
  const addButtonRef = useRef<HTMLButtonElement>(null)
  const overlayRef = useRef<HTMLDivElement>(null)
  const [toast, setToast] = useState<string | null>(null)
  useEffect(() => { if (adding) overlayRef.current?.focus() }, [adding])
  useEffect(() => {
    if (!adding) return
    const dismiss = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setAdding(false); setCustomMode(false); addButtonRef.current?.focus()
    }
    document.addEventListener('keydown', dismiss)
    return () => document.removeEventListener('keydown', dismiss)
  }, [adding])
  const select = useProjectMutation(projectId, ({ motionId, candidateId }: { motionId: string; candidateId: string }) => studioApi.selectCandidate(projectId, motionId, candidateId, project.data!.revision))
  const generate = useProjectMutation(projectId, ({ motionId, settings, action }: { motionId: string; settings: MotionGenerationSettings; action?: string | null }) => studioApi.generateMotion(projectId, motionId, project.data!.revision, settings, action ?? null))
  const generateBatch = useProjectMutation(projectId, ({ motionIds, actions }: { motionIds: string[]; actions: Record<string, string> }) => studioApi.generateMotions(projectId, project.data!.revision, motionIds, generationDefaults, false, actions))
  const revise = useProjectMutation(projectId, ({ motionId, candidateId, draft }: { motionId: string; candidateId: string; draft: FrameDraft }) => studioApi.createMotionRevision(projectId, motionId, project.data!.revision, candidateId, draft.orderedFrames.map((index) => ({ index, hold: holdForFrame(draft, index) })), draft.frameDurationMs, draft.loopPolicy, draft.flipHorizontal, draft.targetFrameCount, draft.cellSize, draft.normalization ?? null))
  const deriveAlpha = useProjectMutation(projectId, ({ motionId, candidateId }: { motionId: string; candidateId: string }) => studioApi.deriveAlpha(projectId, motionId, candidateId, project.data!.revision))
  const retrySettlement = useMutation({ mutationFn: () => studioApi.reconcileActivity(projectId), onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ['project', projectId] }); await queryClient.invalidateQueries({ queryKey: ['operations', projectId] }); await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }) } })
  const cancel = useMutation({ mutationFn: ({ motionId, jobId }: { motionId: string; jobId: string }) => studioApi.cancelMotionOperation(projectId, motionId, jobId), onSuccess: async () => { await queryClient.invalidateQueries({ queryKey: ['operations', projectId] }); await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }) } })
  const add = useProjectMutation(projectId, (motion: MotionDefinition) => studioApi.addMotion(projectId, project.data!.revision, motion))
  const patchMotion = useProjectMutation(projectId, ({ motionId, motion }: { motionId: string; motion: MotionDefinition }) => studioApi.updateMotion(projectId, motionId, project.data!.revision, motion))
  const addRecommendedSet = useProjectMutation(projectId, () => studioApi.addMotions(projectId, project.data!.revision, RECOMMENDED_PRESET_SET.map((preset) => {
    const definition = MOTION_PRESETS.find((item) => item.preset === preset)!
    return { motion_id: uniqueMotionId(project.data!.manifest.motions, selectedViewId ? `${selectedViewId.toLowerCase()}-${preset}` : preset), view_id: selectedViewId, name: definition.displayName, kind: 'preset' as const, preset }
  })))
  // A history "Regenerate with these settings" seeds one motion and its draft settings via
  // router state, read once so a later re-navigation to this same route does not keep
  // clobbering the operator's own selection.
  const seedSettings = (location.state as { seedSettings?: { motionId: string; settings: MotionGenerationSettings } } | null)?.seedSettings
  const selectMotionId = (location.state as { selectMotionId?: string } | null)?.selectMotionId
  const initializedProjectRef = useRef<string | null>(null)
  useEffect(() => {
    if (!project.data || initializedProjectRef.current === projectId) return
    // Resolve both levels of navigation from the same loaded manifest. Applying the
    // handoff before this read would hide a named-view motion in Original / unassigned.
    const manifest = project.data.manifest
    const requestedId = seedSettings?.motionId ?? selectMotionId
    const target = manifest.motions.find((motion) => motion.motion_id === requestedId) ?? manifest.motions[0]
    initializedProjectRef.current = projectId
    setSelectedViewId(target ? target.view_id ?? null : manifest.views?.[0]?.view_id ?? null)
    setSelectedId(target?.motion_id ?? null)
    if (seedSettings && target?.motion_id === seedSettings.motionId) seedMotionSettings(seedSettings.motionId, seedSettings.settings)
  }, [project.data, projectId, seedSettings, selectMotionId])
  useEffect(() => {
    if (!(location.state as { subjectApproved?: boolean } | null)?.subjectApproved) return
    setToast('Subject approved and saved.')
    const timer = window.setTimeout(() => setToast(null), 4000)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    const handoffNotice = (location.state as { handoffNotice?: string } | null)?.handoffNotice
    if (!handoffNotice) return
    setToast(handoffNotice)
    const timer = window.setTimeout(() => setToast(null), 4000)
    return () => window.clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const selectedMotion = project.data?.manifest.motions.find((motion) => motion.motion_id === selectedId && (motion.view_id ?? null) === selectedViewId)
  const generationSettings = selectedMotion ? generationSettingsByMotion[selectedMotion.motion_id] ?? generationDefaults : generationDefaults
  const updateGenerationSettings = (settings: MotionGenerationSettings) => { if (selectedMotion) setGenerationSettingsByMotion((current) => ({ ...current, [selectedMotion.motion_id]: settings })) }
  const selectedOperation = selectedMotion ? operationForMotion(selectedMotion, operations.data ?? []) : undefined
  const selectedGenerationActive = Boolean(selectedOperation && isOperationActive(selectedOperation))
  const selectedSettlementBlocked = selectedOperation?.status === 'completed' && selectedOperation.stage !== 'settled' ? selectedOperation.settlement_blocked ?? null : null
  const selectedElapsedSeconds = useElapsedSeconds(selectedGenerationActive)
  // A settled batch drops its own ticks: a motion that produced a take is finished with, while
  // one that failed produced nothing and stays ticked for the next click.
  useEffect(() => {
    const settled = settledSinceDispatch(project.data?.manifest.motions ?? [], operations.data ?? [], dispatched)
    if (!settled.length) return
    setCheckedOverrides((current) => { const next = { ...current }; for (const id of settled) next[id] = false; return next })
    setDispatched((current) => { const next = { ...current }; for (const id of settled) delete next[id]; return next })
  }, [project.data, operations.data, dispatched])
  if (project.isPending) return <section className="workspace-state" role="status">Opening motions</section>
  if (!project.data) return <section className="workspace-state">Motions unavailable</section>
  const allMotions = project.data.manifest.motions
  const motions = allMotions.filter((motion) => (motion.view_id ?? null) === selectedViewId)
  const selectedView = project.data.manifest.views?.find((view) => view.view_id === selectedViewId)
  const missingViewReference = Boolean(selectedViewId && !selectedView?.reference)
  const currentSubjectRevision = project.data.manifest.subject.current_revision
  const operationList = operations.data ?? []
  const isChecked = (motion: Motion) => checkedOverrides[motion.motion_id] ?? defaultChecked(motion, operationList)
  const checkedIds = new Set(motions.filter(isChecked).map((motion) => motion.motion_id))
  const batchSummary = generationSummary(motions, operationList)
  const batchGenerating = inFlightCheckedCount(motions, operationList, checkedIds) > 0
  const notice = select.notice || generate.notice || generateBatch.notice || revise.notice || add.notice || addRecommendedSet.notice || patchMotion.notice
  const error = select.error || generate.error || generateBatch.error || revise.error || add.error || addRecommendedSet.error || patchMotion.error || operations.error
  const errorMessage = error instanceof Error ? sentenceCase(error.message) : null
  const unapprovedSubject = error instanceof ApiError && error.message.includes('requires the approved current subject')
  function closeAdding() { setAdding(false); setCustomMode(false); addButtonRef.current?.focus() }
  async function addPreset(motionId: string, name: string, preset: MotionDefinition['preset']) { const id = uniqueMotionId(allMotions, selectedViewId ? `${selectedViewId.toLowerCase()}-${motionId}` : motionId); try { await add.mutateAsync({ motion_id: id, view_id: selectedViewId, name, kind: 'preset', preset }); closeAdding(); setSelectedId(id) } catch { /* displayed above */ } }
  async function addCustomMotion(data: FormData) { const name = String(data.get('name')); const prompt = String(data.get('prompt')); const base = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || `motion-${Date.now()}`; const id = uniqueMotionId(allMotions, selectedViewId ? `${selectedViewId.toLowerCase()}-${base}` : base); try { await add.mutateAsync({ motion_id: id, view_id: selectedViewId, name, kind: 'custom', prompt }); closeAdding(); setSelectedId(id) } catch { /* mutation error displayed above */ } }
  async function addRecommended() { try { const result = await addRecommendedSet.mutateAsync(); const first = result.manifest.motions.at(-RECOMMENDED_PRESET_SET.length); if (first) setSelectedId(first.motion_id) } catch { /* mutation error displayed above */ } }
  async function regenerate(motionId: string, settings: MotionGenerationSettings = generationSettings, action?: string) { if (viewSaving || missingViewReference) return; try { await generate.mutateAsync({ motionId, settings, action }); await queryClient.invalidateQueries({ queryKey: ['operations', projectId] }); await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }) } catch { /* mutation renders the professional error state */ } }
  /** The batch button confirms through a sheet: the operator says what each ticked motion does
   * before six clips render, not after. */
  function openBatchSheet() {
    if (viewSaving || missingViewReference) return
    setBatchDraft(Object.fromEntries(motions.filter((motion) => checkedIds.has(motion.motion_id)).map((motion) => [motion.motion_id, actionLineFor(motion)])))
  }
  async function confirmBatch() {
    const draft = batchDraft ?? {}
    const targets = Object.keys(draft)
    if (!targets.length) { setBatchDraft(null); return }
    const actions = Object.fromEntries(Object.entries(draft).filter(([, line]) => line.trim()).map(([id, line]) => [id, line.trim()]))
    try {
      await generateBatch.mutateAsync({ motionIds: targets, actions })
      setBatchDraft(null)
      // The ticked set survives dispatch so the batch can be re-run; without an explicit
      // override the default would drop each motion the moment its job goes in flight.
      setCheckedOverrides((current) => { const next = { ...current }; for (const id of targets) next[id] = true; return next })
      setDispatched(Object.fromEntries(targets.map((id) => [id, motions.find((motion) => motion.motion_id === id)?.candidates.length ?? 0])))
    } catch { /* mutation renders the professional error state */ }
  }
  function toggleChecked(motionId: string) {
    const motion = motions.find((item) => item.motion_id === motionId)
    if (!motion) return
    setCheckedOverrides((current) => ({ ...current, [motionId]: !isChecked(motion) }))
  }
  async function uploadMotionReference(motion: Motion, file: File): Promise<MotionReference> {
    const target = await studioApi.createMotionReferenceUpload(projectId, motion.motion_id, project.data!.revision, file.type)
    await studioApi.upload(target, file)
    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
    const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
    return { key: target.key, sha256, role: 'context' }
  }
  async function patchMotionSpec(motion: Motion, patch: MotionSpecPatch) {
    await patchMotion.mutateAsync({
      motionId: motion.motion_id,
      motion: {
        motion_id: motion.motion_id, name: motion.name, kind: motion.kind,
        view_id: patch.view_id !== undefined ? patch.view_id : motion.view_id ?? null,
        preset: motion.preset ?? undefined,
        action: patch.action ?? motion.action ?? undefined,
        // A custom motion has no preset template, so its "what happens" line is its prompt.
        prompt: (motion.kind === 'custom' ? patch.action : undefined) ?? motion.prompt ?? undefined,
        loop_policy: motion.loop_policy, required: motion.required,
        references: patch.references ?? motion.references ?? [],
        style: patch.style !== undefined ? patch.style : motion.style ?? null,
        script: patch.script !== undefined ? patch.script : motion.script ?? null,
      },
    })
  }
  const editingMotion = editingCandidate && editingCandidate.motionId === selectedMotion?.motion_id ? selectedMotion : undefined
  const editingCandidateObject: MotionCandidate | undefined = editingMotion?.candidates.find((candidate) => candidate.candidate_id === editingCandidate?.candidateId)
  const sameViewWalks = motions.filter((motion) => motion.selected_candidate_id)
  const selectedWalk = selectedViewId ? sameViewWalks.find((motion) => motion.motion_id === bodyReferenceByView[selectedViewId]) : sameViewWalks[0]
  const walkReference = selectedWalk?.candidates.find((candidate) => candidate.candidate_id === selectedWalk.selected_candidate_id)
  const editingTakeIndex = editingCandidateObject ? (groupCandidatesIntoTakes(editingMotion!.candidates).find((take) => take.primary.candidate_id === editingCandidateObject.candidate_id || take.keyed?.candidate_id === editingCandidateObject.candidate_id)?.index ?? 0) : 0

  return <section className="workspace workspace--fit" aria-labelledby="motions-title">
    <div className="workspace--fit__header">
      <SubjectStrip projectId={projectId} subjectRevision={currentSubjectRevision} externalSubject={project.data.manifest.external_subject} projectName={project.data.manifest.name} viewLabel={selectedView?.name} />
      <header className="workspace-heading">
        <div><p className="eyebrow">2 · Motions</p><h2 id="motions-title">Build the animation set</h2></div>
        <button className="button button--primary" ref={addButtonRef} type="button" onClick={() => setAdding(true)}>Add motion</button>
      </header>
      <Toast message={toast} />
      {(notice || error) && <p className="notice" role="status">
        {notice ?? errorMessage ?? 'That motion action could not be completed. Your other motions are unchanged.'}
        {!notice && unapprovedSubject && <> <Link to={`/projects/${projectId}/subject`}>Approve the subject</Link></>}
      </p>}
    </div>
    <div className="motion-layout">
      <div className="motion-rail-panel">
      <CharacterViews project={project.data} selectedViewId={selectedViewId} onBusyChange={setViewSaving} disabled={generate.isPending || generateBatch.isPending || patchMotion.isPending || operationList.some(isOperationActive)} onSelect={(viewId) => { setSelectedViewId(viewId); setSelectedId(allMotions.find((motion) => (motion.view_id ?? null) === viewId)?.motion_id ?? null); setBatchDraft(null); closeAdding() }} />

        <p className="motion-rail-panel__readiness">{readinessSummary(motions, operationList, currentSubjectRevision)}</p>
        {/* Not a live region: each in-flight row's own chip already announces. */}
        {batchSummary && <p className="motion-rail-panel__generating"><span className="button-spinner" aria-hidden="true" />{batchSummary}</p>}
        {checkedIds.size > 0 && <Button variant="primary" onClick={openBatchSheet} disabled={generateBatch.isPending || batchGenerating || viewSaving || missingViewReference || batchComputeBlocked}>
          {batchGenerating && <span className="button-spinner" aria-hidden="true" />}{batchButtonLabel(motions, operationList, checkedIds)}
        </Button>}
        {batchDraft && <div className="batch-sheet" role="dialog" aria-label="What happens in each motion">
          <h4>What happens in each motion?</h4>
          <p className="batch-sheet__hint">Say it in a few words — this is what the clip will show.</p>
          {Object.keys(batchDraft).map((motionId) => <label className="batch-sheet__row" key={motionId}>
            {motions.find((motion) => motion.motion_id === motionId)?.name ?? motionId}
            <input
              aria-label={`What happens in ${motions.find((motion) => motion.motion_id === motionId)?.name ?? motionId}`}
              onChange={(event) => setBatchDraft((current) => ({ ...current, [motionId]: event.target.value }))}
              type="text"
              value={batchDraft[motionId]}
            />
          </label>)}
          <div className="batch-sheet__actions">
            <Button variant="primary" onClick={confirmBatch} disabled={generateBatch.isPending || viewSaving || missingViewReference || batchComputeBlocked}>Generate</Button>
            <Button onClick={() => setBatchDraft(null)} disabled={generateBatch.isPending}>Cancel</Button>
          </div>
        </div>}
        <MotionList projectId={projectId} motions={motions} operations={operationList} selectedId={selectedId} onSelect={setSelectedId} onRetry={(motionId) => { setSelectedId(motionId); void regenerate(motionId) }} checkedIds={checkedIds} onToggleChecked={toggleChecked} currentSubjectRevision={currentSubjectRevision} />
      </div>
      <div className="motion-detail">
        {selectedMotion ? <>
          <div className="motion-detail__title-row">
            <h3>{selectedMotion.name}</h3>
            <span className="motion-detail__tag">{selectedMotion.kind === 'preset' ? 'Preset' : 'Custom'}</span>
          </div>
          <div className="motion-detail__body motion-detail__body--scroll">
            {(project.data.manifest.views?.length ?? 0) > 0 && <label className="view-body-reference">Animation view<select aria-label="Animation view" value={selectedMotion.view_id ?? ''} disabled={selectedGenerationActive || patchMotion.isPending || viewSaving} onChange={(event) => { const viewId = (event.target.value || null) as ViewId | null; void patchMotionSpec(selectedMotion, { view_id: viewId, references: viewId ? (selectedMotion.references ?? []).filter((reference) => reference.role !== 'start') : selectedMotion.references }).then(() => setSelectedViewId(viewId)).catch(() => { /* mutation error displayed above */ }) }}><option value="">Original / unassigned</option>{project.data.manifest.views?.map((view) => <option value={view.view_id} key={view.view_id}>{view.name}</option>)}</select><small>Moving an animation preserves its takes. A named view supplies its reference artwork.</small></label>}
            {missingViewReference && <p role="status" className="notice">Upload a matching view reference in the view panel before generating. Existing takes remain available.</p>}
            {/* Switching where work runs drops per-motion drafts, which carry the old route's parameters. */}
            <ComputeChoice config={machineConfig.data} value={choiceOfSettings(generationSettings)} disabled={selectedGenerationActive} onChange={(choice) => { setComputeChoice(choice); setGenerationSettingsByMotion({}) }} />
            <GenerateZone
              motion={selectedMotion}
              generationSettings={generationSettings}
              onGenerationSettingsChange={updateGenerationSettings}
              onUploadReference={(file) => uploadMotionReference(selectedMotion, file)}
              onPatchMotion={(patch) => patchMotionSpec(selectedMotion, patch)}
              onGenerate={(action) => void regenerate(selectedMotion.motion_id, generationSettings, action)}
              onCancel={() => selectedOperation && cancel.mutate({ motionId: selectedMotion.motion_id, jobId: selectedOperation.job_id })}
              operation={selectedOperation}
              generating={selectedGenerationActive}
              elapsedSeconds={selectedElapsedSeconds}
              generatePending={generate.isPending || viewSaving || missingViewReference || !choiceUsable(machineConfig.data, choiceOfSettings(generationSettings))}
              cancelPending={cancel.isPending}
              projectId={projectId}
              expectedRevision={project.data.revision}
            />
            <TakesZone
              projectId={projectId}
              motion={selectedMotion}
              currentSubjectRevision={currentSubjectRevision}
              onSelect={(candidateId) => select.mutate({ motionId: selectedMotion.motion_id, candidateId })}
              selecting={select.isPending}
              onEditFrames={(candidate) => setEditingCandidate({ motionId: selectedMotion.motion_id, candidateId: candidate.candidate_id })}
              onRegenerateStale={() => void regenerate(selectedMotion.motion_id)}
              generating={selectedGenerationActive}
              settlementBlocked={selectedSettlementBlocked}
              onRetrySettlement={() => retrySettlement.mutate()}
              retryingSettlement={retrySettlement.isPending}
            />
            {editingMotion && editingCandidateObject && selectedViewId && <label className="view-body-reference">Body-scale reference<select aria-label="Body-scale reference" value={bodyReferenceByView[selectedViewId] ?? ''} onChange={(event) => setBodyReferenceByView((current) => ({ ...current, [selectedViewId]: event.target.value }))}><option value="">Choose a comparable take from this view</option>{sameViewWalks.map((motion) => <option key={motion.motion_id} value={motion.motion_id}>{motion.name} · {motion.motion_id}</option>)}</select></label>}
            {editingMotion && editingCandidateObject && <FramesZone
              projectId={projectId}
              motionId={editingMotion.motion_id}
              motionName={editingMotion.name}
              motionPreset={editingMotion.preset}
              walkReference={walkReference}
              candidate={editingCandidateObject}
              takeIndex={editingTakeIndex}
              fallbackLoop={editingMotion.loop_policy}
              onSave={(draft) => revise.mutateAsync({ motionId: editingMotion.motion_id, candidateId: editingCandidateObject.candidate_id, draft }).then((result) => {
                // Saving creates a new take and selects it (studio-ux-flow.md section 4): stay on
                // Frames, now editing that new take, so the editor's own "Saved · N frames" state
                // confirms it rather than the zone just vanishing.
                const savedMotion = result.manifest.motions.find((motion) => motion.motion_id === editingMotion.motion_id)
                const newCandidateId = savedMotion?.selected_candidate_id ?? savedMotion?.candidates.at(-1)?.candidate_id
                if (newCandidateId) setEditingCandidate({ motionId: editingMotion.motion_id, candidateId: newCandidateId })
              })}
              onDeriveAlpha={() => deriveAlpha.mutate({ motionId: editingMotion.motion_id, candidateId: editingCandidateObject.candidate_id })}
              derivingAlpha={deriveAlpha.isPending}
              onClose={() => setEditingCandidate(null)}
            />}
            <MotionLog projectId={projectId} motionId={selectedMotion.motion_id} onRegenerateWithSettings={(settings) => { seedMotionSettings(selectedMotion.motion_id, settings); void regenerate(selectedMotion.motion_id, settings) }} />
          </div>
        </> : !adding && <div className="empty-state"><p className="eyebrow">Recommended set</p><h3>Idle, Walk, Attack, Guard, Cast, Hurt</h3><p>The six motions a combat stage expects, generated as one coherent set.</p><Button variant="primary" onClick={addRecommended} disabled={addRecommendedSet.isPending}>Add the recommended set</Button></div>}
        {adding && <div className="motion-detail__overlay" ref={overlayRef} role="dialog" aria-label="Add a motion" tabIndex={-1}>
          <div className="motion-detail__overlay-header"><h3>Add a motion{selectedView ? ` · ${selectedView.name}` : ''}</h3><Button onClick={closeAdding}>Close</Button></div>
          <div className="motion-detail__overlay-body">
            {!customMode && <div className="preset-grid" role="group" aria-label="Add a motion">
              {MOTION_PRESETS.map((preset) => <article className="preset-card" key={preset.preset}><h4>{preset.displayName}</h4><p>{preset.description}</p><Button onClick={() => addPreset(preset.preset, preset.displayName, preset.preset)} disabled={add.isPending}>{`Add ${preset.displayName}`}</Button></article>)}
              <article className="preset-card preset-card--custom"><h4>Custom…</h4><p>Describe a motion of your own.</p><Button onClick={() => setCustomMode(true)}>Custom…</Button></article>
            </div>}
            {customMode && <form className="inline-form" action={addCustomMotion}><label>Motion name<input name="name" required /></label><label>What happens<textarea name="prompt" required /></label><Button variant="primary" type="submit">Create motion</Button></form>}
          </div>
        </div>}
      </div>
    </div>
  </section>
}

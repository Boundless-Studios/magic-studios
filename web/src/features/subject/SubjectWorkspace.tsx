import { useEffect, useRef, useState, type DragEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { sentenceCase, studioApi } from '../../api/client'
import { useProject, useProjectMutation, useProjectOperations } from '../../api/queries'
import { isSubjectOperation, type SubjectRevision } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { generatingLabel, useElapsedSeconds } from '../../components/ui/useElapsedSeconds'
import { readSubjectDraft, saveSubjectDraft } from './subjectDraft'
import { SubjectLog } from './SubjectLog'
import { SubjectTakesGallery } from './SubjectTakesGallery'

const allowedTypes = new Set(['image/png', 'image/jpeg', 'image/webp'])
const maxReferenceBytes = 20 * 1024 * 1024

export function SubjectWorkspace({ projectId }: { projectId: string }) {
  const initialDraft = readSubjectDraft(projectId)
  const project = useProject(projectId)
  const operations = useProjectOperations(projectId)
  const targets = useQuery({ queryKey: ['targets'], queryFn: studioApi.targets })
  const queryClient = useQueryClient(); const navigate = useNavigate()
  const [references, setReferences] = useState<File[]>(initialDraft.references); const [prepared, setPrepared] = useState<File | null>(null); const [direction, setDirection] = useState(initialDraft.direction); const [fileError, setFileError] = useState<string | null>(null); const [advanced, setAdvanced] = useState(false); const [previews, setPreviews] = useState<string[]>([])
  // "Start another take" (operator, 2026-09-06): a quiet hint shown after the button clears the
  // generation context -- references and guidance are kept, so it points at Generate, not at the
  // drop zone. Cleared as soon as a reference is added or a generation starts.
  const [newSubjectHint, setNewSubjectHint] = useState(false)
  // Set by the first "Start another take" press while a subject is approved: the button then
  // asks for confirmation inline before dropping the generation context.
  const [confirmAnotherTake, setConfirmAnotherTake] = useState(false)
  const [dragging, setDragging] = useState(false)
  const referenceInputRef = useRef<HTMLInputElement>(null)
  const guidanceRef = useRef<HTMLTextAreaElement>(null)
  const [system, setSystem] = useState(''); const [entityKind, setEntityKind] = useState(''); const [entityId, setEntityId] = useState(''); const [displayName, setDisplayName] = useState('')
  const selectedTarget = targets.data?.find((item) => item.system === system) ?? targets.data?.[0]
  useEffect(() => { const urls = references.map((file) => URL.createObjectURL(file)); setPreviews(urls); return () => urls.forEach((url) => URL.revokeObjectURL(url)) }, [references])
  useEffect(() => { saveSubjectDraft(projectId, { references, direction }) }, [direction, projectId, references])
  const [lastGenerateArgs, setLastGenerateArgs] = useState<{ keys: string[]; direction: string } | null>(null)
  const generate = useProjectMutation(projectId, async () => { if (!project.data || !references.length) return; const keys: string[] = []; for (const file of references) { const target = await studioApi.createReferenceUpload(projectId, project.data.revision, file.type); await studioApi.upload(target, file); keys.push(target.key) } setLastGenerateArgs({ keys, direction }); return studioApi.generateSubject(projectId, project.data.revision, keys, direction) })
  const retryGenerate = useProjectMutation(projectId, async () => { if (!project.data || !lastGenerateArgs) return; return studioApi.generateSubject(projectId, project.data.revision, lastGenerateArgs.keys, lastGenerateArgs.direction) })
  const usePrepared = useProjectMutation(projectId, async () => { if (!project.data || !prepared) return; const target = await studioApi.createUpload(projectId, project.data.revision); await studioApi.upload(target, prepared); return studioApi.settleSubject(projectId, project.data.revision, target.key) })
  // "Correct with feedback" (studio-ux-flow.md section 3, revised operator 2026-09-05): runs
  // entirely from the Chosen card's own inline form now -- this mutation just does the upload +
  // generate work, keyed on the chosen take's own `artifact_key` (verified accepted alongside
  // uploaded reference keys in generate_subject, src/magic_video_editor/studio/service.py) first,
  // followed by any staged uploaded references.
  const correctSubject = useProjectMutation(projectId, async ({ revision, direction: correctionDirection }: { revision: SubjectRevision; direction: string }) => {
    if (!project.data) return
    const keys: string[] = [revision.artifact_key]
    for (const file of references) { const target = await studioApi.createReferenceUpload(projectId, project.data.revision, file.type); await studioApi.upload(target, file); keys.push(target.key) }
    return studioApi.generateSubject(projectId, project.data.revision, keys, correctionDirection)
  })
  const externalSubject = project.data?.manifest.external_subject ?? null
  useEffect(() => {
    setSystem(externalSubject?.system ?? targets.data?.[0]?.system ?? '')
    setEntityKind(externalSubject?.entity_kind ?? targets.data?.[0]?.entity_kinds[0] ?? '')
    setEntityId(externalSubject?.entity_id ?? '')
    setDisplayName(externalSubject?.display_name ?? '')
    // Re-seed only when the saved binding itself changes, not on every keystroke into the draft fields.
  }, [externalSubject?.system, externalSubject?.entity_kind, externalSubject?.entity_id, externalSubject?.display_name, targets.data])
  const saveExternalSubject = useProjectMutation(projectId, () => studioApi.patchProject(projectId, project.data!.revision, { system, entity_kind: entityKind, entity_id: entityId.trim(), display_name: displayName.trim() }))
  const clearExternalSubject = useProjectMutation(projectId, () => studioApi.patchProject(projectId, project.data!.revision, null))
  // BOU-3208: a job's `status` stays "completed" forever, even once it settles -- only `stage`
  // (queued/generating/awaiting_settlement -> settled) advances. Matching on status alone kept
  // this stuck "active" (Generate disabled, "generating" showing) for good after the very first
  // subject generation, since the same completed job keeps showing up in the operations listing.
  // Computed ahead of the early returns below so the elapsed-seconds hook is called unconditionally.
  const active = operations.data?.find((item) => isSubjectOperation(item) && item.status !== 'failed' && item.status !== 'cancelled' && item.stage !== 'settled')
  const elapsedSeconds = useElapsedSeconds(Boolean(active))
  if (project.isPending) return <WorkspaceState label="Opening subject" />
  if (!project.data) return <WorkspaceState label="Subject unavailable" />
  const subject = project.data.manifest.subject
  const revisions = subject.revisions ?? (subject.current_revision ? [subject.current_revision] : [])
  const subjectApproved = subject.approval.state === 'approved' || subject.approval.state === 'waived'
  const showGallery = references.length > 0 || revisions.length > 0
  // A subject generation failure surfaces inline next to the button that started it, not just
  // in the shared notice banner -- so this is scoped to generate_subject specifically.
  const subjectFailure = !active ? [...(operations.data ?? [])].reverse().find((item) => item.operation === 'generate_subject' && item.status === 'failed') : undefined
  function addFiles(files: File[]) { const next = [...references]; for (const file of files) { if (!allowedTypes.has(file.type)) { setFileError('Choose PNG, JPEG, or WebP reference images.'); return } if (file.size > maxReferenceBytes) { setFileError('Each reference must be smaller than 20 MB.'); return } if (next.length < 6) next.push(file) } setFileError(null); setReferences(next); setNewSubjectHint(false) }
  // A file dropped on the zone only reaches `addFiles` if every drag event is prevented first --
  // without that the browser navigates to the image and the drop looks dead (operator, 2026-09-06).
  function onDropReferences(event: DragEvent<HTMLElement>) { event.preventDefault(); setDragging(false); addFiles(Array.from(event.dataTransfer.files)) }
  function onDragOverReferences(event: DragEvent<HTMLElement>) { event.preventDefault(); setDragging(true) }
  // The direction stays in the field after submit (it used to clear): it is very often reused
  // across regenerations, and clearing it just to prefill it back from the server carries no
  // benefit -- the operation response never carries a subject job's direction (BOU-3208).
  // References stay visible while generating and after it settles -- they used to be cleared the
  // moment generation started, but the operator wants them kept for the next Regenerate rather
  // than treated as consumed (BOU-3208 operator feedback).
  async function startGeneration() { setNewSubjectHint(false); try { await generate.mutateAsync() } catch { /* mutation renders the professional error state */ return } await queryClient.invalidateQueries({ queryKey: ['operations', projectId] }); await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }) }
  async function retryGeneration() { try { await retryGenerate.mutateAsync() } catch { /* mutation renders the professional error state */ return } await queryClient.invalidateQueries({ queryKey: ['operations', projectId] }); await queryClient.invalidateQueries({ queryKey: ['activity', projectId] }) }
  function continueToMotions() { navigate(`/projects/${projectId}/motions`, { replace: true, state: { subjectApproved: true } }) }
  // "Correct with feedback" runs in one step from the Chosen card's own inline form: it uploads
  // any staged references, calls generate with the chosen take's key first, and lets the shared
  // operations poll pick up the resulting job the same way any other generation is tracked.
  async function submitCorrection(revision: SubjectRevision, correctionDirection: string) {
    await correctSubject.mutateAsync({ revision, direction: correctionDirection })
    await queryClient.invalidateQueries({ queryKey: ['operations', projectId] })
    await queryClient.invalidateQueries({ queryKey: ['activity', projectId] })
  }
  // "Start another take" (operator, 2026-09-06, replacing "New subject"): the references and the
  // guidance are what the operator just built, so they are kept -- only the generation context
  // (the retry args of the last job, any stale error) is dropped. The chosen subject itself stays
  // on the server until a new take is chosen, which the inline confirmation says out loud.
  function startAnotherTake() {
    if (subjectApproved && !confirmAnotherTake) { setConfirmAnotherTake(true); return }
    setConfirmAnotherTake(false); setLastGenerateArgs(null); setFileError(null); setNewSubjectHint(true)
    // jsdom (unit tests) has no scrollIntoView on Element; a real browser always does.
    guidanceRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
    guidanceRef.current?.focus()
  }
  return <section className="workspace" aria-labelledby="subject-title">
    <header className="workspace-heading"><div><p className="eyebrow">1 · Subject</p><h2 id="subject-title">Create the character to animate</h2><p>Add one or more reference images. We’ll generate a clean, animation-ready subject.</p></div></header>
    {(fileError || generate.notice || usePrepared.notice || saveExternalSubject.notice || clearExternalSubject.notice || generate.error || usePrepared.error || saveExternalSubject.error || clearExternalSubject.error) && <p className="notice" role="status">{fileError || generate.notice || usePrepared.notice || saveExternalSubject.notice || clearExternalSubject.notice || (generate.error instanceof Error ? sentenceCase(generate.error.message) : usePrepared.error instanceof Error ? sentenceCase(usePrepared.error.message) : saveExternalSubject.error instanceof Error ? sentenceCase(saveExternalSubject.error.message) : clearExternalSubject.error instanceof Error ? sentenceCase(clearExternalSubject.error.message) : 'That subject action could not be completed. Your approved revision is unchanged.')}</p>}
    <div className={showGallery ? 'subject-layout' : 'subject-layout subject-layout--solo'}>
      {/* One card, top-aligned with the gallery beside it: references, guidance and Generate sit
          together so cause and effect are adjacent (operator, 2026-09-06). The gallery is the very
          next element, so stacking on a narrow viewport puts it right under Generate. */}
      <div className="subject-compose">
      {newSubjectHint && <p className="subject-takes__hint" role="status">Adjust the references or guidance, then Generate</p>}
      <label className={dragging ? 'upload-panel upload-panel--references upload-panel--dragging' : 'upload-panel upload-panel--references'} onDragEnter={onDragOverReferences} onDragOver={onDragOverReferences} onDragLeave={() => setDragging(false)} onDrop={onDropReferences}><span><strong>Drop reference images here</strong><small>PNG, JPEG, or WebP · 1–6 images · up to 20 MB each</small></span><input aria-label="Choose subject references" accept="image/png,image/jpeg,image/webp" multiple ref={referenceInputRef} type="file" onChange={(event) => addFiles(Array.from(event.target.files ?? []))} /></label>
      {references.length > 0 && <div className="reference-strip" aria-label="Subject references">{references.map((file, index) => <article className="reference-card" key={`${file.name}-${index}`}><img src={previews[index]} alt={`Reference ${index + 1}: ${file.name}`} /><span>{index + 1}</span><div><button type="button" disabled={index === 0} onClick={() => setReferences((items) => items.map((item, itemIndex) => itemIndex === index - 1 ? items[index] : itemIndex === index ? items[index - 1] : item))}>Move earlier</button><button type="button" onClick={() => setReferences((items) => items.filter((_, itemIndex) => itemIndex !== index))}>Remove</button></div></article>)}</div>}
      {/* No placeholder that suggests preserving things: an empty field already means "match the
          reference", which is what `subject_prompt` builds (plugins/subject_pool.py). */}
      <div className="direction-field">
        <label htmlFor="subject-guidance">Guidance <span>(optional)</span></label>
        <small>Leave empty to match the reference. Add a note only if something must change, e.g. “no helmet”.</small>
        <textarea id="subject-guidance" maxLength={500} ref={guidanceRef} value={direction} onChange={(event) => setDirection(event.target.value)} />
      </div>
      {/* The button doubles as the progress indicator while a job is in flight: a spinner plus
          "Generating…" (then "Generating… N s" past ten seconds), rather than a separate status
          line underneath (BOU-3208 operator feedback). */}
      <div className="subject-compose__actions">
        <Button variant="primary" disabled={!references.length || generate.isPending || Boolean(active)} onClick={startGeneration}>{active ? <><span className="button-spinner" aria-hidden="true" /><span role="status">{generatingLabel(elapsedSeconds)}</span></> : subject.current_revision ? 'Regenerate subject' : 'Generate subject'}</Button>
        {revisions.length > 0 && <Button onClick={startAnotherTake}>Start another take</Button>}
      </div>
      {confirmAnotherTake && <div className="subject-compose__confirm" role="status"><p>This starts a new take; the current subject stays until you choose the new one.</p><div className="subject-take-card__actions"><Button variant="primary" onClick={startAnotherTake}>Yes, start another take</Button><Button onClick={() => setConfirmAnotherTake(false)}>Cancel</Button></div></div>}
      {subjectFailure && <div className="operation-failure" role="alert"><p>{subjectFailure.failure_explanation ?? 'Subject generation failed.'}</p>{lastGenerateArgs && <Button onClick={retryGeneration} disabled={retryGenerate.isPending}>Retry</Button>}</div>}
      </div>
    {showGallery && <SubjectTakesGallery projectId={projectId} project={project.data} activeGeneration={Boolean(active)} elapsedSeconds={elapsedSeconds} onCorrectSubmit={submitCorrection} correcting={correctSubject.isPending} onApproved={continueToMotions} />}
    <div className="subject-extras">
      <button className="disclosure" type="button" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>Advanced</button>{advanced && <div className="advanced-panel"><h3>Use a prepared cutout</h3><p>If you already have a transparent PNG, use it directly instead of generation.</p><label>Prepared transparent PNG<input aria-label="Choose prepared cutout" accept="image/png" type="file" onChange={(event) => { const file = event.target.files?.[0] ?? null; if (file && file.type !== 'image/png') { setFileError('Prepared cutouts must be PNG files.'); setPrepared(null); return } setFileError(null); setPrepared(file) }} /></label><Button disabled={!prepared || usePrepared.isPending} onClick={() => usePrepared.mutate()}>Use prepared cutout</Button>
        <h3>External subject</h3><p>Bind this project to an entity in another game so preset prompts use its name and a release export can bind sheets to it.</p>
        <label>System<select aria-label="External subject system" value={system} onChange={(event) => { setSystem(event.target.value); setEntityKind(targets.data?.find((item) => item.system === event.target.value)?.entity_kinds[0] ?? '') }}>{targets.data?.map((item) => <option key={item.system} value={item.system}>{item.display_name}</option>)}</select></label>
        <label>Entity kind<select aria-label="External subject entity kind" value={entityKind} onChange={(event) => setEntityKind(event.target.value)}>{selectedTarget?.entity_kinds.map((kind) => <option key={kind} value={kind}>{kind.replaceAll('_', ' ')}</option>)}</select></label>
        <label>Entity id<input aria-label="External subject entity id" value={entityId} onChange={(event) => setEntityId(event.target.value)} placeholder="char-42" /></label>
        <label>Display name<input aria-label="External subject display name" value={displayName} onChange={(event) => setDisplayName(event.target.value)} placeholder="Ser Ember" /></label>
        <Button disabled={!system || !entityKind || !entityId.trim() || !displayName.trim() || saveExternalSubject.isPending} onClick={() => saveExternalSubject.mutate()}>{externalSubject ? 'Update external subject' : 'Bind external subject'}</Button>
        {externalSubject && <Button disabled={clearExternalSubject.isPending} onClick={() => clearExternalSubject.mutate()}>Clear external subject</Button>}
      </div>}
      <SubjectLog projectId={projectId} onTryAgain={startGeneration} />
    </div>
    </div>
  </section>
}
function WorkspaceState({ label }: { label: string }) { return <section className="workspace-state" role="status">{label}</section> }

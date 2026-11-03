import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError, studioApi } from '../../api/client'
import { projectKey } from '../../api/queries'
import type { Heading, ProjectResponse, ProjectSetup, SetupPlan, SetupView } from '../../api/types'
import { SetupChoices } from './SetupChoices'
import { TurnaroundPanel } from '../turnarounds/TurnaroundPanel'
import { useViewDrafts } from '../turnarounds/turnaroundQueries'
import { DIRECTIONS, headings, TEMPLATES, type Template } from './setupModel'
import './setup.css'
export function WorkspaceSetup({ project, selectedViewId, onSelect, onBusyChange }: { project: ProjectResponse; selectedViewId: string | null; onSelect: (id: string | null) => void; onBusyChange: (busy: boolean) => void }) {
  const dialogRef = useRef<HTMLDivElement>(null); const returnFocus = useRef<HTMLElement | null>(null); const client = useQueryClient(); const [draft, setDraft] = useState<ProjectSetup | null>(null); const [revision, setRevision] = useState(0); const [plan, setPlan] = useState<SetupPlan | null>(null); const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [original, setOriginal] = useState<ProjectSetup | null>(null); const [conventionChange, setConventionChange] = useState<{ desired: ProjectSetup; incompatible: SetupView[] } | null>(null); const [collision, setCollision] = useState<{ view: SetupView; heading: Heading; target: SetupView } | null>(null); const [stoppedDispatches, setStoppedDispatches] = useState<Record<string, boolean>>({}); const [newHeading, setNewHeading] = useState<Heading | ''>(''); const [template, setTemplate] = useState<Template>('single')
  const views = project.manifest.views ?? []
  // Onboarding's "Generate the other directions" arrives with ?directions=generate.
  const [turnaroundOpen, setTurnaroundOpen] = useState(() => new URLSearchParams(window.location.search).get('directions') === 'generate')
  useEffect(() => {
    const url = new URL(window.location.href)
    if (!url.searchParams.has('directions')) return
    url.searchParams.delete('directions'); window.history.replaceState(window.history.state, '', url)
  }, [])
  const viewDrafts = useViewDrafts(project.project_id)
  const draftReady = (heading: string | null | undefined) => (viewDrafts.data ?? []).some((item) => item.heading === heading && item.bindable)
  function viewLabel(view: typeof views[number]) {
    if (!view.heading) return view.name
    const direction = DIRECTIONS.find((item) => item.heading === view.heading)
    return `${view.name} · ${direction?.arrow ?? ''} ${view.heading}`
  }
  async function perform(action: () => Promise<void>) { setBusy(true); onBusyChange(true); setError(''); try { await action() } catch (reason) { if (reason instanceof ApiError && reason.status === 409) { setPlan(null); const current = await studioApi.setup(project.project_id); setRevision(current.revision); setOriginal(current.setup); setError('This workspace changed in another tab. Your choices are kept; review them against the latest workspace before confirming.') } else setError(reason instanceof Error ? reason.message : 'Could not save setup.') } finally { setBusy(false); onBusyChange(false) } }
  const editing = draft !== null
  useEffect(() => {
    if (!editing) return
    dialogRef.current?.focus()
    function keydown(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) { setDraft(null); setPlan(null); return }
      if (event.key !== 'Tab' || !dialogRef.current) return
      const items = Array.from(dialogRef.current.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),[href]')); const first = items[0]; const last = items.at(-1)
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', keydown)
    return () => { document.removeEventListener('keydown', keydown); returnFocus.current?.focus() }
  }, [editing, busy])
  async function open() { returnFocus.current = document.activeElement as HTMLElement; await perform(async () => { const current = await studioApi.setup(project.project_id); setDraft(current.setup); setOriginal(current.setup); setRevision(current.revision); setPlan(null); setConventionChange(null); setCollision(null) }) }
  async function apply(next: SetupPlan) { await perform(async () => { const result = await studioApi.applySetup(project.project_id, next); client.setQueryData(projectKey(project.project_id), result); const remaining = result.manifest.views ?? []; onSelect(remaining.some((view) => view.view_id === selectedViewId) ? selectedViewId : remaining[0]?.view_id ?? null); setDraft(null); setPlan(null) }) }
  async function preview() { if (!draft) return; await perform(async () => { const next = await studioApi.previewSetup(project.project_id, revision, draft); setPlan(next) }) }
  async function abandonDispatch(operationId: string) {
    if (!plan || !stoppedDispatches[operationId]) return
    const desired = plan.desired_setup
    await perform(async () => {
      const result = await studioApi.abandonSetupDispatch(project.project_id, operationId, plan.expected_revision)
      client.setQueryData(projectKey(project.project_id), result); setRevision(result.revision)
      const next = await studioApi.previewSetup(project.project_id, result.revision, desired); setPlan(next)
      setStoppedDispatches((current) => ({ ...current, [operationId]: false }))
    })
  }
  async function removeItem(next: ProjectSetup) {
    if (!original || JSON.stringify(draft) !== JSON.stringify(original)) { setDraft(next); setPlan(null); return }
    await perform(async () => {
      const preview = await studioApi.previewSetup(project.project_id, revision, next)
      setDraft(next)
      if (!preview.blocked_operations.length && preview.impacts.every((impact) => impact.requires_confirmation === false)) {
        const result = await studioApi.applySetup(project.project_id, preview); client.setQueryData(projectKey(project.project_id), result)
        const remaining = result.manifest.views ?? []; onSelect(remaining.some((view) => view.view_id === selectedViewId) ? selectedViewId : remaining[0]?.view_id ?? null)
        const current = await studioApi.setup(project.project_id); setDraft(current.setup); setOriginal(current.setup); setRevision(current.revision); setPlan(null)
      } else setPlan(preview)
    })
  }
  function requestConvention(desired: ProjectSetup) {
    if (busy) return
    const incompatible = desired.views.filter((view) => desired.sprite_layout === 'single_view' ? view.heading !== null || view.view_id !== 'single' : desired.sprite_layout === 'side_view' ? !['left', 'right'].includes(view.heading ?? '') : desired.sprite_layout === 'isometric' ? !['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'].includes(view.heading ?? '') : false)
    setPlan(null)
    if (incompatible.length) setConventionChange({ desired, incompatible })
    else { setDraft(desired); setConventionChange(null) }
  }
  function update(view: SetupView) { if (draft) { setDraft({ ...draft, views: draft.views.map((item) => item.view_id === view.view_id ? view : item) }); setPlan(null) } }
  async function upload(file: File, view: SetupView) { await perform(async () => {
    const reference = await studioApi.stageSetupReference(project.project_id, file)
    setDraft((current) => current ? { ...current, views: current.views.map((item) => item.view_id === view.view_id ? { ...item, reference: { revision_id: reference.revision_id } } : item) } : current); setPlan(null)
  }) }
  return <section className="character-views" aria-label="Character directions"><div className="character-views__navigation">{project.manifest.motions.some((motion) => !motion.view_id) && <button className="button" aria-pressed={selectedViewId === null} onClick={() => onSelect(null)}>Original / unassigned</button>}{views.map((view) => <button className="button" aria-pressed={selectedViewId === view.view_id} key={view.view_id} onClick={() => onSelect(view.view_id)}>{viewLabel(view)} · {view.reference ? `${project.manifest.motions.filter((motion) => motion.view_id === view.view_id).length} animations` : draftReady(view.heading) ? 'Draft ready' : project.manifest.motions.some((motion) => motion.view_id === view.view_id) ? 'Reference needed' : 'No animations'}</button>)}</div>
    <div className="setup-toolbar"><button className="button" disabled={busy} onClick={() => void open()}>Add direction</button><button className="button" disabled={busy} onClick={() => void open()}>Add animation</button><button className="button" disabled={busy} onClick={() => void open()}>Edit artwork & directions</button><button className="button" disabled={busy} onClick={() => void open()}>Change template / game view</button><button className="button" aria-expanded={turnaroundOpen} onClick={() => setTurnaroundOpen((current) => !current)}>Generate other directions</button></div>
    {turnaroundOpen && <TurnaroundPanel project={project} onClose={() => setTurnaroundOpen(false)} />}
    {error && !draft && <p role="alert">{error}</p>}
    {draft && <div className="setup-editor" ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label="Edit workspace setup"><header><h2>Your artwork & animation request</h2><button className="button" disabled={busy} onClick={() => { setDraft(null); setPlan(null) }}>Close</button></header>{error && <p role="alert">{error}</p>}<p>Keep prior choices, add only what you need, then review the exact impact before saving. Published history remains available.</p>
      {conventionChange && <div className="setup-note" role="region" aria-label="Resolve incompatible directions"><h3>Review incompatible directions</h3><p>{conventionChange.incompatible.length} directions need an explicit heading or removal for this game view: {conventionChange.incompatible.map((view) => view.name).join(', ')}. Artwork is never mirrored or mapped to a different heading automatically.</p><p>Keeping directions preserves their artwork and history. Assign each a compatible heading before review. Staging removal keeps them active until you confirm the exact server impact.</p><button className="button" onClick={() => setConventionChange(null)}>Cancel game-view change</button><button className="button" onClick={() => { setDraft(conventionChange.desired); setConventionChange(null) }}>Keep directions and assign headings manually</button><button className="button" onClick={() => { const removed = new Set(conventionChange.incompatible.map((view) => view.view_id)); setDraft({ ...conventionChange.desired, views: conventionChange.desired.views.filter((view) => !removed.has(view.view_id)) }); setConventionChange(null) }}>Stage removal of incompatible directions</button></div>}
      {collision && <div className="setup-note" role="alert"><h3>Replace {collision.target.name}?</h3><p>This removes {collision.target.name} and its {collision.target.motions.length} animations from the active request. The next review shows exact takes and revisions affected.</p><button className="button" onClick={() => setCollision(null)}>Cancel replacement</button><button className="button" onClick={() => { setDraft({ ...draft, views: draft.views.filter((view) => view.view_id !== collision.target.view_id).map((view) => view.view_id === collision.view.view_id ? { ...view, heading: collision.heading } : view) }); setCollision(null); setPlan(null) }}>Replace direction and review impact</button></div>}
      {!plan && !conventionChange ? <><label>Game view<select disabled={busy} value={draft.sprite_layout} onChange={(event) => requestConvention({ ...draft, sprite_layout: event.target.value as ProjectSetup['sprite_layout'] })}><option value="single_view">Single direction · original view</option><option value="side_view">Side-scrolling</option><option value="isometric">Compass directions</option></select></label>
      <div className="setup-template-edit"><label>Add one direction<select value={newHeading} onChange={(event) => setNewHeading(event.target.value as Heading)}><option value="">Choose an explicit heading</option>{DIRECTIONS.filter((item) => (draft.sprite_layout === 'side_view' ? ['left', 'right'].includes(item.heading) : draft.sprite_layout === 'isometric' ? !['left', 'right'].includes(item.heading) : false) && !draft.views.some((view) => view.heading === item.heading)).map((item) => <option value={item.heading} key={item.heading}>{item.arrow} {item.label}</option>)}</select></label><button className="button" disabled={!newHeading} onClick={() => { const option = DIRECTIONS.find((item) => item.heading === newHeading); if (option) setDraft({ ...draft, views: [...draft.views, { view_id: `view-${crypto.randomUUID()}`, heading: option.heading, name: option.label, reference: null, motions: [] }] }); setNewHeading('') }}>Add selected direction</button></div>
      <div className="setup-template-edit"><label>Direction suggestions<select value={template} onChange={(event) => setTemplate(event.target.value as Template)}>{TEMPLATES.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label><button className="button" disabled={busy} onClick={() => { const options = template === 'single' ? [{ heading: null, label: 'Original view' }] : headings(template); const additions = options.filter((item) => !draft.views.some((view) => view.heading === item.heading || (item.heading === null && view.view_id === 'single'))); requestConvention({ ...draft, sprite_layout: template === 'single' ? 'single_view' : template === 'side' ? 'side_view' : 'isometric', views: [...draft.views, ...additions.map((item) => ({ view_id: item.heading === null ? 'single' : `view-${crypto.randomUUID()}`, heading: item.heading, name: item.label, reference: null, motions: [] }))] }) }}>Add suggested directions</button></div>
      {draft.views.map((view) => <div className="setup-view-edit" key={view.view_id}><h3>{view.name}</h3><label>Direction name<input value={view.name} onChange={(event) => update({ ...view, name: event.target.value })} /></label><label>Change direction<select value={view.heading ?? ''} onChange={(event) => { const heading = event.target.value ? event.target.value as Heading : null; const target = draft.views.find((item) => item.view_id !== view.view_id && item.heading === heading); if (target && heading) setCollision({ view, heading, target }); else update({ ...view, heading }) }}><option value="">Original view · no heading</option>{DIRECTIONS.filter((item) => draft.sprite_layout === 'side_view' ? ['left', 'right'].includes(item.heading) : !['left', 'right'].includes(item.heading)).map((item) => <option key={item.heading} value={item.heading}>{item.arrow} {item.label}</option>)}</select></label><label className="button setup-upload">Replace art<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={(event) => { const file = event.target.files?.[0]; if (file) void upload(file, view) }} /></label><button className="button" onClick={() => update({ ...view, reference: null })}>Clear reference</button><button className="button" onClick={() => void removeItem({ ...draft, views: draft.views.filter((item) => item.view_id !== view.view_id) })}>Remove direction · {view.name}</button>{view.motions.map((motion) => <button className="button" key={motion.motion_id} onClick={() => void removeItem({ ...draft, views: draft.views.map((item) => item.view_id === view.view_id ? { ...view, motions: view.motions.filter((item) => item.motion_id !== motion.motion_id) } : item) })}>Remove animation · {motion.name}</button>)}</div>)}
      <SetupChoices original={original} setup={draft} onChange={(next) => { setDraft(next); setPlan(null) }} /><button className="button button--primary" disabled={busy || !!conventionChange} onClick={() => void preview()}>Review changes</button></> : plan ? <div className="setup-impact"><h3>Review exact changes</h3>{plan.impacts.length ? <ul>{plan.impacts.map((impact) => <li key={impact.impact_id}><strong>{impact.name}</strong> · {impact.kind.replaceAll('_', ' ')} · {impact.candidate_count} takes · {impact.revision_count} revisions{impact.reference_count ? ` · ${impact.reference_count} references` : ''} · {impact.approval_count ?? 0} approvals</li>)}</ul> : <p>No destructive impacts.</p>}{plan.blocked_operations.length > 0 && <p role="alert">Blocked by active operations: {plan.blocked_operations.join(', ')}. Wait until they settle, then review again.</p>}{plan.blocked_operations.filter((item) => /^dispatch:[^:]+:[^:]+:recovery_required$/.test(item)).map((item) => { const operationId = item.split(':')[1]; return <div className="setup-note" key={item}><h4>Recover an interrupted dispatch</h4><p>Only release this reservation after verifying that dispatch {operationId} has stopped. This fences late results and does not resubmit generation. Running or completed jobs cannot be abandoned here.</p><label><input type="checkbox" checked={!!stoppedDispatches[operationId]} onChange={(event) => setStoppedDispatches((current) => ({ ...current, [operationId]: event.target.checked }))} />I verified this dispatch has stopped</label><button className="button" disabled={busy || !stoppedDispatches[operationId]} onClick={() => void abandonDispatch(operationId)}>Release stopped dispatch reservation</button></div> })}<p>A changed heading keeps history; old takes depict the old assignment. Replacement never merges histories.</p><button className="button" onClick={() => setPlan(null)}>Back to choices / cancel replacement</button><button className="button button--primary" disabled={busy || !!plan.blocked_operations.length} onClick={() => void apply(plan)}>Confirm exact changes</button></div> : null}
    </div>}
  </section>
}

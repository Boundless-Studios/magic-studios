import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, studioApi } from '../../api/client'
import { projectKey, wakeOperationPollers } from '../../api/queries'
import type { CompassHeading, EquipmentAttachment, PickProblem, ProjectResponse, SetupPlan, Turnaround, TurnaroundEquipment, TurnaroundPickEntry, ViewDraft } from '../../api/types'
import { characterSide, HEADING_LABEL, pickHeadings, turnaroundEligibility } from './turnaroundModel'
import { useViewDrafts } from './turnaroundQueries'
import './turnaround.css'

const ATTACHMENTS: EquipmentAttachment[] = ['hand', 'shoulder', 'hip', 'back', 'other']
const message = (reason: unknown, fallback: string) => reason instanceof Error ? reason.message : fallback

/** Generate other directions from one image: turn the S reference, pick frames, check drafts, bind them. */
export function TurnaroundPanel({ project, onClose }: { project: ProjectResponse; onClose: () => void }) {
  const id = project.project_id
  useEffect(() => {
    function keydown(event: KeyboardEvent) { if (event.key === 'Escape') onClose() }
    document.addEventListener('keydown', keydown)
    return () => document.removeEventListener('keydown', keydown)
  }, [onClose])
  const config = useQuery({ queryKey: ['generation-config'], queryFn: studioApi.getGenerationConfig })
  const turnarounds = useQuery({
    queryKey: ['turnarounds', id], queryFn: () => studioApi.turnarounds(id).then((page) => page.turnarounds),
    // A completed job settles a moment later, so keep polling until it has.
    refetchInterval: (query) => (query.state.data ?? []).some((take) => !take.settled && !['failed', 'cancelled'].includes(take.status ?? '')) ? 3000 : false,
  })
  const eligibility = turnaroundEligibility(project, config.data)
  const takes = turnarounds.data ?? []
  // The newest take unless the person chose an earlier one.
  const [chosen, setChosen] = useState<string | null>(null)
  const shown = takes.find((take) => take.turnaround_id === chosen) ?? takes.at(-1)
  return <div className="turnaround-overlay"><section className="turnaround-panel" role="dialog" aria-modal="true" aria-label="Generate other directions">
    <header className="turnaround-panel__header"><h2>Generate other directions</h2><button className="button" onClick={onClose}>Close</button></header>
    <p>Studio turns your front or three-quarter artwork in place in one short video. You pick a frame for each direction and check each draft yourself before it becomes that direction&apos;s reference. Nothing is mirrored or bound automatically.</p>
    {eligibility.ok ? <StartForm project={project} sourceViewId={eligibility.sourceViewId} sourceRevisionId={eligibility.sourceRevisionId} onStarted={() => setChosen(null)} /> : <p role="note" className="turnaround-panel__reason">{config.isLoading ? 'Checking machines…' : eligibility.reason}</p>}
    {takes.length > 1 && shown && <label className="turnaround-panel__take">Turnaround<select aria-label="Turnaround take" value={shown.turnaround_id} onChange={(event) => setChosen(event.target.value)}>{takes.map((take, index) => <option key={take.turnaround_id} value={take.turnaround_id}>{`Take ${index + 1}${take.settled ? '' : ` · ${take.status ?? 'pending'}`}${index === takes.length - 1 ? ' (newest)' : ''}`}</option>)}</select></label>}
    {shown && <TakeStatus key={shown.turnaround_id} project={project} take={shown} />}
  </section></div>
}

function StartForm({ project, sourceViewId, sourceRevisionId, onStarted }: { project: ProjectResponse; sourceViewId: string; sourceRevisionId: string; onStarted: () => void }) {
  const client = useQueryClient()
  const [rows, setRows] = useState<TurnaroundEquipment[]>([])
  const [busy, setBusy] = useState(false); const [error, setError] = useState('')
  const sided = (row: TurnaroundEquipment) => ['hand', 'shoulder', 'hip'].includes(row.attachment)
  const valid = rows.every((row) => row.item.trim() && (!sided(row) || row.source_picture_side !== 'unsided'))
  function update(index: number, change: Partial<TurnaroundEquipment>) {
    setRows((current) => current.map((row, at) => {
      if (at !== index) return row
      const next = { ...row, ...change }
      return sided(next) && next.source_picture_side === 'unsided' ? { ...next, source_picture_side: 'left' } : next
    }))
  }
  async function start() {
    setBusy(true); setError('')
    try {
      await studioApi.startTurnaround(project.project_id, project.revision, sourceViewId, sourceRevisionId, rows.map((row) => ({ ...row, item: row.item.trim() })))
      onStarted()
      await Promise.all([client.invalidateQueries({ queryKey: ['turnarounds', project.project_id] }), client.invalidateQueries({ queryKey: projectKey(project.project_id) }), wakeOperationPollers(client, project.project_id)])
    } catch (reason) { setError(message(reason, 'Could not start the turnaround.')) } finally { setBusy(false) }
  }
  return <div className="turnaround-panel__start">
    <h3>One-sided equipment</h3>
    <p>Optional. Name anything held or worn on one side so every direction keeps it there, for example an axe in one hand.</p>
    {rows.map((row, index) => <fieldset key={index} className="turnaround-panel__row"><legend>Item {index + 1}</legend>
      <label>Item<input aria-label={`Item ${index + 1} name`} maxLength={40} value={row.item} onChange={(event) => update(index, { item: event.target.value })} /></label>
      <label>Attached to<select aria-label={`Item ${index + 1} attachment`} value={row.attachment} onChange={(event) => update(index, { attachment: event.target.value as EquipmentAttachment })}>{ATTACHMENTS.map((value) => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>Side of the picture<select aria-label={`Item ${index + 1} picture side`} value={row.source_picture_side} onChange={(event) => update(index, { source_picture_side: event.target.value as TurnaroundEquipment['source_picture_side'] })}><option value="left">left</option><option value="right">right</option>{!sided(row) && <option value="unsided">neither (centred)</option>}</select></label>
      {row.item.trim() && <small>{characterSide(row)}</small>}
      <button className="button" type="button" onClick={() => setRows((current) => current.filter((_, at) => at !== index))}>Remove item {index + 1}</button>
    </fieldset>)}
    {rows.length < 4 && <button className="button" type="button" onClick={() => setRows((current) => [...current, { item: '', attachment: 'hand', source_picture_side: 'left' }])}>Add equipment</button>}
    <button className="button button--primary" type="button" disabled={busy || !valid} onClick={() => void start()}>Generate</button>
    {error && <p role="alert">{error}</p>}
  </div>
}

function TakeStatus({ project, take }: { project: ProjectResponse; take: Turnaround }) {
  if (!take.settled) return <p role="status">{take.status === 'failed' ? 'The turnaround failed. Generate again to retry.' : take.status === 'cancelled' ? 'The turnaround was cancelled.' : 'Turning your character… this takes about a minute on the machine.'}</p>
  return <PickEditor project={project} turnaroundId={take.turnaround_id} />
}

function PickEditor({ project, turnaroundId }: { project: ProjectResponse; turnaroundId: string }) {
  const id = project.project_id
  const client = useQueryClient()
  const detail = useQuery({ queryKey: ['turnaround', id, turnaroundId], queryFn: () => studioApi.turnaround(id, turnaroundId) })
  const frames = useQuery({
    queryKey: ['turnaround-frames', id, turnaroundId], enabled: Boolean(detail.data?.frame_count),
    queryFn: async () => {
      const count = detail.data?.frame_count ?? 0; const pages = []
      for (let start = 0; start < count; start += 32) pages.push(studioApi.turnaroundFrames(id, turnaroundId, start, 32))
      return (await Promise.all(pages)).flatMap((page) => page.frames)
    },
  })
  const take = detail.data
  const headings = take ? pickHeadings(take.source_heading) : []
  const [picks, setPicks] = useState<Partial<Record<CompassHeading, TurnaroundPickEntry>>>({})
  const [problems, setProblems] = useState<PickProblem[]>([])
  const [busy, setBusy] = useState(false); const [error, setError] = useState('')
  useEffect(() => {
    if (!take) return
    const saved = take.pick_set?.picks
    setPicks(Object.fromEntries(pickHeadings(take.source_heading).map((heading) => [heading, saved?.[heading] ?? { frame_index: take.suggested_picks?.[heading]?.frame ?? 0 }])))
  }, [take])
  if (!take) return <p role="status">Loading the turnaround…</p>
  const count = take.frame_count ?? 0
  const saved = take.pick_set?.picks
  const unsaved = !saved || headings.some((heading) => picks[heading]?.frame_index !== saved[heading]?.frame_index)
  async function save() {
    setBusy(true); setError(''); setProblems([])
    try {
      await studioApi.saveTurnaroundPicks(id, turnaroundId, take?.pick_set?.pick_revision ?? 0, picks)
      // Saving moves the project revision and can make earlier drafts stale.
      await Promise.all([client.invalidateQueries({ queryKey: ['turnaround', id, turnaroundId] }), client.invalidateQueries({ queryKey: projectKey(id) }), client.invalidateQueries({ queryKey: ['view-drafts', id] })])
    } catch (reason) {
      const detail = reason instanceof ApiError ? reason.detail as { detail?: { problems?: PickProblem[] } } : null
      if (detail?.detail?.problems) setProblems(detail.detail.problems)
      setError(reason instanceof ApiError && reason.status === 409 ? 'The picks changed elsewhere. They have been reloaded; review them and save again.' : message(reason, 'Could not save picks.'))
      if (reason instanceof ApiError && reason.status === 409) await client.invalidateQueries({ queryKey: ['turnaround', id, turnaroundId] })
    } finally { setBusy(false) }
  }
  return <div className="turnaround-panel__picks">
    <h3>Pick a frame for each direction</h3>
    {take.suggestions_error && <p role="note">Studio could not suggest frames ({take.suggestions_error}). Pick them by hand.</p>}
    <div className="turnaround-panel__slots">{headings.map((heading) => {
      const pick = picks[heading] ?? { frame_index: 0 }
      const frame = frames.data?.[pick.frame_index]
      const problem = problems.filter((item) => item.heading === heading)
      return <figure key={heading} className="turnaround-panel__slot" aria-label={`${HEADING_LABEL[heading]} (${heading})`}>
        {frame ? <img src={frame.image} alt={`${heading} frame ${pick.frame_index}`} /> : <span className="turnaround-panel__empty">Frame {pick.frame_index}</span>}
        <figcaption><strong>{heading} · {HEADING_LABEL[heading]}</strong>
          <label>Frame<input type="range" aria-label={`${heading} frame`} min={0} max={Math.max(0, count - 1)} value={pick.frame_index} onChange={(event) => { const frame = Number(event.target.value); setPicks((current) => ({ ...current, [heading]: { frame_index: frame } })); setProblems((current) => current.filter((item) => item.heading !== heading)) }} /> {pick.frame_index}</label>
          {take.suggested_picks?.[heading] && <small>Suggested frame {take.suggested_picks[heading]?.frame}: {take.suggested_picks[heading]?.reason}.</small>}
          <p className="turnaround-panel__card">{(take.placement_cards[heading] ?? []).join(' ')}</p>
          {problem.map((item) => <label key={item.kind} className="turnaround-panel__confirm"><input type="checkbox" checked={Boolean(item.kind === 'out_of_order' ? pick.confirm_out_of_order : pick.confirm_out_of_region)} onChange={(event) => setPicks((current) => ({ ...current, [heading]: { ...pick, [item.kind === 'out_of_order' ? 'confirm_out_of_order' : 'confirm_out_of_region']: event.target.checked } }))} />{item.message}</label>)}
        </figcaption>
      </figure>
    })}</div>
    <button className="button button--primary" disabled={busy || frames.isLoading} onClick={() => void save()}>Save picks</button>
    {take.pick_set && <small> Saved pick set {take.pick_set.pick_revision}.</small>}
    {error && <p role="alert">{error}</p>}
    {take.pick_set && <Drafts project={project} take={take} unsaved={unsaved} />}
  </div>
}

function Drafts({ project, take, unsaved }: { project: ProjectResponse; take: Turnaround; unsaved: boolean }) {
  const drafts = useViewDrafts(project.project_id)
  const client = useQueryClient()
  const [error, setError] = useState(''); const [busy, setBusy] = useState(false)
  const pickRevision = take.pick_set?.pick_revision ?? 0
  async function create(heading: CompassHeading) {
    setBusy(true); setError('')
    try { await studioApi.createViewDraft(project.project_id, project.revision, take.turnaround_id, pickRevision, heading); await refresh() }
    catch (reason) {
      setError(reason instanceof ApiError && reason.status === 409 ? 'The project changed. It has been reloaded; try again.' : message(reason, 'Could not make the draft.'))
      if (reason instanceof ApiError && reason.status === 409) await refresh()
    } finally { setBusy(false) }
  }
  async function refresh() { await Promise.all([client.invalidateQueries({ queryKey: ['view-drafts', project.project_id] }), client.invalidateQueries({ queryKey: projectKey(project.project_id) })]) }
  return <div className="turnaround-panel__drafts">
    <h3>Drafts</h3>
    <p>Each draft uses your picked frame as it is. A draft never counts as artwork until you check it and use it as that direction&apos;s reference.</p>
    <p className="setup-note">Standing versions: coming later.</p>
    {unsaved && <p role="note">Save your picks first: a draft uses the saved frame, not the one you are moving.</p>}
    {pickHeadings(take.source_heading).map((heading) => {
      const current = (drafts.data ?? []).filter((draft) => draft.turnaround_id === take.turnaround_id && draft.heading === heading && !draft.discarded)
      return <div key={heading} className="turnaround-panel__draft-row">
        <strong>{heading} · {HEADING_LABEL[heading]}</strong>
        <button className="button" disabled={busy || unsaved} onClick={() => void create(heading)}>Use this frame</button>
        {current.map((draft) => <DraftTile key={draft.draft_id} project={project} draft={draft} onChange={refresh} />)}
      </div>
    })}
    {error && <p role="alert">{error}</p>}
  </div>
}

function DraftTile({ project, draft, onChange }: { project: ProjectResponse; draft: ViewDraft; onChange: () => Promise<void> }) {
  const client = useQueryClient()
  const [boxes, setBoxes] = useState({ facing: false, equipment: false, identity: false })
  const [plan, setPlan] = useState<SetupPlan | null>(null)
  const [busy, setBusy] = useState(false); const [error, setError] = useState('')
  const [title, ...placements] = draft.placement_card
  async function run(action: () => Promise<void>, fallback: string) { setBusy(true); setError(''); try { await action() } catch (reason) { setError(message(reason, fallback)) } finally { setBusy(false) } }
  const check = () => run(async () => { await studioApi.checkViewDraft(project.project_id, draft.draft_id, project.revision, boxes); await onChange() }, 'Could not save the check.')
  const discard = () => run(async () => { await studioApi.discardViewDraft(project.project_id, draft.draft_id, project.revision); await onChange() }, 'Could not discard the draft.')
  const preview = () => run(async () => {
    const current = await studioApi.setup(project.project_id)
    const existing = current.setup.views.find((view) => view.heading === draft.heading)
    const views = existing
      ? current.setup.views.map((view) => view.view_id === existing.view_id ? { ...view, reference: { revision_id: draft.draft_id } } : view)
      : [...current.setup.views, { view_id: `view-${crypto.randomUUID()}`, heading: draft.heading, name: HEADING_LABEL[draft.heading], reference: { revision_id: draft.draft_id }, motions: [] }]
    setPlan(await studioApi.previewSetup(project.project_id, current.revision, { ...current.setup, views }))
  }, 'Could not prepare the change.')
  const apply = () => run(async () => {
    if (!plan) return
    const result = await studioApi.applySetup(project.project_id, plan); client.setQueryData(projectKey(project.project_id), result); setPlan(null); await onChange()
  }, 'Could not use the draft.')
  return <article className="turnaround-panel__tile" aria-label={`${draft.heading} draft`}>
    <img src={draft.image_path} alt={`${draft.heading} draft`} />
    <div>
      {draft.synthesized_parts.length > 0 && <p className="turnaround-panel__synthesized"><strong>Synthesized:</strong> {draft.synthesized_parts.join('; ')}. Your source never showed these, so they are invented.</p>}
      {draft.stale_reason && !draft.bound_view_ids.length && <p role="note" className="setup-note">This draft can no longer be used: {draft.stale_reason}.</p>}
      {draft.bound_view_ids.length > 0 ? <p role="status">Used as the {draft.heading} reference.</p> : draft.checked ? <>
        <p>Checked.</p>
        {!plan && draft.bindable && <button className="button button--primary" disabled={busy} onClick={() => void preview()}>Use as {draft.heading} reference</button>}
        {plan && <div className="setup-impact"><p>This will change:</p><ul>{plan.impacts.map((impact) => <li key={impact.impact_id}>{impact.name}{impact.kind === 'draft_source_superseded' ? ': made from a reference that has since been replaced' : impact.candidate_count ? `: ${impact.candidate_count} earlier takes become history` : ''}</li>)}</ul>
          {plan.blocked_operations.length > 0 ? <p role="alert">Wait for running work in this direction to finish first.</p> : <button className="button button--primary" disabled={busy} onClick={() => void apply()}>Confirm</button>}
          <button className="button" disabled={busy} onClick={() => setPlan(null)}>Cancel</button></div>}
      </> : <fieldset className="turnaround-panel__check"><legend>Check this draft</legend>
        <label><input type="checkbox" checked={boxes.facing} onChange={(event) => setBoxes({ ...boxes, facing: event.target.checked })} />Facing is correct: {title}</label>
        <label><input type="checkbox" checked={boxes.equipment} onChange={(event) => setBoxes({ ...boxes, equipment: event.target.checked })} />Equipment is where the card says{placements.length ? `: ${placements.join(' ')}` : ' (no one-sided items)'}</label>
        <label><input type="checkbox" checked={boxes.identity} onChange={(event) => setBoxes({ ...boxes, identity: event.target.checked })} />Same face and outfit wherever the source shows them, and the synthesized parts look plausible</label>
        <button className="button" disabled={busy} onClick={() => void check()}>Save check</button>
      </fieldset>}
      {!draft.bound_view_ids.length && <button className="button" disabled={busy} onClick={() => void discard()}>Discard</button>}
      {error && <p role="alert">{error}</p>}
    </div>
  </article>
}

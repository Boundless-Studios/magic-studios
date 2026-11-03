import { WorkspaceSetup } from '../setup/WorkspaceSetup'
import { useState } from 'react'
import { studioApi } from '../../api/client'
import { useAssetPreview, useProjectMutation } from '../../api/queries'
import type { CharacterView, ProjectResponse, SpriteLayout, ViewId } from '../../api/types'
import { Button } from '../../components/ui/Button'

import { viewOptions } from './characterViewModel'

function LegacyCharacterViews({ project, selectedViewId, onSelect, onBusyChange, disabled }: {
  project: ProjectResponse; selectedViewId: ViewId | null; onSelect: (id: ViewId | null) => void; onBusyChange: (busy: boolean) => void; disabled: boolean
}) {
  const layout = project.manifest.sprite_layout ?? 'unassigned'
  const views = project.manifest.views ?? []
  const selected = views.find((view) => view.view_id === selectedViewId)
  const [configure, setConfigure] = useState(layout === 'unassigned' || views.length === 0)
  const [error, setError] = useState<string | null>(null)
  const [nextViewId, setNextViewId] = useState<ViewId | ''>('')
  const saveLayout = useProjectMutation(project.project_id, (value: SpriteLayout) => studioApi.setSpriteLayout(project.project_id, project.revision, value))
  const saveView = useProjectMutation(project.project_id, ({ viewId, reference }: { viewId: ViewId; reference?: { key: string; sha256: string } | null }) => studioApi.updateView(project.project_id, viewId, project.revision, reference === undefined ? {} : { reference }))
  const [uploading, setUploading] = useState(false)
  const busy = disabled || uploading || saveLayout.isPending || saveView.isPending
  async function perform(action: () => Promise<unknown>) {
    onBusyChange(true); setError(null)
    try { await action() } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not save this view. Try again.') }
    finally { onBusyChange(false) }
  }
  async function upload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]; event.target.value = ''
    if (!file || !selected) return
    const viewId = selected.view_id
    await perform(async () => {
      setUploading(true)
      try {
        const target = await studioApi.createViewReferenceUpload(project.project_id, viewId, project.revision, file.type || 'image/png')
        await studioApi.upload(target, file)
        const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
        const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
        await saveView.mutateAsync({ viewId, reference: { key: target.key, sha256 } })
      } finally { setUploading(false) }
    })
  }
  const missing = viewOptions(layout).filter(({ id }) => !views.some((view) => view.view_id === id))
  const addId = missing.some(({ id }) => id === nextViewId) ? nextViewId : missing[0]?.id
  const subject = project.manifest.subject.current_revision
  return <section className="character-views" aria-label="Character views">
    <div className="character-views__navigation" role="group" aria-label="Select character view">
      <Button aria-pressed={selectedViewId === null} onClick={() => onSelect(null)} disabled={uploading || saveLayout.isPending || saveView.isPending}>Original / unassigned</Button>
      {views.map((view) => {
        const motions = project.manifest.motions.filter((motion) => motion.view_id === view.view_id)
        const current = motions.filter((motion) => motion.selected_candidate_id && motion.candidates.some((candidate) => candidate.candidate_id === motion.selected_candidate_id && (!motion.spec_digest || candidate.motion_spec_digest === motion.spec_digest))).length
        return <Button key={view.view_id} aria-pressed={selectedViewId === view.view_id} onClick={() => onSelect(view.view_id)} disabled={uploading || saveLayout.isPending || saveView.isPending}>{view.name} · {view.reference ? `${current}/${motions.length} selected` : 'Reference needed'}</Button>
      })}
    </div>
    <details className="character-views__configuration" open={configure} onToggle={(event) => setConfigure(event.currentTarget.open)}><summary>Configure views</summary>
    <div className="character-views__setup">
      <label>Sprite layout<select aria-label="Sprite layout" value={layout} disabled={busy} onChange={(event) => void perform(() => saveLayout.mutateAsync(event.target.value as SpriteLayout))}>
        <option value="unassigned">Unassigned / original art</option><option value="side_view">Side view · left / right</option><option value="isometric">Isometric · supplied compass views</option>
      </select></label>
      <p>Supply artwork for each required view. Studio preserves its facing and projection; it does not turn or mirror artwork into missing views.</p>
    </div>
      {missing.length > 0 && <div className="character-views__add"><label>Add view<select aria-label="Add view" value={addId ?? ''} disabled={busy} onChange={(event) => setNextViewId(event.target.value as ViewId)}>{missing.map(({ id, label }) => <option value={id} key={id}>{label}</option>)}</select></label><Button disabled={busy || !addId} onClick={() => { if (addId) void perform(async () => { await saveView.mutateAsync({ viewId: addId }); onSelect(addId) }) }}>Add view</Button></div>}
    </details>
    {selected && <div className="character-views__reference">
      <ViewPreview projectId={project.project_id} view={selected} />
      <div><h3>{selected.name} reference</h3><details open={!selected.reference}><summary>Reference settings</summary><p>{selected.reference ? 'This artwork is shared by every animation in this view. Replacing it keeps takes and selections, and marks earlier takes out of date.' : 'Upload matching facing artwork before generating animations in this view.'}</p>
        <label>{selected.reference ? 'Replace view reference' : 'Upload view reference'}<input aria-label="View reference image" type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={(event) => void upload(event)} /></label>
        {subject && <Button disabled={busy} onClick={() => void perform(() => saveView.mutateAsync({ viewId: selected.view_id, reference: { key: subject.artifact_key, sha256: subject.sha256 } }))}>Use original subject for this view</Button>}
        {selected.reference && <Button disabled={busy} onClick={() => void perform(() => saveView.mutateAsync({ viewId: selected.view_id, reference: null }))}>Clear view reference</Button>}
      </details></div>
    </div>}
    {error && <p role="alert">{error}</p>}
    {uploading && <p role="status">Uploading view reference…</p>}
  </section>
}
function ViewPreview({ projectId, view }: { projectId: string; view: CharacterView }) {
  const preview = useAssetPreview(projectId, view.reference?.key)
  return <div className="character-views__preview">{preview.data ? <img src={preview.data.upload_url} alt={`${view.name} reference`} /> : <span>{view.reference ? 'Loading reference' : 'No reference yet'}</span>}{preview.isError && <p role="alert">Reference preview unavailable.</p>}</div>
}

export function CharacterViews(props: Parameters<typeof LegacyCharacterViews>[0]) {
  return props.project.manifest.sprite_layout && props.project.manifest.sprite_layout !== 'unassigned' ? <WorkspaceSetup {...props} /> : <LegacyCharacterViews {...props} />
}

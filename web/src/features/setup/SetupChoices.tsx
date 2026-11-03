import type { MotionPreset, ProjectSetup, SetupMotion, SetupView } from '../../api/types'
import { MOTION_PRESETS } from '../motions/motionPresets'
import { motionFor } from './setupModel'
export function SetupChoices({ setup, onChange, removableDirections = false, original }: { setup: ProjectSetup; onChange: (setup: ProjectSetup) => void; removableDirections?: boolean; original?: ProjectSetup | null }) {
  function presetMotion(view: SetupView, preset: MotionPreset): SetupMotion {
    const existing = original?.views.find((item) => item.view_id === view.view_id)?.motions.find((item) => item.preset === preset)
    return existing ?? { ...motionFor(view.view_id, preset), motion_id: `motion-${crypto.randomUUID()}`, semantic_slot: preset }
  }
  function update(view: SetupView) { onChange({ ...setup, views: setup.views.map((item) => item.view_id === view.view_id ? view : item) }) }
  return <div className="setup-direction-list">{setup.views.map((view) => <fieldset className="setup-direction" key={view.view_id}>
    <legend>{view.name} <span>{view.reference ? 'Artwork supplied' : view.motions.length ? 'Reference needed' : 'No animations requested'}</span></legend>
    {removableDirections && <button type="button" className="button" onClick={() => onChange({ ...setup, views: setup.views.filter((item) => item.view_id !== view.view_id) })}>Remove direction · {view.name}</button>}
    <div className="setup-animation-grid">{MOTION_PRESETS.filter((item) => ['idle', 'walk', 'run', 'attack', 'hurt', 'death'].includes(item.preset)).map((item) => <label key={item.preset} className="setup-animation">
      <input type="checkbox" checked={view.motions.some((motion) => motion.preset === item.preset)} onChange={(event) => update({ ...view, motions: event.target.checked ? [...view.motions, presetMotion(view, item.preset)] : view.motions.filter((motion) => motion.preset !== item.preset) })} />
      <span aria-hidden="true" className={`setup-mini setup-mini--${item.preset}`}>●<i /></span><span>{item.displayName}<small>{item.description}</small></span>
    </label>)}</div>
    {view.motions.filter((motion) => motion.kind === 'custom').map((motion) => <label key={motion.motion_id}><input type="checkbox" checked onChange={() => update({ ...view, motions: view.motions.filter((item) => item.motion_id !== motion.motion_id) })} />{motion.name}</label>)}
    <form className="setup-custom" onSubmit={(event) => { event.preventDefault(); const form = event.currentTarget; const data = new FormData(form); const name = String(data.get('name')).trim(); const prompt = String(data.get('prompt')).trim(); if (!name || !prompt) return; const id = `motion-${crypto.randomUUID()}`; update({ ...view, motions: [...view.motions, { motion_id: id, name, kind: 'custom', prompt, loop_policy: 'once', required: true }] }); form.reset() }}><label className="setup-custom__field">Custom animation<input name="name" aria-label={`Custom animation name for ${view.name}`} placeholder="Animation name" /></label><label className="setup-custom__field">Action guidance<input name="prompt" aria-label={`Custom animation action for ${view.name}`} placeholder="What happens?" /></label><button type="submit" className="button">Add custom</button></form>
  </fieldset>)}</div>
}

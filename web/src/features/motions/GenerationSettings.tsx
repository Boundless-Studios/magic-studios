import { useEffect, useState } from 'react'
import type { Motion, MotionGenerationSettings, MotionReference, MotionReferenceRole, StylePreset, ViewId } from '../../api/types'
import { PRESET_DEFAULTS } from './generationPresetDefaults'

type SeededField = 'steps' | 'width' | 'height'

const STYLE_OPTIONS: { value: StylePreset; label: string }[] = [
  { value: 'high_fantasy', label: 'High Fantasy' },
  { value: 'cyberpunk', label: 'Cyberpunk' },
  { value: 'steampunk', label: 'Steampunk' },
  { value: 'space_corporate', label: 'Space Corporate' },
  { value: 'grimdark', label: 'Grimdark' },
  { value: 'post_apocalyptic', label: 'Post-Apocalyptic' },
  { value: 'noir', label: 'Noir' },
  { value: 'psychedelic', label: 'Psychedelic' },
  { value: 'ancient_mythology', label: 'Ancient Mythology' },
  { value: 'western', label: 'Western' },
]

export interface MotionSpecPatch { view_id?: ViewId | null; action?: string; references?: MotionReference[]; style?: StylePreset | null; script?: string | null }

export function GenerationSettings({
  value,
  onChange,
  motion,
  onUploadReference,
  onPatchMotion,
}: {
  value: MotionGenerationSettings
  onChange: (value: MotionGenerationSettings) => void
  /** T17: when supplied alongside the two callbacks below, the panel also edits the motion's
   * persisted references/style/script (not just this ephemeral generation-settings draft). */
  motion?: Motion
  onUploadReference?: (file: File) => Promise<MotionReference>
  onPatchMotion?: (patch: MotionSpecPatch) => void | Promise<void>
}) {
  const [advanced, setAdvanced] = useState(false)
  // A field stops taking preset defaults once the operator has edited it directly -- switching
  // preset afterward must not clobber a value they just chose (T7). This panel stays mounted
  // while MotionWorkspace switches the selected motion, so the flags belong to whichever
  // motion is current: reset them when that identity changes (T13), or an edit made to motion A
  // would otherwise suppress motion B's preset defaults too.
  const [dirty, setDirty] = useState<Record<SeededField, boolean>>({ steps: false, width: false, height: false })
  useEffect(() => { setDirty({ steps: false, width: false, height: false }) }, [motion?.motion_id])
  const number = (field: 'steps' | 'seed' | 'width' | 'height' | 'duration_seconds') => (event: React.ChangeEvent<HTMLInputElement>) => {
    if (field === 'steps' || field === 'width' || field === 'height') setDirty((current) => ({ ...current, [field]: true }))
    onChange({ ...value, [field]: Number(event.target.value) })
  }
  const changePreset = (preset: MotionGenerationSettings['preset']) => {
    const defaults = PRESET_DEFAULTS[preset]
    onChange({
      ...value,
      preset,
      steps: dirty.steps ? value.steps : defaults.steps,
      width: dirty.width ? value.width : defaults.width,
      height: dirty.height ? value.height : defaults.height,
    })
  }
  // Higgsfield runs its own sampler: steps, seed and preset would be inputs it never receives.
  const hosted = value.provider_parameters.execution_provider === 'higgsfield'
  return <section className="generation-settings" aria-label="Generation settings">
    <label>Duration (seconds)<input aria-label="Duration (seconds)" min="1" max="120" type="number" value={value.duration_seconds} onChange={number('duration_seconds')} /></label>
    <button className="disclosure" type="button" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>Advanced generation settings</button>
    {advanced && <div className="generation-settings__grid">
      {!hosted && import.meta.env.VITE_MAGIC_DESKTOP_RECIPE !== 'balanced' && !value.provider_parameters.recipe_version && value.provider_parameters.execution_profile !== 'h3-w4a8-16gb-lightx8' && <label>Preset<select aria-label="Preset" value={value.preset} onChange={(event) => changePreset(event.target.value as MotionGenerationSettings['preset'])}><option value="quick">Quick</option><option value="standard">Standard</option><option value="high">High</option><option value="cinematic">Cinematic</option></select></label>}
      {!hosted && <label>Steps<input aria-label="Steps" min="4" max="60" type="number" value={value.steps} disabled={value.provider_parameters.execution_profile === 'h3-w4a8-16gb-lightx8'} onChange={number('steps')} /></label>}
      {!hosted && <label>Seed<input aria-label="Seed" min="0" type="number" value={value.seed} onChange={number('seed')} /></label>}
      <label>Width<input aria-label="Width" min="64" max="4096" type="number" value={value.width} onChange={number('width')} /></label>
      <label>Height<input aria-label="Height" min="64" max="4096" type="number" value={value.height} onChange={number('height')} /></label>
      <label>Backdrop<select aria-label="Backdrop" value={value.backdrop} onChange={(event) => onChange({ ...value, backdrop: event.target.value as MotionGenerationSettings['backdrop'] })}><option value="chroma_green">Chroma green (key out alpha)</option><option value="native_alpha">Native alpha</option></select></label>
    </div>}
    {advanced && motion && onPatchMotion && <MotionReferencesEditor motion={motion} onUploadReference={onUploadReference} onPatchMotion={onPatchMotion} />}
  </section>
}

/** References with role chips, a style select, and a script textarea (T17), persisted on the
 * motion via `PATCH /motions/{motion_id}`. */
function MotionReferencesEditor({
  motion,
  onUploadReference,
  onPatchMotion,
}: {
  motion: Motion
  onUploadReference?: (file: File) => Promise<MotionReference>
  onPatchMotion: (patch: MotionSpecPatch) => void | Promise<void>
}) {
  const references = motion.references ?? []
  async function addReference(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file || !onUploadReference) return
    const reference = await onUploadReference(file)
    await onPatchMotion({ references: [...references, reference] })
  }
  function setRole(key: string, role: MotionReferenceRole) {
    void onPatchMotion({ references: references.map((reference) => reference.key === key ? { ...reference, role } : reference) })
  }
  function removeReference(key: string) {
    void onPatchMotion({ references: references.filter((reference) => reference.key !== key) })
  }
  return <div className="motion-references">
    {!motion.view_id && <>
    <p className="eyebrow">References</p>
    {references.length > 0 && <ul className="motion-references__list">
      {references.map((reference) => <li className="motion-references__chip" key={reference.key}>
        <span>{reference.key.split('/').pop()}</span>
        <select aria-label={`Role for ${reference.key.split('/').pop()}`} value={reference.role} onChange={(event) => setRole(reference.key, event.target.value as MotionReferenceRole)}>
          <option value="start">Start</option><option value="context">Context</option><option value="end">End</option>
        </select>
        <button type="button" aria-label={`Remove reference ${reference.key.split('/').pop()}`} onClick={() => removeReference(reference.key)}>Remove</button>
      </li>)}
    </ul>}
    {onUploadReference && <label>Add reference<input aria-label="Add reference" type="file" accept="image/png,image/jpeg,image/webp" onChange={addReference} /></label>}
    </>}
    {motion.view_id && <p>The character view supplies this animation’s facing reference.</p>}
    <label>Style<select aria-label="Style" value={motion.style ?? ''} onChange={(event) => onPatchMotion({ style: (event.target.value || null) as StylePreset | null })}>
      <option value="">None</option>
      {STYLE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select></label>
    <label>Script<textarea aria-label="Script" maxLength={2000} value={motion.script ?? ''} onChange={(event) => onPatchMotion({ script: event.target.value || null })} /></label>
  </div>
}

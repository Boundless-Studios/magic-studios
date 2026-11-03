import { Link } from 'react-router-dom'
import type { GenerationConfig } from '../../api/types'
import { machineAllowed, type ComputeChoiceValue } from './computeChoiceModel'

export function ComputeChoice({ config, value, onChange, disabled }: {
  config: GenerationConfig | undefined
  value: ComputeChoiceValue
  onChange: (value: ComputeChoiceValue) => void
  disabled?: boolean
}) {
  const hosted = config?.providers?.higgsfield
  const machine = machineAllowed(config)
  if (!machine && !hosted?.configured) {
    // A public account with nothing it can run on: say what would unblock it.
    return <p className="compute-choice compute-choice--hint">{config?.account?.h3 === false
      ? 'MiniMax H3 is not available for public accounts.'
      : 'Shared machines are not available for public accounts.'} <Link to="/account/machines">{config?.account?.h3 === false ? 'Add a Higgsfield key' : 'Add a Higgsfield key or connect a GPU'}</Link> to generate.</p>
  }
  // Why no machine was selected, from discovery: "Motions need a 16 GB card and this one has 12 GB".
  const blocked = config?.selected ? null : config?.workers?.find((worker) => worker.reason)?.reason ?? null
  if (!hosted?.available) return blocked ? <p className="compute-choice compute-choice--hint" role="note">Your machine can't run motions: {blocked}.</p> : null
  if (!hosted.configured) {
    return <p className="compute-choice compute-choice--hint">{blocked ? `Your machine can't run motions: ${blocked}. ` : 'No GPU handy? '}<Link to="/account/machines">Add your Higgsfield API key</Link> to generate with your own Higgsfield account.</p>
  }
  const machineLabel = config?.selected ? 'Your GPU machine' : blocked ? "Your GPU machine (can't run motions)" : 'Your GPU machine (none online)'
  return <label className="compute-choice">Run on
    <select aria-label="Run on" value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      {machine && <option value="machine">{machineLabel}</option>}
      {hosted.models?.map((model) => <option key={model.model} value={model.model}>{`Higgsfield · ${model.label}`}</option>)}
    </select>
    {value === 'machine' && blocked && <small role="note">{blocked}.</small>}
    {value !== 'machine' && <small>Charged to your Higgsfield account (key {hosted.key_hint}). Steps and seed are not sent; Higgsfield sizes the video from the opening frame. Not benchmarked in Magic Studios yet.</small>}
  </label>
}

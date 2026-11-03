import type { GenerationConfig, MotionGenerationSettings } from '../../api/types'

/** `machine` runs on an enrolled GPU; any other value is a Higgsfield model ID. */
export type ComputeChoiceValue = string

/** Whether this account may run on a GPU machine: always for invited accounts; for a public
 * account only on its own machine, and only where MiniMax H3 is allowed for it. */
export function machineAllowed(config: GenerationConfig | undefined): boolean {
  const account = config?.account
  return !account || account.tier !== 'public' || (account.h3 && account.own_machine)
}

/** Whether a choice can run for this account at all; when none can, Generate stays disabled
 * and the picker explains what would unblock it. */
export function choiceUsable(config: GenerationConfig | undefined, choice: ComputeChoiceValue): boolean {
  return choice === 'machine' ? machineAllowed(config) : Boolean(config?.providers?.higgsfield?.configured)
}

/** The choice shown before the operator picks: their machine whenever discovery offers one,
 * otherwise their saved Higgsfield key. Nothing runs until they press Generate. */
export function defaultComputeChoice(config: GenerationConfig | undefined): ComputeChoiceValue {
  const hosted = config?.providers?.higgsfield
  const machine = machineAllowed(config) && (config?.settings ?? config?.default_settings)
  if (machine || !hosted?.configured || !hosted.settings) return 'machine'
  return String(hosted.settings.provider_parameters.higgsfield_model ?? hosted.models?.[0]?.model ?? 'machine')
}

/** The route a set of settings will actually run on. The picker shows this, never a separate
 * preference, so seeded or historical settings cannot be sent under a different label. */
export function choiceOfSettings(settings: MotionGenerationSettings): ComputeChoiceValue {
  if (settings.provider_parameters.execution_provider !== 'higgsfield') return 'machine'
  return String(settings.provider_parameters.higgsfield_model ?? 'kling-3-std')
}

/** Settings for a choice. Higgsfield settings come from discovery unchanged except the model,
 * with the duration kept inside that model's accepted range. */
export function settingsForChoice(config: GenerationConfig | undefined, choice: ComputeChoiceValue, machine: MotionGenerationSettings): MotionGenerationSettings {
  const hosted = config?.providers?.higgsfield
  const model = hosted?.models?.find((item) => item.model === choice)
  if (choice === 'machine' || !hosted?.settings || !model) return machine
  const duration = Math.min(model.duration_seconds.max, Math.max(model.duration_seconds.min, hosted.settings.duration_seconds))
  return { ...hosted.settings, duration_seconds: duration, provider_parameters: { ...hosted.settings.provider_parameters, higgsfield_model: model.model } }
}

import type { AnimationType, LoopPolicy, Motion, MotionPreset, TargetInfo } from '../../api/types'

/**
 * The studio's preset motion library, mirrored from the server's `presets.py`. Kept as a static
 * table rather than fetched from `GET /studio/presets` so the "Add motion" grid renders with no
 * round trip. Slot aliasing onto any one target's own vocabulary (e.g. one target's
 * `move`→`walk`, `trip`→`hurt`) is target-adapter business now, resolved from
 * `GET /studio/targets` via `presetForTargetSlot` below -- no target's name appears in
 * this table.
 */
export interface MotionPresetInfo {
  preset: MotionPreset
  displayName: string
  description: string
  semanticSlot: string
  loopPolicy: LoopPolicy
  animationType: AnimationType
  /** The "what happens" line this preset starts from; the operator edits it before generating. */
  defaultAction: string
}

export const MOTION_PRESETS: MotionPresetInfo[] = [
  { preset: 'idle', displayName: 'Idle', description: 'Resting loop with subtle breathing motion.', semanticSlot: 'idle', loopPolicy: 'loop', animationType: 'movement', defaultAction: 'breathes and shifts weight' },
  { preset: 'walk', displayName: 'Walk', description: 'Steady forward walk cycle.', semanticSlot: 'walk', loopPolicy: 'loop', animationType: 'movement', defaultAction: 'walks in place' },
  { preset: 'run', displayName: 'Run', description: 'Full sprint cycle.', semanticSlot: 'run', loopPolicy: 'loop', animationType: 'movement', defaultAction: 'runs at a full sprint' },
  { preset: 'jump', displayName: 'Jump', description: 'Jump up and land.', semanticSlot: 'jump', loopPolicy: 'once', animationType: 'movement', defaultAction: 'jumps up and lands' },
  { preset: 'attack', displayName: 'Attack', description: 'One decisive weapon swing.', semanticSlot: 'attack', loopPolicy: 'once', animationType: 'attack', defaultAction: 'swings a weapon' },
  { preset: 'guard', displayName: 'Guard', description: 'Braced block, held steady.', semanticSlot: 'guard', loopPolicy: 'once', animationType: 'movement', defaultAction: 'raises a guard' },
  { preset: 'cast', displayName: 'Cast', description: 'Channel and release a spell.', semanticSlot: 'cast', loopPolicy: 'once', animationType: 'spellcast', defaultAction: 'casts a spell with a flourish' },
  { preset: 'hurt', displayName: 'Hurt', description: 'React to a hit and recover stance.', semanticSlot: 'hurt', loopPolicy: 'once', animationType: 'movement', defaultAction: 'flinches from a hit' },
  { preset: 'death', displayName: 'Death', description: 'Collapse from a fatal blow.', semanticSlot: 'death', loopPolicy: 'once', animationType: 'movement', defaultAction: 'collapses from a fatal blow' },
  { preset: 'trip', displayName: 'Trip', description: 'Sweep low to trip an opponent.', semanticSlot: 'trip', loopPolicy: 'once', animationType: 'movement', defaultAction: 'sweeps low to trip an opponent' },
]

export const MOTION_PRESETS_BY_ID: Record<MotionPreset, MotionPresetInfo> = Object.fromEntries(
  MOTION_PRESETS.map((preset) => [preset.preset, preset]),
) as Record<MotionPreset, MotionPresetInfo>

const MOTION_PRESETS_BY_SEMANTIC_SLOT: Record<string, MotionPresetInfo> = Object.fromEntries(
  MOTION_PRESETS.map((preset) => [preset.semanticSlot, preset]),
)

/** The reference game's six, in `videoLabSlotBatch.js`'s `SLOT_BATCH` order, as studio
 * presets. "Add the recommended set" adds exactly these. */
export const RECOMMENDED_PRESET_SET: MotionPreset[] = ['idle', 'walk', 'attack', 'guard', 'cast', 'hurt']

/** The "What happens" line to show for a motion: its own stored line, else the preset default;
 * a custom motion has no template, so its direction (`prompt`) is that line. */
export function actionLineFor(motion: Pick<Motion, 'action' | 'kind' | 'preset' | 'prompt'>): string {
  if (motion.action) return motion.action
  if (motion.kind === 'custom') return motion.prompt ?? ''
  return motion.preset ? MOTION_PRESETS_BY_ID[motion.preset]?.defaultAction ?? '' : ''
}

/** motion_id/name defaults for a freshly-added preset motion; the server compiles the authored
 * prompt and canonical loop policy itself (see `_compile_preset_specification`). */
export function motionIdForPreset(preset: MotionPreset): string {
  return preset
}

/** Resolves one of a target's own slot names (from a `/new?target=…&slot=…` deep link) onto the
 * studio's own preset, via that target's `TargetInfo.slots` from `GET /studio/targets` -- no
 * target's slot vocabulary is hard-coded in web source. Returns `undefined` for a slot name the
 * target does not declare, or one whose winning neutral slot has no studio preset. */
export function presetForTargetSlot(target: TargetInfo, slotName: string): MotionPreset | undefined {
  const slot = target.slots.find((candidate) => candidate.name === slotName)
  if (!slot) return undefined
  return MOTION_PRESETS_BY_SEMANTIC_SLOT[slot.winner]?.preset
}

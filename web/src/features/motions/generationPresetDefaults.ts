import type { MotionGenerationSettings } from '../../api/types'

/** Ported from the reference game's `PRESET_DEFAULTS` (VideoLab.jsx:73-78). Mirrors the server's
 * `MotionGenerationSettings.PRESET_DEFAULTS` -- keep both in lockstep by hand; there is no
 * shared codegen between the Python and TypeScript models here. */
export const PRESET_DEFAULTS: Record<MotionGenerationSettings['preset'], { steps: number; width: number; height: number }> = {
  quick: { steps: 16, width: 864, height: 480 },
  standard: { steps: 20, width: 1344, height: 768 },
  high: { steps: 28, width: 1344, height: 768 },
  cinematic: { steps: 32, width: 1344, height: 768 },
}

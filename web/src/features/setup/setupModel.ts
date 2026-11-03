import type { Heading, ProjectSetup, SetupMotion, SetupView } from '../../api/types'
import { MOTION_PRESETS_BY_ID } from '../motions/motionPresets'
import type { MotionPreset } from '../../api/types'
export type Template = 'single' | 'side' | 'four' | 'eight'
export const DIRECTIONS: { heading: Heading; label: string; arrow: string }[] = [
  { heading: 'N', label: 'North · away', arrow: '↑' }, { heading: 'NE', label: 'Northeast', arrow: '↗' },
  { heading: 'E', label: 'East · right', arrow: '→' }, { heading: 'SE', label: 'Southeast', arrow: '↘' },
  { heading: 'S', label: 'South · toward you', arrow: '↓' }, { heading: 'SW', label: 'Southwest', arrow: '↙' },
  { heading: 'W', label: 'West · left', arrow: '←' }, { heading: 'NW', label: 'Northwest', arrow: '↖' },
  { heading: 'left', label: 'Left', arrow: '←' }, { heading: 'right', label: 'Right', arrow: '→' },
]
// Studio does not draw missing directions, so each template states the artwork it needs.
export const TEMPLATES: { id: Template; title: string; detail: string; example: string; artwork: string }[] = [
  { id: 'single', title: 'Single direction', detail: 'Keep the camera and facing in your artwork.', example: '●', artwork: 'Uses only this image' },
  { id: 'side', title: 'Side-scrolling', detail: 'Left and right, seen from the side.', example: '← ● →', artwork: 'Needs art for 2 directions' },
  { id: 'four', title: 'Four-direction', detail: 'North, east, south and west.', example: '↑ → ↓ ←', artwork: 'Needs art for 4 directions' },
  { id: 'eight', title: 'Eight-direction', detail: 'A compass of eight screen headings.', example: '↖ ↑ ↗  ← ● →  ↙ ↓ ↘', artwork: 'Needs art for 8 directions' },
]
export function headings(template: Template) { return DIRECTIONS.filter(({ heading }) => template === 'side' ? heading === 'left' || heading === 'right' : template === 'four' ? ['N', 'E', 'S', 'W'].includes(heading) : template === 'eight' ? heading !== 'left' && heading !== 'right' : false) }
/** Directions that request animations but have no artwork yet; each needs its own upload before it can generate. */
export function artworkNeeded(setup: ProjectSetup, facing: Heading | null) { return setup.views.filter((view) => view.heading !== facing && view.motions.length > 0).length }
export function motionFor(viewId: string, preset: MotionPreset): SetupMotion { const info = MOTION_PRESETS_BY_ID[preset]; return { motion_id: `${viewId.toLowerCase()}-${preset}`, name: info.displayName, kind: 'preset', preset, loop_policy: info.loopPolicy, required: true } }
export function recommendedSetup(template: Template, facing: Heading | null, reference: string | null, justImage = false): ProjectSetup {
  const dirs = template === 'single' ? [{ heading: null, label: 'Original view' }] : justImage ? headings(template).filter((item) => item.heading === facing) : headings(template)
  const views: SetupView[] = dirs.map(({ heading, label }) => {
    const id = heading ?? 'single'; const supplied = heading === facing
    const presets: MotionPreset[] = justImage ? [] : template === 'eight' ? ['walk', ...(['N', 'E', 'S', 'W'].includes(id) ? ['attack' as const] : []), ...(id === 'S' ? ['idle' as const] : [])] : ['idle', 'walk']
    return { view_id: id, heading, name: label, reference: supplied && reference ? { revision_id: reference } : null, motions: presets.map((preset) => motionFor(id, preset)) }
  })
  return { schema_version: 1, sprite_layout: template === 'single' ? 'single_view' : template === 'side' ? 'side_view' : 'isometric', identity_reference: reference ? { revision_id: reference } : null, views }
}

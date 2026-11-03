import type { Motion, SpriteLayout, ViewId } from '../../api/types'

export const VIEW_OPTIONS: { id: ViewId; label: string }[] = [
  { id: 'left', label: 'Left' }, { id: 'right', label: 'Right' },
  { id: 'N', label: 'North · away' }, { id: 'NE', label: 'Northeast' }, { id: 'E', label: 'East · right' }, { id: 'SE', label: 'Southeast' },
  { id: 'S', label: 'South · toward viewer' }, { id: 'SW', label: 'Southwest' }, { id: 'W', label: 'West · left' }, { id: 'NW', label: 'Northwest' },
]
export function viewOptions(layout: SpriteLayout) { return VIEW_OPTIONS.filter(({ id }) => layout === 'side_view' ? id === 'left' || id === 'right' : layout === 'isometric' ? id !== 'left' && id !== 'right' : false) }
export function uniqueMotionId(motions: Motion[], base: string) {
  let id = base; let suffix = 2
  while (motions.some((motion) => motion.motion_id === id)) id = `${base}-${suffix++}`
  return id
}

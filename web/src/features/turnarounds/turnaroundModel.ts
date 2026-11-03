import { canStartTurnaround, type CompassHeading, type GenerationConfig, type ProjectResponse, type TurnaroundEquipment } from '../../api/types'

/** The order the turnaround visits headings: right first, then away, left, back. */
export const TURN_ORDER: CompassHeading[] = ['S', 'SE', 'E', 'NE', 'N', 'NW', 'W', 'SW']
export const HEADING_LABEL: Record<CompassHeading, string> = {
  N: 'Back', NE: 'Back right', E: 'Right', SE: 'Front right', S: 'Front', SW: 'Front left', W: 'Left', NW: 'Back left',
}

/** The headings to pick, in turn order from the source (which is the loop's anchor, never a pick). */
export function pickHeadings(source: CompassHeading): CompassHeading[] {
  const start = TURN_ORDER.indexOf(source)
  return [...TURN_ORDER.slice(start + 1), ...TURN_ORDER.slice(0, start)]
}

/** A view's compass heading. Legacy compass views carry it only as their `view_id`, as the server reads them. */
export function effectiveHeading(view: { view_id: string; heading?: string | null }): string | null {
  return view.heading ?? (TURN_ORDER.includes(view.view_id as CompassHeading) ? view.view_id : null)
}

/** Source headings the server qualifies, in the order a project's references are preferred. */
export const SOURCE_HEADINGS: CompassHeading[] = ['S', 'SE', 'SW']

export type Eligibility = { ok: true; sourceViewId: string; sourceRevisionId: string } | { ok: false; reason: string }

/** The same rules POST /turnarounds enforces, so the UI never offers a flow it would refuse. */
export function turnaroundEligibility(project: ProjectResponse, config: GenerationConfig | undefined): Eligibility {
  const layout = project.manifest.sprite_layout
  if (layout === 'side_view') return { ok: false, reason: 'Turnarounds currently need a front or three-quarter source' }
  const views = (project.manifest.views ?? []).filter((view) => view.reference)
  const source = SOURCE_HEADINGS.map((heading) => views.find((view) => effectiveHeading(view) === heading)).find(Boolean)
  if (!source?.reference) return { ok: false, reason: 'Needs a front or three-quarter (S, SE or SW) reference: upload one or use Upload reference' }
  if (!canStartTurnaround(config)) return { ok: false, reason: 'No machine supports turnarounds yet' }
  return { ok: true, sourceViewId: source.view_id, sourceRevisionId: source.reference.revision_id }
}

/** What an equipment row means on the character, from an S, SE or SW source: the picture's left is their right. */
export function characterSide(row: TurnaroundEquipment): string {
  if (row.source_picture_side === 'unsided' || !['hand', 'shoulder', 'hip'].includes(row.attachment)) return `${row.item}: ${row.attachment}`
  return `${row.item}: the character's ${row.source_picture_side === 'left' ? 'right' : 'left'} ${row.attachment}`
}

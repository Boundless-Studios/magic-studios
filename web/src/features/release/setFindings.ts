import { setMotionLabel } from './setSummary'
import type { FindingSeverity, ManifestSummary } from '../../api/types'

/** One row in the Release page's Set findings section (studio-ux-flow.md section 5). Quality
 * findings are warnings, never blockers (operator decision, 2026-09-05): every finding is
 * informational and approving the set always proceeds, recording every finding acknowledged. A
 * finding that concerns a specific motion (its selected candidate's own quality assessment)
 * carries `motionId`/`motionName` for the "Fix in Frames" link; a genuine set-level finding
 * (canvas mismatch, a stale subject selection, ...) carries neither. */
export interface SetFinding {
  id: string
  severity: FindingSeverity
  title: string
  explanation: string
  motionId: string | null
  motionName: string | null
}

/**
 * Derives the Release page's set findings from the manifest. `set_assessment.findings` already
 * copies every selected candidate's own findings (same code + message) into the set-level
 * assessment, so it is the single source here -- but a copy that also appears on a selected
 * candidate is re-labelled under that motion's name and pointed at "Fix in Frames" instead of
 * being shown twice.
 */
export function deriveSetFindings(manifest: Pick<ManifestSummary, 'motions' | 'set_assessment' | 'views'>): SetFinding[] {
  const motionFindingKeys = new Set<string>()
  const motionFindings: SetFinding[] = manifest.motions.flatMap((motion) => {
    const candidate = motion.selected_candidate_id
      ? motion.candidates.find((item) => item.candidate_id === motion.selected_candidate_id)
      : null
    return (candidate?.assessment?.findings ?? []).map((finding) => {
      motionFindingKeys.add(`${finding.code}::${finding.message}`)
      return {
        id: `${motion.motion_id}-${finding.code}`,
        severity: finding.severity,
        title: `${setMotionLabel(motion, manifest.views)}: ${finding.code.replaceAll('_', ' ')}`,
        explanation: finding.message,
        motionId: motion.motion_id,
        motionName: setMotionLabel(motion, manifest.views),
      }
    })
  })
  const setOnlyFindings: SetFinding[] = (manifest.set_assessment?.findings ?? [])
    .filter((finding) => !motionFindingKeys.has(`${finding.code}::${finding.message}`))
    .map((finding) => ({
      id: finding.code,
      severity: finding.severity,
      title: finding.code.replaceAll('_', ' '),
      explanation: finding.message,
      motionId: null,
      motionName: null,
    }))
  return [...motionFindings, ...setOnlyFindings]
}

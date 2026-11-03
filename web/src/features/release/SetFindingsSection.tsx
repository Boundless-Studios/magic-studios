import { Link } from 'react-router-dom'
import type { SetFinding } from './setFindings'

/**
 * The Release page's set findings section (studio-ux-flow.md section 5): the quality inbox,
 * folded in. Quality findings are warnings, never blockers (operator decision, 2026-09-05) --
 * every finding shown here is informational ("Heads up"); approving the set always proceeds and
 * records every finding as acknowledged, so there is no per-finding waiver control. A finding
 * that concerns a specific motion instead links "Fix in Frames" back to Motions with that motion
 * selected, the same handoff `MotionWorkspace` already reads from `location.state`.
 */
export function SetFindings({ projectId, findings }: {
  projectId: string
  findings: SetFinding[]
}) {
  if (!findings.length) return <p className="quality-empty">No set findings.</p>
  return <ul aria-label="Set findings" className="quality-groups">
    {findings.map((finding) => (
      <li aria-label={finding.title} className="finding" key={finding.id}>
        <div>
          <h4>{finding.title}</h4>
          <p>{finding.explanation}</p>
          {finding.motionId && <Link state={{ selectMotionId: finding.motionId }} to={`/projects/${projectId}/motions`}>Fix in Frames</Link>}
        </div>
        <div className="finding__actions">
          <span className="chip chip--warn">Heads up</span>
        </div>
      </li>
    ))}
  </ul>
}

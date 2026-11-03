import { memo } from 'react'
import { Link } from 'react-router-dom'
import { useAssetPreview } from '../../api/queries'
import type { ExternalSubject, SubjectRevision } from '../../api/types'

/**
 * The persistent subject strip at the top of the Motions page (studio-ux-flow.md section 4):
 * the chosen subject is on screen wherever motion work happens, and the opening pose that binds
 * every motion together is an internal detail the operator never configures.
 */
export const SubjectStrip = memo(function SubjectStrip({ projectId, subjectRevision, externalSubject, projectName, viewLabel }: {
  projectId: string
  subjectRevision: SubjectRevision | null
  externalSubject: ExternalSubject | null
  projectName: string
  viewLabel?: string
}) {
  const preview = useAssetPreview(projectId, subjectRevision?.artifact_key)
  const name = externalSubject?.display_name || projectName || 'Chosen subject'
  return <div className="subject-strip">
    <div className={subjectRevision && preview.data ? 'subject-strip__thumb checkerboard' : 'subject-strip__thumb'}>
      {subjectRevision && preview.data ? <img alt={`${name} thumbnail`} src={preview.data.upload_url} /> : <span aria-hidden="true">{name.charAt(0).toUpperCase()}</span>}
    </div>
    <div className="subject-strip__body">
      <strong>{name}</strong>
      <small>{viewLabel ? `Character identity · animations use the ${viewLabel} reference` : 'Opening pose is prepared from this subject automatically'}</small>
    </div>
    <Link className="subject-strip__change" to={`/projects/${projectId}/subject`}>Change subject</Link>
  </div>
})

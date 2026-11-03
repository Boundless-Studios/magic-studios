import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { studioApi } from '../../api/client'
import type { CharacterView, ExternalSubject, Motion, SubjectRevision } from '../../api/types'
import { railThumbFrameStyle } from '../motions/motionRailThumb'
import { buildSetItems, setSummaryLine, type SetMotionItem } from './setSummary'

const STRIP_TILE_SIZE_PX = 64

function SubjectTile({ projectId, subjectRevision, name }: { projectId: string; subjectRevision: SubjectRevision | null; name: string }) {
  const preview = useQuery({
    queryKey: ['asset-preview', projectId, subjectRevision?.artifact_key],
    queryFn: () => studioApi.preview(projectId, subjectRevision!.artifact_key),
    enabled: Boolean(subjectRevision),
  })
  if (!subjectRevision || !preview.data) {
    return <div aria-label={`${name} thumbnail unavailable`} className="set-strip__tile set-strip__tile--placeholder" role="img">{name.charAt(0).toUpperCase()}</div>
  }
  return <img alt={`${name} thumbnail`} className="set-strip__tile" src={preview.data.upload_url} />
}

function MotionTile({ projectId, item }: { projectId: string; item: SetMotionItem }) {
  const candidate = item.candidate
  const preview = useQuery({
    queryKey: ['asset-preview', projectId, candidate?.artifact_key],
    queryFn: () => studioApi.preview(projectId, candidate!.artifact_key),
    enabled: Boolean(candidate),
  })
  if (!candidate) {
    return <div className="set-strip__cell set-strip__cell--placeholder">
      <div aria-label={`${item.name} · not selected`} className="set-strip__tile set-strip__tile--placeholder" role="img" />
      <small style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{item.name} · not selected</small>
    </div>
  }
  if (!preview.data) {
    return <div className="set-strip__cell"><div aria-label={`${item.name} thumbnail loading`} className="set-strip__tile" role="img" /><small style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{item.name}</small></div>
  }
  const media = candidate.metadata?.media_kind === 'sheet'
    ? <span aria-label={item.name} className="set-strip__tile" role="img" style={railThumbFrameStyle(preview.data.upload_url, candidate.metadata, STRIP_TILE_SIZE_PX)} />
    : <video aria-label={item.name} className="set-strip__tile" muted preload="metadata" src={preview.data.upload_url}>
        <track default kind="captions" src="data:text/vtt,WEBVTT" srcLang="en" />
      </video>
  return <div className={item.stale ? 'set-strip__cell set-strip__cell--stale' : 'set-strip__cell'}>
    {media}
    <small style={{ whiteSpace: 'normal', overflowWrap: 'anywhere' }}>{item.name}</small>
    {item.stale && <Link className="set-strip__stale-tag" state={{ selectMotionId: item.motionId }} to="../motions">Stale</Link>}
  </div>
}

/**
 * The Release page's set summary (studio-ux-flow.md section 5): the chosen subject followed by
 * the selected take of every motion, with missing or stale motions called out inline, plus the
 * one-line readiness summary.
 */
export function SetStrip({ projectId, projectName, subjectRevision, externalSubject, motions, findingsCount, views = [] }: {
  projectId: string
  projectName: string
  subjectRevision: SubjectRevision | null
  externalSubject: ExternalSubject | null
  views?: CharacterView[]
  motions: Motion[]
  findingsCount: number
}) {
  const items = buildSetItems(motions, subjectRevision, views)
  const name = externalSubject?.display_name || projectName || 'Chosen subject'
  return <section aria-labelledby="set-strip-title" className="release-card set-strip">
    <span className="step-number">01</span>
    <h3 id="set-strip-title">Set</h3>
    <div className="set-strip__row">
      <SubjectTile name={name} projectId={projectId} subjectRevision={subjectRevision} />
      <ol aria-label="Selected takes" className="set-strip__motions">
        {items.map((item) => <li key={item.motionId}><MotionTile item={item} projectId={projectId} /></li>)}
      </ol>
    </div>
    <p className="set-strip__summary">{setSummaryLine(items, findingsCount)}</p>
  </section>
}

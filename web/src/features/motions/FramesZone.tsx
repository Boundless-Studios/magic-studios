import { useQuery } from '@tanstack/react-query'
import { useEffect, useRef, type ReactNode } from 'react'
import { studioApi } from '../../api/client'
import type { LoopPolicy, MotionCandidate, MotionPreset } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { FrameEditor } from './FrameEditor'
import type { FrameDraft } from './frameDraft'

/** The Frames zone (studio-ux-flow.md section 4, zone 3 of 3): hidden until "Edit frames" on a
 * take, opened below the gallery under its own heading. Saving keeps existing behaviour --
 * creates a new take and selects it -- which is `onSave`'s job, not this wrapper's. */
export function FramesZone({
  projectId, motionId, motionName, motionPreset, candidate, walkReference, takeIndex, fallbackLoop, onSave, onDeriveAlpha, derivingAlpha, onClose, settingsRail,
}: {
  projectId: string
  motionId: string
  motionName: string
  motionPreset?: MotionPreset | null
  candidate: MotionCandidate
  walkReference?: MotionCandidate
  takeIndex: number
  fallbackLoop: LoopPolicy
  onSave: (draft: FrameDraft) => Promise<void>
  onDeriveAlpha: () => void
  derivingAlpha: boolean
  onClose: () => void
  settingsRail?: ReactNode
}) {
  const preview = useQuery({ queryKey: ['asset-preview', projectId, candidate.artifact_key], queryFn: () => studioApi.preview(projectId, candidate.artifact_key) })
  const walkPreview = useQuery({ queryKey: ['asset-preview', projectId, walkReference?.artifact_key], queryFn: () => studioApi.preview(projectId, walkReference!.artifact_key), enabled: motionPreset === 'attack' && !!walkReference })
  const zoneRef = useRef<HTMLElement>(null)
  const scrolledCandidate = useRef<string | null>(null)
  useEffect(() => {
    if (!preview.data || scrolledCandidate.current === candidate.candidate_id) return
    scrolledCandidate.current = candidate.candidate_id
    zoneRef.current?.scrollIntoView?.({ block: 'nearest' })
  }, [candidate.candidate_id, preview.data])
  return <section ref={zoneRef} className="motion-zone motion-zone--frames" aria-label="Frames">
    <div className="motion-zone__heading">
      <h4>Frames · take {takeIndex}</h4>
      <Button onClick={onClose}>Close</Button>
    </div>
    {!preview.data
      ? <div className="playback-stage" role="status"><small>{preview.isPending ? 'Loading frame editor' : 'Frame editor unavailable'}</small></div>
      : <FrameEditor key={candidate.candidate_id} projectId={projectId} motionId={motionId} motionName={motionName} motionPreset={motionPreset} candidate={candidate} previewUrl={preview.data.upload_url} walkReference={walkReference} walkReferenceUrl={walkPreview.data?.upload_url} fallbackLoop={fallbackLoop} onSave={onSave} onDeriveAlpha={onDeriveAlpha} derivingAlpha={derivingAlpha} settingsRail={settingsRail} />}
  </section>
}

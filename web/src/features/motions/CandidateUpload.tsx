import { useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { studioApi } from '../../api/client'
import { Button } from '../../components/ui/Button'

// T21: ingest a clip/sheet produced elsewhere (Ludo, AutoSprite, an artist) as a
// candidate. The frame-count field only matters for a PNG strip -- an mp4/webm
// self-reports its frame count -- so it appears once a PNG is chosen.
const UPLOAD_CONTENT_TYPES: Record<string, string> = { '.mp4': 'video/mp4', '.webm': 'video/webm', '.png': 'image/png' }

// T9: normalize once so the frame-count field's visibility and the settled content type can
// never disagree -- some browsers/OSes report an empty File.type even for a selected .png.
function normalizedUploadContentType(file: File): string {
  return file.type || UPLOAD_CONTENT_TYPES[`.${file.name.split('.').pop()}`] || ''
}

/** "or upload a clip you already have" -- revealed inline in the Generate zone
 * (studio-ux-flow.md section 4). */
export function CandidateUpload({ projectId, motionId, expectedRevision }: { projectId: string; motionId: string; expectedRevision: number }) {
  const [file, setFile] = useState<File | null>(null)
  const [frameCount, setFrameCount] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const client = useQueryClient()
  const contentType = file ? normalizedUploadContentType(file) : ''
  const isPng = contentType === 'image/png'
  async function submit() {
    if (!file) return
    setBusy(true); setError(null)
    try {
      const target = await studioApi.createCandidateUpload(projectId, motionId, expectedRevision, contentType)
      await studioApi.upload(target, file)
      await studioApi.settleCandidateUpload(projectId, motionId, expectedRevision, target.key, isPng ? Number(frameCount) : null)
      await client.invalidateQueries({ queryKey: ['studio-project', projectId] })
      setFile(null); setFrameCount('')
    } catch { setError('That upload could not be ingested. Check the file and frame count.') }
    finally { setBusy(false) }
  }
  return <div className="candidate-upload">
    <label>Upload existing animation<input type="file" accept="video/mp4,video/webm,image/png" aria-label="Upload existing animation" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
    {isPng && <label>Frame count<input type="number" min={1} aria-label="Uploaded frame count" value={frameCount} onChange={(event) => setFrameCount(event.target.value)} /></label>}
    {error && <p className="field-error" role="alert">{error}</p>}
    <Button onClick={submit} disabled={!file || busy || (isPng && !frameCount)}>{busy ? 'Uploading…' : 'Add as candidate'}</Button>
  </div>
}

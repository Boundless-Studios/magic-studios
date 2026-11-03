import { memo, useEffect, useRef, useState } from 'react'
import { studioApi } from '../../api/client'
import { useAssetPreview } from '../../api/queries'
import type { MotionCandidate } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { SheetPlayer } from '../../components/SheetPlayer'
import { saveFromUrl } from '../../lib/saveFile'

/** A settled candidate's preview: a sprite sheet gets the animated `SheetPlayer`; anything else
 * (a decoded clip) gets a plain `<video>`. Shared between the rail's candidate thumbnails and the
 * Takes gallery cards -- neither needs the generation-in-progress affordances `AssetPreview`
 * carries for the stage. */
export const CandidateMedia = memo(function CandidateMedia({ projectId, candidate, name, compact = false }: { projectId: string; candidate: MotionCandidate; name: string; compact?: boolean }) {
  const preview = useAssetPreview(projectId, candidate.artifact_key)
  if (!preview.data) return <div className={compact ? 'candidate__preview' : 'take-card__preview'} role="img" aria-label={`${name} preview unavailable`}><small>{preview.isError ? 'Preview unavailable' : 'Loading preview'}</small></div>
  if (candidate.metadata?.media_kind === 'sheet') return <SheetPlayer className={compact ? 'candidate__media' : 'take-card__media'} name={name} imageUrl={preview.data.upload_url} candidate={candidate} />
  const assetUrl = new URL(preview.data.upload_url, window.location.href)
  const poster = assetUrl.pathname.startsWith('/local-assets/') ? `${assetUrl.pathname}?poster=true&v=${candidate.sha256}` : undefined
  // `src` is the cached `['asset-preview', projectId, artifactKey]` URL, so it is stable across
  // polls and the element is never remounted (memoised above so it is not even re-rendered).
  return <video className={compact ? 'candidate__media' : 'take-card__media'} aria-label={`${name} playback`} controls loop muted poster={poster} preload={compact ? 'metadata' : 'auto'} src={preview.data.upload_url}><track default kind="captions" src="data:text/vtt,WEBVTT" srcLang="en" /></video>
})

// T20: PNG · WebM · GIF, each resolved to a fresh signed read URL on click so the link
// is never stale by the time the browser follows it.
export function CandidateDownloadMenu({ projectId, motionId, candidateId }: { projectId: string; motionId: string; candidateId: string }) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ top: 0, left: 0 })
  const menuRef = useRef<HTMLDivElement>(null)
  const downloadTriggerRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!open) return
    const dismiss = (event: globalThis.PointerEvent) => { if (!menuRef.current?.contains(event.target as Node)) setOpen(false) }
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') { setOpen(false); downloadTriggerRef.current?.focus() } }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape) }
  }, [open])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  async function download(label: string, fetchTarget: () => Promise<{ upload_url: string }>) {
    setBusy(label); setError(null)
    try {
      saveFromUrl((await fetchTarget()).upload_url)
      if (menuRef.current?.contains(document.activeElement)) downloadTriggerRef.current?.focus()
      setOpen(false)
    } catch (failure) { setError(failure instanceof Error ? failure.message : 'Download failed. Try again.') }
    finally { setBusy(null) }
  }
  return <div className="candidate__downloads" ref={menuRef}>
    <button className="button button--secondary" type="button" ref={downloadTriggerRef} aria-label="Download" title="Download animation" onClick={(event) => { const rect = event.currentTarget.getBoundingClientRect(); setPosition({ top: Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - 168)), left: Math.max(8, Math.min(rect.left, window.innerWidth - 300)) }); setOpen((value) => !value); setError(null) }} aria-expanded={open}>↓</button>
    {open && <div className="candidate__download-panel" style={position} role="group" aria-label="Download format">
      <Button disabled={busy !== null} onClick={() => download('PNG', () => studioApi.candidateSheetDownload(projectId, motionId, candidateId))}>{busy === 'PNG' ? 'Preparing…' : 'PNG'}</Button>
      <Button disabled={busy !== null} onClick={() => download('WebM', () => studioApi.candidatePreviewDownload(projectId, motionId, candidateId, 'webm'))}>{busy === 'WebM' ? 'Preparing…' : 'WebM'}</Button>
      <Button disabled={busy !== null} onClick={() => download('GIF', () => studioApi.candidatePreviewDownload(projectId, motionId, candidateId, 'gif'))}>{busy === 'GIF' ? 'Preparing…' : 'GIF'}</Button>
      {error && <p role="alert" className="field-error">{error}</p>}
    </div>}
  </div>
}

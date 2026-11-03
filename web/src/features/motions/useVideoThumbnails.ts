import { useEffect, useState } from 'react'
import { studioApi } from '../../api/client'

interface FramePreviews { url: string; frames: string[]; poses: number[][]; duration: number; failed: boolean }
interface SourceCandidate { projectId: string; motionId: string; candidateId: string }
const localPreviewCache = new Map<string, FramePreviews>()

/** Use native server frames when the candidate is known; retain browser extraction for other clips. */
export function useVideoThumbnails(url: string, count: number, enabled: boolean, source?: SourceCandidate) {
  const projectId = source?.projectId
  const motionId = source?.motionId
  const candidateId = source?.candidateId
  const cacheKey = `${url}:${count}:${candidateId ?? ''}`
  const [result, setResult] = useState<FramePreviews>(() => localPreviewCache.get(cacheKey) ?? { url: '', frames: [], poses: [], duration: 0, failed: false })
  useEffect(() => {
    if (!enabled || count < 1) return
    const cached = localPreviewCache.get(cacheKey)
    if (cached) { setResult(cached); return }
    if (projectId && motionId && candidateId) {
      let cancelled = false
      const load = async () => {
        const frames: string[] = []
        const poses: number[][] = []
        let duration = 0
        for (let start = 0; start < count; start += 32) {
          const page = await studioApi.candidateFrames(projectId, motionId, candidateId, start, Math.min(32, count - start))
          if (cancelled) return
          if (page.frame_count !== count || page.index_space !== 'stored_source_frames' || page.frames.some((frame, offset) => frame.index !== start + offset)) throw new Error('Incomplete native frame extraction')
          duration = page.duration_ms / 1000
          for (const frame of page.frames) { frames.push(frame.image); poses.push(frame.pose) }
          setResult({ url, frames: [...frames], poses: [...poses], duration, failed: false })
        }
        if (frames.length !== count || !Number.isFinite(duration) || duration <= 0) throw new Error('Incomplete native frame extraction')
        const complete = { url, frames, poses, duration, failed: false }
        if (localPreviewCache.size >= 2) localPreviewCache.delete(localPreviewCache.keys().next().value!)
        localPreviewCache.set(cacheKey, complete)
        if (!cancelled) setResult(complete)
      }
      void load().catch(() => { if (!cancelled) setResult({ url, frames: [], poses: [], duration: 0, failed: true }) })
      return () => { cancelled = true }
    }
    // Local previews use the same FFmpeg extraction as saved sprites. Browser VP9-alpha
    // seeking can stop after frame one on Windows even when metadata loads successfully.
    if (new URL(url, window.location.href).pathname.startsWith('/local-assets/')) {
      const controller = new AbortController()
      const endpoint = new URL(url, window.location.href)
      endpoint.searchParams.set('frame_previews', String(count))
      void fetch(endpoint, { signal: controller.signal }).then(async (response) => {
        if (!response.ok) throw new Error('Frame extraction failed')
        const data = await response.json() as { frames: string[]; poses: number[][]; duration: number }
        if (data.frames.length !== count || data.poses.length !== count || !Number.isFinite(data.duration) || data.duration <= 0) throw new Error('Incomplete frame extraction')
        if (!controller.signal.aborted) {
          const next = { url, ...data, failed: false }
          if (localPreviewCache.size >= 2) localPreviewCache.delete(localPreviewCache.keys().next().value!)
          localPreviewCache.set(cacheKey, next)
          setResult(next)
        }
      }).catch(() => {
        if (!controller.signal.aborted) setResult({ url, frames: [], poses: [], duration: 0, failed: true })
      })
      return () => controller.abort()
    }
    const video = document.createElement('video')
    video.muted = true
    video.playsInline = true
    video.crossOrigin = 'anonymous'
    video.preload = 'auto'
    video.style.cssText = 'position:fixed;left:-10000px;top:0;width:512px;height:512px;pointer-events:none'
    video.setAttribute('aria-hidden', 'true')
    document.body.appendChild(video)
    const canvas = document.createElement('canvas')
    let cancelled = false
    const controller = new AbortController()
    let objectUrl: string | undefined
    let index = 0
    let initialized = false
    const frames: string[] = []
    const poses: number[][] = []
    const fail = () => { if (!cancelled) setResult({ url, frames: [...frames], poses: [...poses], duration: video.duration, failed: true }) }
    const seek = () => {
      if (cancelled) return
      // Sample inside each source frame, never at the end-of-stream boundary.
      video.currentTime = (index + 0.25) * video.duration / count
    }
    const capture = () => {
      if (cancelled) return
      try {
        const context = canvas.getContext('2d')
        if (!context) { fail(); return }
        context.clearRect(0, 0, canvas.width, canvas.height)
        context.drawImage(video, 0, 0, canvas.width, canvas.height)
        frames.push(canvas.toDataURL('image/png'))
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data
        const pose: number[] = []
        // Spatial color signature; lower-body rows receive twice the weight for walking.
        for (let y = 0; y < 16; y += 1) for (let x = 0; x < 16; x += 1) {
          const offset = (Math.floor((y + 0.5) * canvas.height / 16) * canvas.width + Math.floor((x + 0.5) * canvas.width / 16)) * 4
          for (let c = 0; c < 3; c += 1) pose.push(pixels[offset + c] / 255 * pixels[offset + 3] / 255 * (y >= 8 ? 2 : 1))
        }
        poses.push(pose)
        setResult({ url, frames: [...frames], poses: [...poses], duration: video.duration, failed: false })
        index += 1
        if (index < count) seek()
      } catch { fail() }
    }
    const sought = () => {
      if (cancelled) return
      if (!initialized) { ready(); return }
      window.setTimeout(capture, 35)
    }
    const ready = () => {
      if (!Number.isFinite(video.duration)) { video.currentTime = 1e10; return }
      if (video.duration <= 0) { fail(); return }
      initialized = true
      const scale = Math.min(512, Math.max(video.videoWidth, video.videoHeight, 1)) / Math.max(video.videoWidth, video.videoHeight, 1)
      canvas.width = Math.max(1, Math.round(video.videoWidth * scale))
      canvas.height = Math.max(1, Math.round(video.videoHeight * scale))
      seek()
    }
    video.addEventListener('loadeddata', ready, { once: true })
    video.addEventListener('seeked', sought)
    video.addEventListener('error', fail)
    // Materialize once: local/signed asset servers may not support byte-range seeking.
    void fetch(url, { signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error('Video download failed')
      const blob = await response.blob()
      if (cancelled) return
      objectUrl = URL.createObjectURL(blob)
      video.src = objectUrl
    }).catch(() => { if (!cancelled) fail() })
    return () => {
      cancelled = true
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
      video.removeEventListener('loadeddata', ready)
      video.removeEventListener('seeked', sought)
      video.removeEventListener('error', fail)
      video.removeAttribute('src')
      video.load()
      video.remove()
    }
  }, [url, count, enabled, cacheKey, projectId, motionId, candidateId])
  return result.url === url ? result : { frames: [], poses: [], duration: 0, failed: false }
}

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { LoopPolicy, MotionCandidate, MotionPreset, NormalizationSpec } from '../../api/types'
import { studioApi } from '../../api/client'
import { Button } from '../../components/ui/Button'
import {
  CELL_SIZE_CHOICES,
  TARGET_FRAME_COUNT_CHOICES,
  clampFps,
  createFrameDraft,
  draftDurationMs,
  frameDraftStorageKey,
  frameDurationMsFromFps,
  frameDurationMsFromTotalDuration,
  fpsFromFrameDurationMs,
  holdForFrame,
  isFrameDraftDirty,
  loadFrameDraft,
  moveFrameTo,
  saveFrameDraft,
  setFrameHold,
  setPacking,
  toggleFrame,
  applyCandidateTiming,
  validateFrameDraft,
  type FrameDraft,
} from './frameDraft'
import { sampleLoop, suggestWalkLoop } from './walkLoop'
import { sampleAttack } from './attackLoop'
import { matchWalkBody } from './bodyCalibration'
import { useVideoThumbnails } from './useVideoThumbnails'
import { containFrameBox, frameNaturalSize, stageFrameStyle } from './frameStage'

interface FrameEditorProps {
  projectId: string
  motionId: string
  motionName: string
  motionPreset?: MotionPreset | null
  candidate: MotionCandidate
  previewUrl: string
  walkReference?: MotionCandidate
  walkReferenceUrl?: string
  fallbackLoop: LoopPolicy
  onSave: (draft: FrameDraft) => Promise<void>
  onDeriveAlpha?: () => void
  derivingAlpha?: boolean
  /** Optional generation and upload controls below the current animation settings. */
  settingsRail?: ReactNode
}

export function FrameEditor({ projectId, motionId, motionName, motionPreset, candidate, previewUrl, walkReference, walkReferenceUrl, fallbackLoop, onSave, onDeriveAlpha, derivingAlpha = false, settingsRail }: FrameEditorProps) {
  const frameCount = candidate.metadata?.frame_count ?? 0
  const isIdle = motionPreset === 'idle'
  const canSuggestLoop = ['walk', 'run', 'idle', 'attack'].includes(motionPreset ?? motionName.trim().toLowerCase())
  const isAttack = motionPreset === 'attack' || (!motionPreset && motionName.trim().toLowerCase() === 'attack')
  const storageKey = frameDraftStorageKey(projectId, motionId, candidate.candidate_id)
  const [draft, setDraft] = useState(() => loadFrameDraft(storageKey, candidate, fallbackLoop))
  const [currentFrame, setCurrentFrame] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const selectionStart = useRef<number | null>(null)
  const dragged = useRef(false)
  const selectionOrder = useRef<number[]>([])
  const [announcement, setAnnouncement] = useState('')
  const videoRef = useRef<HTMLVideoElement>(null)
  const currentFrameRef = useRef(currentFrame)
  const stageFrameRef = useRef<HTMLDivElement>(null)
  const [stageContainerSize, setStageContainerSize] = useState({ width: 0, height: 0 })
  // Decide by what was actually measured, not by the artifact key's extension: a provider can
  // hand back a PNG sprite sheet under a job's .mp4 output key (A3).
  const videoCandidate = candidate.metadata?.media_kind
    ? candidate.metadata.media_kind === 'video'
    : /\.(mp4|webm|mov)(?:$|\?)/i.test(candidate.artifact_key) && !/[?&]media=image(?:&|$)/.test(previewUrl)
  const thumbnails = useVideoThumbnails(previewUrl, frameCount, videoCandidate, { projectId, motionId, candidateId: candidate.candidate_id })
  const [videoDuration, setSourceDuration] = useState(0)
  const sourceDuration = thumbnails.duration || (Number.isFinite(videoDuration) ? videoDuration : 0)
  const [loopStart, setLoopStart] = useState(draft.loopRange?.start ?? 0)
  const [loopEnd, setLoopEnd] = useState(draft.loopRange?.end ?? Math.max(0, frameCount - 1))
  const [loopCount, setLoopCount] = useState(draft.targetFrameCount && [8, 12, 16].includes(draft.targetFrameCount) ? draft.targetFrameCount : 16)
  const [hasLoop, setHasLoop] = useState(!!draft.loopRange)
  const [loopMessage, setLoopMessage] = useState('')
  const [loopRequested, setLoopRequested] = useState(false)
  const [verifiedAttackRange, setVerifiedAttackRange] = useState<{ start: number; end: number } | null>(null)
  const [showSelectedFrames, setShowSelectedFrames] = useState(!!draft.loopRange)
  const [calibration, setCalibration] = useState<NormalizationSpec | null>(null)
  const lastAutoCalibration = useRef<NormalizationSpec | null>(null)
  const calibrationFrame = draft.loopRange?.start ?? loopStart
  const calibrationThumbnail = thumbnails.frames[calibrationFrame]
  const activeNormalization = draft.normalization === undefined ? calibration : draft.normalization
  const errors = validateFrameDraft(draft, frameCount)
  const loopNeedsPreview = videoCandidate && hasLoop && (draft.loopRange?.start !== loopStart || draft.loopRange?.end !== loopEnd)
  const dirty = isFrameDraftDirty(draft, candidate, fallbackLoop)
  const included = useMemo(() => new Set(draft.orderedFrames), [draft.orderedFrames])
  const totalHold = draft.orderedFrames.reduce((sum, frameIndex) => sum + holdForFrame(draft, frameIndex), 0)
  const frameNatural = useMemo(() => frameNaturalSize(candidate), [candidate])
  const normalizationPreviewScale = 128 / (draft.cellSize ?? 256)
  const stageBox = useMemo(
    () => containFrameBox(stageContainerSize.width, stageContainerSize.height, frameNatural.width, frameNatural.height),
    [stageContainerSize, frameNatural],
  )

  useEffect(() => { saveFrameDraft(storageKey, draft) }, [draft, storageKey])
  useEffect(() => {
    if (!candidate.metadata?.has_alpha || !walkReferenceUrl || !walkReference?.metadata || walkReference.metadata.media_kind !== 'sheet' || !calibrationThumbnail) return
    let active = true
    void matchWalkBody(calibrationThumbnail, walkReferenceUrl, walkReference.metadata.frame_count).then((measured) => {
      if (!active) return
      const result = measured ? { ...measured, cleanup_green_edges: candidate.recipe?.op === 'derive_alpha', reference_candidate_id: walkReference.candidate_id, reference_sha256: walkReference.sha256 } : null
      setCalibration(result)
      if (result) {
        const previousAutomatic = lastAutoCalibration.current
        lastAutoCalibration.current = result
        setDraft((current) => current.loopRange && (current.normalization === undefined || (previousAutomatic !== null && JSON.stringify(current.normalization) === JSON.stringify(previousAutomatic))) ? { ...current, normalization: result } : current)
      }
    })
    return () => { active = false }
  }, [isAttack, candidate.metadata?.has_alpha, candidate.recipe?.op, walkReferenceUrl, walkReference?.metadata, walkReference?.candidate_id, walkReference?.sha256, calibrationThumbnail])
  useEffect(() => { currentFrameRef.current = currentFrame }, [currentFrame])
  // Frame holds (T7): each included frame plays for `hold` ticks of frameDurationMs, not one.
  // Expand the playback sequence to the literal step-by-step order (a frame with hold=3 appears
  // three times in a row) so the timer honors the same timing the export will use.
  const expandedFrames = useMemo(() => {
    const sequence: number[] = []
    draft.orderedFrames.forEach((frameIndex) => {
      const hold = holdForFrame(draft, frameIndex)
      for (let step = 0; step < hold; step += 1) sequence.push(frameIndex)
    })
    return sequence
  }, [draft])
  // The stage box can't be sized by CSS alone (a background-image div has no intrinsic size to
  // derive from aspect-ratio): measure the container and compute the largest centered box that
  // contains one frame without cropping it (see frameStage.ts).
  useEffect(() => {
    const node = stageFrameRef.current
    if (!node || videoCandidate) return
    const measure = () => setStageContainerSize({ width: node.clientWidth, height: node.clientHeight })
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [videoCandidate])
  useEffect(() => {
    if (!videoCandidate || !videoRef.current || playing) return
    if (sourceDuration > 0) videoRef.current.currentTime = (currentFrame + 0.25) * sourceDuration / Math.max(1, frameCount)
  }, [currentFrame, sourceDuration, frameCount, videoCandidate, playing])
  useEffect(() => {
    if (!playing || expandedFrames.length === 0) return
    let position = expandedFrames.indexOf(currentFrameRef.current)
    if (position === -1) position = 0
    if (draft.loopPolicy === 'once' && position === expandedFrames.length - 1) { position = 0; setCurrentFrame(expandedFrames[0]) }
    const timer = window.setInterval(() => {
      if (draft.loopPolicy === 'once' && position >= expandedFrames.length - 1) { setPlaying(false); return }
      position = (position + 1) % expandedFrames.length
      setCurrentFrame(expandedFrames[position])
    }, draft.frameDurationMs)
    return () => window.clearInterval(timer)
  }, [draft.frameDurationMs, draft.loopPolicy, expandedFrames, playing])

  function applyLoop(start = loopStart, end = loopEnd, count = loopCount) {
    const complete = isAttack && verifiedAttackRange?.start === start && verifiedAttackRange.end === end
    const frames = isAttack ? sampleAttack(start, end, count, complete) : sampleLoop(start, end, count)
    if (!frames.length) { setLoopMessage('Choose a range with at least as many source frames as output frames.'); return }
    const ticks = end - start + (isAttack && !complete ? 1 : 0)
    const duration = sourceDuration > 0 ? ticks * sourceDuration * 1000 / frameCount : ticks * draft.frameDurationMs
    update({ ...draft, orderedFrames: frames, loopRange: { start, end }, frameHolds: {}, targetFrameCount: count, cellSize: draft.cellSize ?? 256, normalization: isAttack && candidate.metadata?.has_alpha ? (draft.normalization === undefined ? calibration ?? undefined : draft.normalization) : draft.normalization, frameDurationMs: Math.max(1, Math.round(duration / count)) })
    setCurrentFrame(frames[0]); setPlaying(true); setShowSelectedFrames(true); setHasLoop(true)
    setLoopMessage(isAttack
      ? complete
        ? `Previewing ${count} frames of one complete attack. Matching recovery pose excluded from the loop. Check the strike and seam before saving.`
        : `Previewing ${count} frames as a one-shot. No matching recovery was detected; this selection will not loop cleanly.`
      : `Previewing ${count} frames. Matching end pose excluded. ${isIdle ? 'Check the breathing cycle and stable support before saving.' : 'Check that both legs complete a stride before saving.'}`)
  }
  useEffect(() => {
    if (!loopRequested || isAttack || isIdle || videoCandidate) return
    if (thumbnails.failed) {
      setLoopMessage('Could not read the source frames. Close and reopen the editor to retry.')
      setLoopRequested(false)
      return
    }
    if (thumbnails.poses.length < frameCount || !Number.isFinite(sourceDuration) || sourceDuration <= 0) return
    const suggestion = suggestWalkLoop(thumbnails.poses, frameCount / sourceDuration, loopCount)
    setLoopRequested(false)
    if (!suggestion) {
      setLoopMessage('No clear repeating stride found. Set the start and matching end pose manually, then preview.')
      return
    }
    const { start, end } = suggestion
    const frames = sampleLoop(start, end, loopCount)
    const duration = (end - start) * sourceDuration * 1000 / frameCount
    setLoopStart(start); setLoopEnd(end)
    setDraft((current) => ({ ...current, orderedFrames: frames, loopRange: { start, end }, frameHolds: {}, targetFrameCount: loopCount, cellSize: current.cellSize ?? 256, normalization: isAttack && candidate.metadata?.has_alpha ? (current.normalization === undefined ? calibration ?? undefined : current.normalization) : current.normalization, frameDurationMs: Math.max(1, Math.round(duration / loopCount)), loopPolicy: 'loop' }))
    setSaveError(null); setCurrentFrame(start); setPlaying(true); setShowSelectedFrames(true); setHasLoop(true)
    setLoopMessage(`Suggested frames ${start + 1}–${end + 1}. Previewing ${loopCount} frames; matching end pose excluded. Check the full stride and loop seam before saving.`)
  }, [loopRequested, thumbnails.failed, thumbnails.poses, frameCount, sourceDuration, loopCount, isAttack, isIdle, videoCandidate, calibration, candidate.metadata?.has_alpha])
  function suggestLoop() {
    setLoopRequested(true)
    setLoopMessage('')
    if (!isAttack && !videoCandidate) return
    const mode = isAttack ? 'attack' : isIdle ? 'idle' : 'walk'
    void studioApi.suggestCandidateLoop(projectId, motionId, candidate.candidate_id, mode, loopCount as 8 | 12 | 16).then((suggestion) => {
      setLoopRequested(false)
      if (suggestion.status !== 'complete' || suggestion.start === null || suggestion.end === null || !suggestion.frame_duration_ms || suggestion.ordered_frames.length !== loopCount) {
        setVerifiedAttackRange(null)
        if (suggestion.start !== null && suggestion.end !== null) { setLoopStart(suggestion.start); setLoopEnd(suggestion.end) }
        setLoopMessage(isAttack ? `No complete attack and recovery found. ${suggestion.warnings.join(' ') || 'Inspect a one-shot range manually or generate another take.'}` : `No complete ${isIdle ? 'idle' : 'walk'} cycle found. ${suggestion.warnings.join(' ') || 'Set the start and matching end pose manually, then preview.'}`)
        return
      }
      const { start, end } = suggestion
      if (isAttack) setVerifiedAttackRange({ start, end }); setLoopStart(start); setLoopEnd(end)
      setDraft((current) => ({ ...current, orderedFrames: suggestion.ordered_frames, loopRange: { start, end }, frameHolds: {}, targetFrameCount: loopCount, cellSize: current.cellSize ?? 256, normalization: candidate.metadata?.has_alpha ? (current.normalization === undefined ? calibration ?? undefined : current.normalization) : current.normalization, frameDurationMs: suggestion.frame_duration_ms!, loopPolicy: suggestion.loop_policy }))
      setSaveError(null); setCurrentFrame(start); setPlaying(true); setShowSelectedFrames(true); setHasLoop(true)
      setLoopMessage(isAttack ? `Suggested attack frames ${start + 1}–${end + 1}. Review the strike, recovery, grounding, and playback speed before saving.` : `Suggested frames ${start + 1}–${end + 1}. Previewing ${loopCount} frames from native source analysis. ${isIdle ? 'Check subtle breathing, stable support, and the loop seam before saving.' : 'Check both legs, grounding, and the loop seam before saving.'} ${suggestion.warnings.join(' ')}`)
    }).catch(() => {
      setLoopRequested(false)
      setLoopMessage(isAttack ? 'Attack analysis unavailable. Set a one-shot range manually, then preview it.' : `${isIdle ? 'Idle' : 'Walk'} analysis unavailable. Set the start and matching end pose manually, then preview.`)
    })
  }
  function update(next: FrameDraft) { setDraft(next); setSaveError(null) }
  function updateNormalization(key: keyof NormalizationSpec, value: number) {
    if (!activeNormalization) return
    update({ ...draft, normalization: { ...activeNormalization, [key]: value }, targetFrameCount: draft.targetFrameCount ?? loopCount, cellSize: draft.cellSize ?? 256 })
  }
  function toggleNormalization(enabled: boolean) {
    const fallback = {
      scale: Math.min(8, (draft.cellSize ?? 256) / Math.max(frameNatural.width, frameNatural.height)),
      source_anchor_x: frameNatural.width / 2,
      source_anchor_y: frameNatural.height * 0.92,
      target_anchor_x: (draft.cellSize ?? 256) / 2,
      target_anchor_y: (draft.cellSize ?? 256) * 0.90625,
      padding: 8,
      cleanup_green_edges: candidate.recipe?.op === 'derive_alpha',
      ...(isAttack && walkReference ? { reference_candidate_id: walkReference.candidate_id, reference_sha256: walkReference.sha256 } : {}),
    }
    update({ ...draft, normalization: enabled ? calibration ?? fallback : null, targetFrameCount: draft.targetFrameCount ?? loopCount, cellSize: draft.cellSize ?? 256 })
  }
  function step(offset: number) {
    setPlaying(false)
    setCurrentFrame((frame) => {
      if (!showSelectedFrames) return Math.max(0, Math.min(frameCount - 1, frame + offset))
      const position = draft.orderedFrames.indexOf(frame)
      return draft.orderedFrames[Math.max(0, Math.min(draft.orderedFrames.length - 1, position + offset))] ?? frame
    })
  }
  function commitTotalDuration(rawValue: string) {
    const calculated = frameDurationMsFromTotalDuration(Number(rawValue), draft.targetFrameCount ?? (totalHold || 1))
    if (!calculated) return
    update({ ...draft, frameDurationMs: Math.max(1, Math.round(calculated)) })
  }
  function commitFps(rawValue: string) {
    const clamped = clampFps(Number(rawValue))
    const calculated = frameDurationMsFromFps(clamped)
    if (!calculated) return
    update({ ...draft, frameDurationMs: Math.max(1, Math.round(calculated)) })
  }
  function moveTo(frameIndex: number, targetPosition: number) {
    const bounded = Math.max(0, Math.min(targetPosition, draft.orderedFrames.length - 1))
    update(moveFrameTo(draft, frameIndex, bounded))
    setAnnouncement(`Frame ${frameIndex + 1} moved to position ${bounded + 1}`)
  }
  function handleFrameKeyDown(event: KeyboardEvent, frameIndex: number) {
    if (!event.altKey || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) return
    const position = draft.orderedFrames.indexOf(frameIndex)
    if (position === -1) return
    event.preventDefault()
    moveTo(frameIndex, position + (event.key === 'ArrowLeft' ? -1 : 1))
  }
  useEffect(() => {
    const finish = () => {
      if (selectionStart.current !== null && dragged.current) {
        setDraft((current) => ({ ...current, frameHolds: Object.fromEntries(Object.entries(current.frameHolds).filter(([index]) => current.orderedFrames.includes(Number(index)))) }))
      }
      selectionStart.current = null
    }
    window.addEventListener('pointerup', finish)
    window.addEventListener('pointercancel', finish)
    return () => {
      window.removeEventListener('pointerup', finish)
      window.removeEventListener('pointercancel', finish)
    }
  }, [])
  function selectThrough(frameIndex: number) {
    const start = selectionStart.current
    if (start === null || (start === frameIndex && !dragged.current)) return
    dragged.current = true
    const low = Math.min(start, frameIndex)
    const high = Math.max(start, frameIndex)
    // Filter the order captured at pointer-down, so shrinking and expanding the range
    // never destroys explicit reordering. Newly included frames use source position.
    let next = { ...draft, orderedFrames: selectionOrder.current.filter((index) => index >= low && index <= high) }
    for (let index = low; index <= high; index += 1) {
      if (!next.orderedFrames.includes(index)) next = toggleFrame(next, index)
    }
    update(next)
    setCurrentFrame(frameIndex)
  }
  async function save() {
    setSaving(true); setSaveError(null)
    try { await onSave(draft); sessionStorage.removeItem(storageKey) }
    catch { setSaveError('The revision could not be saved. Your draft is still here.') }
    finally { setSaving(false) }
  }
  function discard() {
    const clean = createFrameDraft(candidate, fallbackLoop)
    sessionStorage.removeItem(storageKey); setDraft(clean); setSaveError(null); setHasLoop(false); setShowSelectedFrames(false); setLoopStart(0); setLoopEnd(Math.max(0, frameCount - 1)); setLoopCount(16); setLoopMessage(''); setPlaying(false)
  }

  return <section className="frame-editor-shell" aria-label={`${motionName} frame editor`}>
    <div className="frame-editor__columns">
      <div className="frame-editor__main">
        <div className="frame-editor__stage-frame" ref={stageFrameRef}>
          {videoCandidate
            ? <video ref={videoRef} className="frame-editor__video" aria-label={`${motionName} frame preview`} muted playsInline preload="auto" onLoadedMetadata={(event) => setSourceDuration(event.currentTarget.duration)} src={previewUrl} style={{ transform: draft.flipHorizontal ? 'scaleX(-1)' : undefined }} />
            : <div className="frame-editor__stage" style={{ ...stageFrameStyle(previewUrl, currentFrame, frameCount, stageBox), transform: draft.flipHorizontal ? 'scaleX(-1)' : undefined }} role="img" aria-label={`${motionName}, frame ${currentFrame + 1}`} />}
          {videoCandidate && thumbnails.frames[currentFrame] && <img className="frame-editor__playback" src={thumbnails.frames[currentFrame]} alt={`${motionName} selected frame ${currentFrame + 1}`} style={{ transform: draft.flipHorizontal ? 'scaleX(-1)' : undefined }} />}
        </div>
        <div className="frame-editor__transport">
          <Button aria-label="Previous frame" title="Previous frame" onClick={() => step(-1)} disabled={showSelectedFrames ? draft.orderedFrames.indexOf(currentFrame) <= 0 : currentFrame <= 0}>⏮</Button>
          <Button aria-label={playing ? 'Pause' : 'Play'} title={playing ? 'Pause' : 'Play'} onClick={() => setPlaying((value) => !value)} disabled={draft.orderedFrames.length === 0 || (videoCandidate && thumbnails.frames.length < frameCount)}>{playing ? '⏸' : '▶'}</Button>
          <Button aria-label="Next frame" title="Next frame" onClick={() => step(1)} disabled={showSelectedFrames ? draft.orderedFrames.indexOf(currentFrame) >= draft.orderedFrames.length - 1 : currentFrame >= frameCount - 1}>⏭</Button>
          <small>{videoCandidate && thumbnails.frames.length < frameCount ? (thumbnails.failed ? 'Frame previews unavailable' : `Loading frames ${thumbnails.frames.length}/${frameCount}…`) : showSelectedFrames ? 'Click a frame to inspect' : 'Drag to select · Click to toggle'}</small>
          <strong role="status" aria-live="polite" aria-label="Playback position">Frame {currentFrame + 1} of {frameCount}</strong>
        </div>
        <input className="frame-editor__scrubber" aria-label="Current frame" type="range" min={0} max={Math.max(0, (showSelectedFrames ? draft.orderedFrames.length : frameCount) - 1)} value={showSelectedFrames ? Math.max(0, draft.orderedFrames.indexOf(currentFrame)) : currentFrame} onChange={(event) => { setPlaying(false); setCurrentFrame(showSelectedFrames ? draft.orderedFrames[Number(event.target.value)] : Number(event.target.value)) }} />
        <p className="sr-only" role="status" aria-live="polite" aria-label="Reorder announcement">{announcement}</p>
        <div className="frame-editor__timeline-controls" role="group" aria-label="Timeline view">
          <Button aria-pressed={showSelectedFrames} onClick={() => setShowSelectedFrames(true)}>Selected frames ({draft.orderedFrames.length})</Button>
          <Button aria-pressed={!showSelectedFrames} onClick={() => setShowSelectedFrames(false)}>Source frames ({frameCount})</Button>
        </div>
        <ol className="frame-timeline" aria-label="Animation frames">
          {(showSelectedFrames ? draft.orderedFrames : Array.from({ length: frameCount }, (_, index) => index)).map((frameIndex) => {
            return <li key={frameIndex} className={included.has(frameIndex) ? 'is-included' : 'is-excluded'}>
              <button type="button" className="frame-timeline__preview"
                aria-label={`${showSelectedFrames ? 'Preview' : included.has(frameIndex) ? 'Exclude' : 'Include'} frame ${frameIndex + 1}`}
                aria-pressed={included.has(frameIndex)} aria-current={currentFrame === frameIndex ? 'true' : undefined}
                onPointerDown={(event) => {
                  if (event.button !== 0) return
                  if (showSelectedFrames) { setPlaying(false); setCurrentFrame(frameIndex); return }
                  selectionStart.current = null; dragged.current = false
                  // Touch swipes scroll the strip; taps still toggle through onClick.
                  if (event.pointerType === 'touch') return
                  selectionStart.current = frameIndex
                  selectionOrder.current = [...draft.orderedFrames]
                  setPlaying(false); setCurrentFrame(frameIndex)
                }}
                onPointerEnter={() => selectThrough(frameIndex)}
                onClick={(event) => {
                  if (dragged.current && event.detail !== 0) return
                  setPlaying(false); setCurrentFrame(frameIndex); if (!showSelectedFrames) update(toggleFrame(draft, frameIndex))
                }}
                onKeyDown={(event) => handleFrameKeyDown(event, frameIndex)}>
                {videoCandidate
                  ? thumbnails.frames[frameIndex]
                    ? <img src={thumbnails.frames[frameIndex]} alt={`Source frame ${frameIndex + 1}`} draggable={false} style={{ transform: draft.flipHorizontal ? 'scaleX(-1)' : undefined }} />
                    : <span className="is-video-frame">{thumbnails.failed ? 'Unavailable' : 'Loading…'}</span>
                  : <span style={frameStyle(previewUrl, frameIndex, frameCount)} />}
                <b>{frameIndex + 1}{included.has(frameIndex) ? ' ✓' : ''}</b>
              </button>
            </li>
          })}
        </ol>
      </div>
      <div className="frame-editor__settings">
        {videoCandidate && <section className="frame-editor__workflow" aria-label={isAttack ? 'Attack extraction' : 'Loop extraction'}>
          <h3>{isAttack ? '1. Find one complete attack' : '1. Find a complete cycle'}</h3>
          {canSuggestLoop && <Button variant="primary" onClick={suggestLoop} disabled={loopRequested}>{isAttack ? 'Suggest attack loop' : isIdle ? 'Suggest idle loop' : 'Suggest walk loop'}</Button>}
          <p role="status" aria-live="polite" aria-label="Loop analysis status">{loopRequested ? `Preparing ${frameCount} source frames — the selection will preview automatically when ready.` : loopNeedsPreview ? `Boundaries changed. Click Preview selected ${isAttack ? 'attack' : 'loop'} to update the selected frames.` : loopMessage || (!canSuggestLoop ? 'Choose the start and end of this action manually, then preview the selected frames.' : isAttack ? 'Click Suggest attack loop to find one strike and recovery.' : isIdle ? 'Click Suggest idle loop to find a subtle repeating breathing cycle.' : 'Click Suggest walk loop to find matching poses and preview one stride.')}</p>
          <p>{!canSuggestLoop ? 'Choose a complete action. Playback can run once; no walk is needed for framing or pivot controls.' : isAttack ? 'Choose anticipation, strike, follow-through, and a return to the ready pose. A range without recovery plays once.' : isIdle ? 'Match a relaxed pose across one breathing cycle, keeping the feet and support stable. The matching end pose is excluded.' : 'Match the same pose one full stride apart. The matching end pose is excluded.'}</p>
          <div className="frame-editor__parameters">
            <label>Start frame<input aria-label={isAttack ? 'Attack start frame' : 'Loop start frame'} type="number" min={1} max={frameCount} value={loopStart + 1} onChange={(event) => { const frame = Math.max(0, Math.min(frameCount - 1, Number(event.target.value) - 1)); setLoopStart(frame); setCurrentFrame(frame); setPlaying(false) }} /></label>
            <label>{isAttack ? 'Recovery/end' : 'Matching end'}<input aria-label={isAttack ? 'Attack recovery frame' : 'Loop matching end frame'} type="number" min={1} max={frameCount} value={loopEnd + 1} onChange={(event) => { const frame = Math.max(0, Math.min(frameCount - 1, Number(event.target.value) - 1)); setLoopEnd(frame); setCurrentFrame(frame); setPlaying(false) }} /></label>
          </div>
          <h3>2. Sample and preview</h3>
          <label>{isAttack ? 'Attack frames' : 'Cycle frames'}<select aria-label={isAttack ? 'Attack output frames' : 'Loop output frames'} value={loopCount} onChange={(event) => { const count = Number(event.target.value); setLoopCount(count); if (hasLoop) applyLoop(loopStart, loopEnd, count) }}>{[8, 12, 16].map((count) => <option key={count} value={count}>{count} frames</option>)}</select></label>
          <Button onClick={() => applyLoop()} disabled={thumbnails.frames.length < frameCount}>{isAttack ? 'Preview selected attack' : 'Preview selected loop'}</Button>
        </section>}
        {<section className="frame-editor__workflow" aria-label="Body-scale reference">
          <h3>Match a comparable body reference</h3>
          {walkReferenceUrl && walkReference?.metadata?.media_kind === 'sheet' && thumbnails.frames[currentFrame] && <div className="body-calibration__comparison">
            <figure><div className="body-calibration__canvas"><div className="body-calibration__guide" style={{ backgroundImage: `url("${walkReferenceUrl}")`, backgroundSize: `${walkReference.metadata.frame_count * 100}% 100%`, backgroundPosition: 'left top' }} /></div><figcaption>Selected reference</figcaption></figure>
            <figure><div className="body-calibration__canvas"><div className="body-calibration__guide" />{activeNormalization && <img className="body-calibration__attack" src={thumbnails.frames[currentFrame]} alt="Attack at matched body scale" style={{ width: (candidate.metadata?.width ?? 512) * activeNormalization.scale * 0.4, height: (candidate.metadata?.height ?? 512) * activeNormalization.scale * 0.4, left: 48.8 + (activeNormalization.target_anchor_x - activeNormalization.source_anchor_x * activeNormalization.scale) * 0.4, top: 48.8 + (activeNormalization.target_anchor_y - activeNormalization.source_anchor_y * activeNormalization.scale) * 0.4 }} />}</div><figcaption>Current animation · current frame</figcaption></figure>
          </div>}
          {!candidate.metadata?.has_alpha && <p>Remove the green backdrop to enable fixed body scale and transparent output.</p>}
          {candidate.metadata?.has_alpha && !walkReferenceUrl && <p>Choose a comparable take from this view, or set framing and pivot manually below.</p>}
        </section>}
        {candidate.metadata?.has_alpha && <section className="frame-editor__workflow" aria-label="Framing and pivot">
          <h3>Framing and pivot</h3><p>{activeNormalization?.reference_candidate_id ? 'Comparable reference selected. Confirm its support point visually.' : activeNormalization ? 'Manual fixed transform · pivot registration unverified.' : 'No normalization or pivot registration selected.'}</p>
          <p>Use the feet or support point as the anchor, rather than moving weapon bounds. One fixed scale and origin apply to every frame; exported cells expand together to preserve overhang.</p>
          <label className="checkbox-field"><input type="checkbox" aria-label={isAttack && walkReferenceUrl ? 'Match selected walk body scale' : 'Set framing and pivot'} checked={!!activeNormalization} onChange={(event) => toggleNormalization(event.target.checked)} /> {isAttack && walkReferenceUrl ? 'Match selected walk body scale' : 'Set framing and pivot'}</label>
          {activeNormalization && <>
            <div className="frame-editor__parameters">
              <label>Body scale<input aria-label={isAttack ? 'Attack body scale' : 'Body scale'} type="number" min="0.01" max="8" step="0.01" value={activeNormalization.scale} onChange={(event) => updateNormalization('scale', Number(event.target.value))} /></label>
              <label>Source anchor X<input aria-label={isAttack ? 'Attack source anchor X' : 'Source anchor X'} type="number" min="0" value={activeNormalization.source_anchor_x} onChange={(event) => updateNormalization('source_anchor_x', Number(event.target.value))} /></label>
              <label>Source anchor Y<input aria-label={isAttack ? 'Attack source anchor Y' : 'Source anchor Y'} type="number" min="0" value={activeNormalization.source_anchor_y} onChange={(event) => updateNormalization('source_anchor_y', Number(event.target.value))} /></label>
              <label>Target pivot X<input aria-label={isAttack ? 'Walk target anchor X' : 'Target pivot X'} type="number" min="0" value={activeNormalization.target_anchor_x} onChange={(event) => updateNormalization('target_anchor_x', Number(event.target.value))} /></label>
              <label>Target pivot Y<input aria-label={isAttack ? 'Walk target anchor Y' : 'Target pivot Y'} type="number" min="0" value={activeNormalization.target_anchor_y} onChange={(event) => updateNormalization('target_anchor_y', Number(event.target.value))} /></label>
              <label>Overhang padding<input aria-label="Overhang padding" type="number" min="0" max="256" value={activeNormalization.padding ?? 0} onChange={(event) => updateNormalization('padding', Number(event.target.value))} /></label>
            </div>
            <label className="checkbox-field"><input type="checkbox" aria-label="Clean green edges" checked={!!activeNormalization.cleanup_green_edges} onChange={(event) => update({ ...draft, normalization: { ...activeNormalization, cleanup_green_edges: event.target.checked }, targetFrameCount: draft.targetFrameCount ?? loopCount, cellSize: draft.cellSize ?? 256 })} /> Clean green edges</label>
            <div className="framing-previews" aria-label="Fixed framing previews">
              {(['light', 'dark'] as const).map((theme) => <figure key={theme}>
                <div className={`framing-preview framing-preview--${theme}`} role="img" aria-label={`${motionName} fixed framing on ${theme} background`}>
                  <div className="framing-preview__guide" style={{ width: (draft.cellSize ?? 256) * normalizationPreviewScale, height: (draft.cellSize ?? 256) * normalizationPreviewScale }} />
                  <div style={{ position: 'absolute', width: frameNatural.width * activeNormalization.scale * normalizationPreviewScale, height: frameNatural.height * activeNormalization.scale * normalizationPreviewScale, left: 36 + (activeNormalization.target_anchor_x - activeNormalization.source_anchor_x * activeNormalization.scale) * normalizationPreviewScale, top: 36 + (activeNormalization.target_anchor_y - activeNormalization.source_anchor_y * activeNormalization.scale) * normalizationPreviewScale, overflow: 'hidden', transform: draft.flipHorizontal ? 'scaleX(-1)' : undefined, ...(videoCandidate ? { backgroundImage: thumbnails.frames[currentFrame] ? `url("${thumbnails.frames[currentFrame]}")` : undefined, backgroundSize: '100% 100%' } : frameStyle(previewUrl, currentFrame, frameCount)) }} />
                  <span className="framing-preview__pivot" style={{ left: 36 + activeNormalization.target_anchor_x * normalizationPreviewScale, top: 36 + activeNormalization.target_anchor_y * normalizationPreviewScale }} />
                </div><figcaption>{theme === 'light' ? 'Light' : 'Dark'} · current frame</figcaption>
              </figure>)}
            </div>
            <p>Preview the complete animation and check its support point before saving. The cross marks the exported pivot.</p>
          </>}
        </section>}
        <section className="frame-editor__workflow" aria-label="Sprite output">
          <h3>{videoCandidate ? '3. Choose sprite size' : 'Sprite output'}</h3>
          <div className="frame-editor__parameters">
            {!videoCandidate && <label>Frame count<select aria-label="Frame count" value={draft.targetFrameCount ?? ''} onChange={(event) => update(setPacking(draft, event.target.value ? Number(event.target.value) : null, event.target.value ? (draft.cellSize ?? CELL_SIZE_CHOICES[1]) : null))}><option value="">Keep source frames</option>{TARGET_FRAME_COUNT_CHOICES.map((count) => <option key={count} value={count}>{count} frames</option>)}</select></label>}
            <label>Frame size<select aria-label="Cell size" value={draft.cellSize ?? ''} onChange={(event) => update(setPacking(draft, event.target.value ? (draft.targetFrameCount ?? (videoCandidate ? loopCount : TARGET_FRAME_COUNT_CHOICES.find((count) => count >= totalHold) ?? 32)) : null, event.target.value ? Number(event.target.value) : null))}><option value="">Keep source size</option>{CELL_SIZE_CHOICES.map((size) => <option key={size} value={size}>{size} × {size}</option>)}</select></label>
            <div><span>{draft.targetFrameCount ?? totalHold} output frames</span><strong>{(draftDurationMs(draft) / 1000).toFixed(2)} s</strong></div>
          </div>
          <p>{videoCandidate ? 'Cycle timing follows the source. Preview the loop before saving.' : 'Preview your selected frames before saving.'}</p>
        </section>
        <details className="frame-editor__advanced">
          <summary>More frame controls</summary>
          <div className="frame-editor__parameters">
          <label>Duration (ms)<input aria-label="Total duration (ms)" min="1" type="number" value={Math.round(draftDurationMs(draft))} onChange={(event) => commitTotalDuration(event.target.value)} /></label>
          <label>Playback FPS<input aria-label="Frames per second" min="1" max="60" type="number" value={Math.round(fpsFromFrameDurationMs(draft.frameDurationMs) ?? 1)} onChange={(event) => commitFps(event.target.value)} /></label>
          <label>Loop<select aria-label="Loop mode" value={draft.loopPolicy} onChange={(event) => update({ ...draft, loopPolicy: event.target.value as LoopPolicy })}><option value="loop">Loop</option><option value="once">Once</option><option value="ping_pong">Ping-pong</option></select></label>
          <label className="checkbox-field"><input type="checkbox" aria-label="Flip horizontally" checked={draft.flipHorizontal} onChange={(event) => update({ ...draft, flipHorizontal: event.target.checked })} /> Flip horizontally</label>
          <Button onClick={() => update(applyCandidateTiming(draft, candidate))}>Use candidate timing</Button>

            <label>Frame {currentFrame + 1} hold<input aria-label={`Hold for frame ${currentFrame + 1}`} type="number" min={1} max={8} disabled={!included.has(currentFrame)} value={holdForFrame(draft, currentFrame)} onChange={(event) => update(setFrameHold(draft, currentFrame, Number(event.target.value)))} /></label>
            <div className="frame-editor__reorder">
              <Button aria-label={`Move frame ${currentFrame + 1} earlier`} disabled={draft.orderedFrames.indexOf(currentFrame) <= 0} onClick={() => moveTo(currentFrame, draft.orderedFrames.indexOf(currentFrame) - 1)}>← Earlier</Button>
              <Button aria-label={`Move frame ${currentFrame + 1} later`} disabled={!included.has(currentFrame) || draft.orderedFrames.indexOf(currentFrame) >= draft.orderedFrames.length - 1} onClick={() => moveTo(currentFrame, draft.orderedFrames.indexOf(currentFrame) + 1)}>Later →</Button>
            </div>
          </div>
        </details>
        {settingsRail}
        {errors.length > 0 && <p className="field-error" role="alert">{errors[0]}</p>}
        {saveError && <p className="field-error" role="alert">{saveError}</p>}
      </div>
    </div>
    <footer className="frame-editor__actions">
      <strong>{dirty ? `Draft · ${draft.orderedFrames.length} of ${frameCount} frames` : `Saved · ${draft.orderedFrames.length} frames`}</strong>
      {onDeriveAlpha && candidate.metadata && !candidate.metadata.has_alpha && <Button onClick={onDeriveAlpha} disabled={derivingAlpha}>{derivingAlpha ? 'Removing green backdrop…' : 'Remove green backdrop'}</Button>}
      <Button onClick={discard} disabled={!dirty || saving}>Discard draft</Button>
      <Button variant="primary" onClick={save} disabled={!dirty || errors.length > 0 || saving || loopNeedsPreview || loopRequested}>{saving ? 'Saving revision' : 'Save revision'}</Button>
    </footer>
  </section>
}

function frameStyle(url: string, frameIndex: number, frameCount: number) {
  const position = frameCount <= 1 ? 0 : (frameIndex / (frameCount - 1)) * 100
  return { backgroundImage: `url("${url}")`, backgroundPosition: `${position}% center`, backgroundSize: `${Math.max(1, frameCount) * 100}% 100%` }
}

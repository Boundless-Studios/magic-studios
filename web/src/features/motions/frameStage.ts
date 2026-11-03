import type { MotionCandidate } from '../../api/types'

/**
 * The stage's letterbox math (object-fit: contain, computed by hand): given the stage-frame
 * container's measured pixel size and one frame's natural pixel size, returns the largest box
 * with the frame's aspect ratio that fits entirely inside the container -- CSS aspect-ratio
 * alone can't do this for a background-image div (it has no intrinsic size to derive width
 * from), so the pixel box has to be measured and computed explicitly, then applied as an
 * explicit width/height rather than left to CSS layout.
 */
export function containFrameBox(containerWidth: number, containerHeight: number, frameWidth: number, frameHeight: number): { width: number; height: number } {
  if (containerWidth <= 0 || containerHeight <= 0 || frameWidth <= 0 || frameHeight <= 0) return { width: 0, height: 0 }
  const scale = Math.min(containerWidth / frameWidth, containerHeight / frameHeight)
  return { width: frameWidth * scale, height: frameHeight * scale }
}

/** Measured pixels for one frame: a video uses its full width; a horizontal sheet divides
 * total width evenly across its frame_count columns. */
export function frameNaturalSize(candidate: MotionCandidate): { width: number; height: number } {
  const metadata = candidate.metadata
  if (!metadata || metadata.frame_count < 1) return { width: 1, height: 1 }
  return { width: metadata.media_kind === 'video' ? metadata.width : metadata.width / metadata.frame_count, height: metadata.height }
}

/**
 * The stage's inline style for one frame, drawn centered and fully contained in `box` (the
 * result of `containFrameBox`): the whole sheet is scaled so one frame equals `box`, then
 * positioned by pixel offset -- not the percentage sprite trick the compact strips use, which
 * only works when the element's own box already has the frame's aspect ratio.
 */
export function stageFrameStyle(url: string, frameIndex: number, frameCount: number, box: { width: number; height: number }): { backgroundImage: string; backgroundPosition: string; backgroundSize: string; backgroundRepeat: 'no-repeat'; width: number; height: number } {
  const count = Math.max(1, frameCount)
  return {
    backgroundImage: `url("${url}")`,
    backgroundPosition: `-${frameIndex * box.width}px 0`,
    backgroundSize: `${box.width * count}px ${box.height}px`,
    backgroundRepeat: 'no-repeat',
    width: box.width,
    height: box.height,
  }
}

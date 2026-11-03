/** The rail tile's fixed pixel size (matches `.motion-rail__thumb`'s 2.5rem at the default root
 * font size). A literal px size, not a percentage, so the frame-0 background math below is
 * exact instead of relying on percentage-of-element-size sprite math that silently degenerates
 * to a solid color tile when the element's own box can't be sized from its background alone. */
export const RAIL_THUMB_SIZE_PX = 40

/**
 * Frame 0 of a sprite sheet, scaled and cropped by pixel math (not CSS percentage sprite math):
 * `background-size: auto <tileSize>px` scales the whole sheet so its height matches the tile,
 * preserving the sheet's real aspect ratio; `background-position-x` is then the pixel offset of
 * frame 0 in that SCALED sheet (0, since frame 0 is always first), computed from the metadata's
 * real width/height/frame_count rather than assumed. Exported for unit coverage of the exact
 * background-image/size/position triple, independent of any live network fetch.
 */
export interface RailThumbFrameStyle { backgroundImage: string; backgroundSize: string; backgroundPosition: string; backgroundRepeat: 'no-repeat' }

/** One style object per artifact (the signed read URL carries the artifact key) + tile size. The
 * rail calls this inside render for every row, so without a cache six rows minted six fresh style
 * objects on every poll and every keystroke, remounting nothing but forcing the browser to
 * re-resolve each background. */
const styleCache = new Map<string, RailThumbFrameStyle>()

export function railThumbFrameStyle(imageUrl: string, metadata: { width: number; height: number; frame_count: number }, tileSize: number = RAIL_THUMB_SIZE_PX): RailThumbFrameStyle {
  const cacheKey = `${tileSize}|${metadata.width}x${metadata.height}x${metadata.frame_count}|${imageUrl}`
  const cached = styleCache.get(cacheKey)
  if (cached) return cached
  const scaledSheetWidth = metadata.height > 0 ? metadata.width * (tileSize / metadata.height) : 0
  const frameWidth = metadata.frame_count > 0 ? scaledSheetWidth / metadata.frame_count : 0
  const style: RailThumbFrameStyle = {
    backgroundImage: `url("${imageUrl}")`,
    backgroundSize: `auto ${tileSize}px`,
    backgroundPosition: `-${frameWidth * 0}px center`,
    backgroundRepeat: 'no-repeat',
  }
  // Signed read URLs rotate, so the key space grows slowly forever: drop the lot rather than leak.
  if (styleCache.size >= 256) styleCache.clear()
  styleCache.set(cacheKey, style)
  return style
}

/** Test seam: the number of distinct artifact/tile combinations decoded so far. */
export function railThumbStyleCacheSize(): number { return styleCache.size }

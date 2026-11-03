import type { NormalizationSpec } from '../../api/types'

interface BodyMeasure { height: number; hipX: number; feetY: number }

async function measureBody(url: string, frameCount: number): Promise<BodyMeasure | null> {
  try {
    const image = new Image()
    image.crossOrigin = 'anonymous'
    image.src = url
    await image.decode()
    const width = Math.floor(image.naturalWidth / frameCount)
    const height = image.naturalHeight
    if (!width || !height) return null
    const canvas = document.createElement('canvas')
    canvas.width = width; canvas.height = height
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context) return null
    context.drawImage(image, 0, 0, width, height, 0, 0, width, height)
    const pixels = context.getImageData(0, 0, width, height).data
    const left = Math.floor(width * 0.24)
    const right = Math.ceil(width * 0.76)
    let top = height; let feetY = -1
    for (let y = 0; y < height; y += 1) for (let x = left; x < right; x += 1) {
      if (pixels[(y * width + x) * 4 + 3] < 96) continue
      top = Math.min(top, y); feetY = Math.max(feetY, y)
    }
    if (feetY <= top) return null
    // At belt height, the body's median x is a stable origin despite separated feet or an axe.
    const rowStart = Math.round(top + (feetY - top) * 0.53)
    const rowEnd = Math.round(top + (feetY - top) * 0.68)
    const xs: number[] = []
    for (let y = rowStart; y <= rowEnd; y += 1) for (let x = left; x < right; x += 1) {
      if (pixels[(y * width + x) * 4 + 3] >= 96) xs.push(x)
    }
    xs.sort((a, b) => a - b)
    return { height: feetY - top + 1, hipX: xs[Math.floor(xs.length / 2)] ?? Math.round(width / 2), feetY }
  } catch { return null }
}

/** Measure central body height, excluding an outstretched weapon, from each first frame. */
export async function matchWalkBody(attackFrameUrl: string, walkSheetUrl: string, walkFrameCount: number): Promise<NormalizationSpec | null> {
  const [attack, walk] = await Promise.all([measureBody(attackFrameUrl, 1), measureBody(walkSheetUrl, walkFrameCount)])
  if (!attack || !walk) return null
  return {
    scale: Math.round(walk.height / attack.height * 1000) / 1000,
    source_anchor_x: attack.hipX,
    source_anchor_y: attack.feetY,
    target_anchor_x: walk.hipX,
    target_anchor_y: walk.feetY,
    padding: 8,
  }
}

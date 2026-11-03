import { expect, it } from 'vitest'
import { RAIL_THUMB_SIZE_PX, railThumbFrameStyle, railThumbStyleCacheSize } from './motionRailThumb'

const metadata = { width: 256, height: 64, frame_count: 4 }

/** Perf: the rail computes a thumbnail style inside render for every row, so during a six-motion
 * batch this ran six times per poll and once per keystroke. It must be computed once per artifact
 * and then handed back by reference. */
it('decodes a rail thumbnail once per artifact key and reuses the same style object', () => {
  const before = railThumbStyleCacheSize()
  const url = 'https://assets.invalid/cache-once.png'
  const first = railThumbFrameStyle(url, metadata)
  const second = railThumbFrameStyle(url, { ...metadata })
  const third = railThumbFrameStyle(url, { ...metadata }, RAIL_THUMB_SIZE_PX)

  expect(second).toBe(first)
  expect(third).toBe(first)
  expect(railThumbStyleCacheSize()).toBe(before + 1)
})

it('keys the cache on the artifact and the tile size, not on the artifact alone', () => {
  const url = 'https://assets.invalid/two-sizes.png'
  const rail = railThumbFrameStyle(url, metadata, 40)
  const strip = railThumbFrameStyle(url, metadata, 64)
  expect(strip).not.toBe(rail)
  expect(rail.backgroundSize).toBe('auto 40px')
  expect(strip.backgroundSize).toBe('auto 64px')
  // A different artifact never reuses another's style.
  expect(railThumbFrameStyle('https://assets.invalid/other.png', metadata, 40)).not.toBe(rail)
})

it('still computes frame 0 by real pixel math', () => {
  const style = railThumbFrameStyle('https://assets.invalid/pixel-math.png', metadata, 40)
  expect(style.backgroundImage).toBe('url("https://assets.invalid/pixel-math.png")')
  expect(style.backgroundSize).toBe('auto 40px')
  expect(style.backgroundPosition).toBe('-0px center')
  expect(style.backgroundRepeat).toBe('no-repeat')
})

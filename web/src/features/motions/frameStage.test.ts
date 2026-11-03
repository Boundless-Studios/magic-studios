import { expect, it } from 'vitest'
import { containFrameBox, frameNaturalSize, stageFrameStyle } from './frameStage'

it('contains a 64x64 frame inside a 486x160 box: 160x160, centered by the caller', () => {
  // Height is the binding constraint (160/64 = 2.5 < 486/64 = 7.59), so the frame is scaled to
  // 160x160 -- square, not stretched to fill the wide container.
  expect(containFrameBox(486, 160, 64, 64)).toEqual({ width: 160, height: 160 })
})

it('contains a wide frame when width is the binding constraint', () => {
  // 400x100 frame in a 300x300 box: 300/400=0.75 < 300/100=3, so width binds.
  expect(containFrameBox(300, 300, 400, 100)).toEqual({ width: 300, height: 75 })
})

it('returns a zero box for an unmeasured (0x0) container instead of NaN/Infinity', () => {
  expect(containFrameBox(0, 0, 64, 64)).toEqual({ width: 0, height: 0 })
})

it('reads one frame\'s natural pixel size from sheet metadata (total width / frame_count)', () => {
  const metadata = { decodable: true, media_kind: 'sheet' as const, frame_count: 4, width: 256, height: 64, constant_geometry: true, has_alpha: true, duplicate_terminal_frame: false, baseline_y: null, packing_estimate: null }
  expect(frameNaturalSize({ metadata } as never)).toEqual({ width: 64, height: 64 })
})

it('builds the stage background style from a computed box: whole sheet scaled so one frame equals the box', () => {
  const style = stageFrameStyle('https://assets.invalid/idle.png', 1, 4, { width: 160, height: 160 })
  expect(style.backgroundImage).toBe('url("https://assets.invalid/idle.png")')
  expect(style.width).toBe(160)
  expect(style.height).toBe(160)
  // Frame 1 of 4: offset by one frame-width (160px) into a sheet scaled to 4*160=640 wide.
  expect(style.backgroundPosition).toBe('-160px 0')
  expect(style.backgroundSize).toBe('640px 160px')
  expect(style.backgroundRepeat).toBe('no-repeat')
})

it('uses the complete measured video frame rather than dividing its width by frame count', () => {
  expect(frameNaturalSize({ metadata: { media_kind: 'video', width: 768, height: 512, frame_count: 65 } } as never)).toEqual({ width: 768, height: 512 })
})

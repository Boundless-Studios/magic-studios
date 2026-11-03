import { expect, it } from 'vitest'
import { sampleAttack } from './attackLoop'

it('excludes a server-confirmed matching seam and keeps the end of a one-shot', () => {
  expect(sampleAttack(10, 26, 8, true)).toEqual([10, 12, 14, 16, 18, 20, 22, 24])
  expect(sampleAttack(10, 26, 8, false).at(-1)).toBe(26)
  expect(sampleAttack(10, 16, 8, true)).toEqual([])
})

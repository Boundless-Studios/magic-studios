import { expect, it } from 'vitest'
import { sampleLoop, suggestWalkLoop } from './walkLoop'

it('samples a full cycle evenly without the repeated matching end pose', () => {
  expect(sampleLoop(10, 34, 8)).toEqual([10, 13, 16, 19, 22, 25, 28, 31])
  expect(sampleLoop(10, 34, 12)).toEqual(Array.from({ length: 12 }, (_, i) => 10 + 2 * i))
  expect(sampleLoop(10, 18, 16)).toEqual([])
})
it('suggests a repeating stride and rejects a static clip', () => {
  const poses = Array.from({ length: 65 }, (_, i) => [Math.cos(i * Math.PI / 12), Math.sin(i * Math.PI / 12)])
  const match = suggestWalkLoop(poses, 24)!
  expect(match).not.toBeNull()
  expect((match.end - match.start) % 24).toBe(0)
  expect(suggestWalkLoop(Array.from({ length: 65 }, () => [0.5, 0.5]), 24)).toBeNull()
})

import { expect, it } from 'vitest'
import type { TargetInfo } from '../../api/types'
import { MOTION_PRESETS, presetForTargetSlot, RECOMMENDED_PRESET_SET } from './motionPresets'

const TEST_TARGET: TargetInfo = {
  system: 'acme',
  display_name: 'Acme',
  entity_kinds: ['character', 'journey_traveller'],
  delivery_configured: true,
  slots: [
    { name: 'idle', accepts: ['idle'], winner: 'idle' },
    { name: 'move', accepts: ['walk'], winner: 'walk' },
    { name: 'attack', accepts: ['attack'], winner: 'attack' },
    { name: 'trip', accepts: ['hurt', 'trip'], winner: 'trip' },
    { name: 'guard', accepts: ['guard'], winner: 'guard' },
    { name: 'cast', accepts: ['cast'], winner: 'cast' },
  ],
}

it('lists all ten presets with a one-line description', () => {
  expect(MOTION_PRESETS).toHaveLength(10)
  for (const preset of MOTION_PRESETS) {
    expect(preset.description.length).toBeGreaterThan(0)
  }
})

it('mirrors the reference game\'s six as the recommended set, in slot-batch order', () => {
  expect(RECOMMENDED_PRESET_SET).toEqual(['idle', 'walk', 'attack', 'guard', 'cast', 'hurt'])
})

it('resolves a target slot onto the studio preset via TargetInfo.slots, not a hard-coded table', () => {
  expect(presetForTargetSlot(TEST_TARGET, 'move')).toBe('walk')
  expect(presetForTargetSlot(TEST_TARGET, 'trip')).toBe('trip')
  expect(presetForTargetSlot(TEST_TARGET, 'idle')).toBe('idle')
  expect(presetForTargetSlot(TEST_TARGET, 'not-a-real-slot')).toBeUndefined()
})

it('loops only idle/walk/run; everything else plays once', () => {
  for (const preset of MOTION_PRESETS) {
    const expected = ['idle', 'walk', 'run'].includes(preset.preset) ? 'loop' : 'once'
    expect(preset.loopPolicy).toBe(expected)
  }
})

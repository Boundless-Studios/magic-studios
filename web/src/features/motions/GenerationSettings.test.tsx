import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { useState } from 'react'
import { DEFAULT_MOTION_GENERATION_SETTINGS, type Motion } from '../../api/types'
import { GenerationSettings, type MotionSpecPatch } from './GenerationSettings'

const idle: Motion = { motion_id: 'idle', name: 'Idle', kind: 'preset', preset: 'idle', semantic_slot: 'idle', loop_policy: 'loop', required: true, prompt: 'Idle at rest', references: [], style: null, script: null, candidates: [], selected_candidate_id: null }

it('keeps core timing visible and exposes retained admin generation controls', async () => {
  const user = userEvent.setup()
  const onChange = vi.fn()
  function Harness() {
    const [value, setValue] = useState(DEFAULT_MOTION_GENERATION_SETTINGS)
    return <GenerationSettings value={value} onChange={(next) => { setValue(next); onChange(next) }} />
  }
  render(<Harness />)

  expect(screen.getByLabelText('Duration (seconds)')).toHaveValue(4)
  await user.click(screen.getByRole('button', { name: 'Advanced generation settings' }))
  expect(screen.getByLabelText('Preset')).toHaveValue('standard')
  expect(screen.getByLabelText('Steps')).toHaveValue(20)
  expect(screen.getByLabelText('Seed')).toHaveValue(42)
  expect(screen.getByLabelText('Width')).toHaveValue(512)
  expect(screen.getByLabelText('Height')).toHaveValue(512)
  await user.clear(screen.getByLabelText('Seed'))
  await user.type(screen.getByLabelText('Seed'), '123')
  expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ seed: 123 }))
})

it('seeds steps and size from the chosen preset unless the operator already edited them', async () => {
  const user = userEvent.setup()
  function Harness() {
    const [value, setValue] = useState(DEFAULT_MOTION_GENERATION_SETTINGS)
    return <GenerationSettings value={value} onChange={setValue} />
  }
  render(<Harness />)
  await user.click(screen.getByRole('button', { name: 'Advanced generation settings' }))

  await user.selectOptions(screen.getByLabelText('Preset'), 'high')
  expect(screen.getByLabelText('Steps')).toHaveValue(28)
  expect(screen.getByLabelText('Width')).toHaveValue(1344)
  expect(screen.getByLabelText('Height')).toHaveValue(768)

  // The operator hand-edits width; a later preset change must not clobber it.
  await user.clear(screen.getByLabelText('Width'))
  await user.type(screen.getByLabelText('Width'), '999')
  await user.selectOptions(screen.getByLabelText('Preset'), 'cinematic')
  expect(screen.getByLabelText('Steps')).toHaveValue(32)
  expect(screen.getByLabelText('Width')).toHaveValue(999)
  expect(screen.getByLabelText('Height')).toHaveValue(768)
})

it('resets preset dirty flags when the selected motion changes (T13)', async () => {
  const user = userEvent.setup()
  const motionA: Motion = { ...idle, motion_id: 'a' }
  const motionB: Motion = { ...idle, motion_id: 'b' }
  function Harness() {
    const [motion, setMotion] = useState<Motion>(motionA)
    const [value, setValue] = useState(DEFAULT_MOTION_GENERATION_SETTINGS)
    return <>
      <button onClick={() => { setMotion(motionB); setValue(DEFAULT_MOTION_GENERATION_SETTINGS) }}>Select B</button>
      <GenerationSettings value={value} onChange={setValue} motion={motion} />
    </>
  }
  render(<Harness />)
  await user.click(screen.getByRole('button', { name: 'Advanced generation settings' }))

  // Edit motion A's width directly, then switch to motion B: B never touched width, so its
  // later preset change must apply that preset's default width instead of carrying A's edit.
  await user.clear(screen.getByLabelText('Width'))
  await user.type(screen.getByLabelText('Width'), '999')
  await user.click(screen.getByRole('button', { name: 'Select B' }))
  await user.selectOptions(screen.getByLabelText('Preset'), 'high')

  expect(screen.getByLabelText('Width')).toHaveValue(1344)
  expect(screen.getByLabelText('Steps')).toHaveValue(28)
})

it('uploads a reference, adds it with a role, and persists a style and script (T17)', async () => {
  const user = userEvent.setup()
  const onPatchMotion = vi.fn()
  const onUploadReference = vi.fn().mockResolvedValue({ key: 'sources/studio/p/motion-references/r1.png', sha256: 'a'.repeat(64), role: 'context' })
  render(<GenerationSettings value={DEFAULT_MOTION_GENERATION_SETTINGS} onChange={() => {}} motion={idle} onUploadReference={onUploadReference} onPatchMotion={onPatchMotion} />)
  await user.click(screen.getByRole('button', { name: 'Advanced generation settings' }))

  const file = new File(['data'], 'ref.png', { type: 'image/png' })
  await user.upload(screen.getByLabelText('Add reference'), file)
  expect(onUploadReference).toHaveBeenCalledWith(file)
  expect(onPatchMotion).toHaveBeenCalledWith({ references: [{ key: 'sources/studio/p/motion-references/r1.png', sha256: 'a'.repeat(64), role: 'context' }] })

  await user.selectOptions(screen.getByLabelText('Style'), 'cyberpunk')
  expect(onPatchMotion).toHaveBeenLastCalledWith({ style: 'cyberpunk' })

  await user.type(screen.getByLabelText('Script'), 'x')
  expect(onPatchMotion).toHaveBeenLastCalledWith({ script: 'x' })
})

it('shows role chips for existing references and lets the operator remove one', async () => {
  const user = userEvent.setup()
  const onPatchMotion = vi.fn<(patch: MotionSpecPatch) => void>()
  const withReference: Motion = { ...idle, references: [{ key: 'sources/studio/p/motion-references/r1.png', sha256: 'a'.repeat(64), role: 'start' }] }
  render(<GenerationSettings value={DEFAULT_MOTION_GENERATION_SETTINGS} onChange={() => {}} motion={withReference} onPatchMotion={onPatchMotion} />)
  await user.click(screen.getByRole('button', { name: 'Advanced generation settings' }))

  expect(screen.getByLabelText('Role for r1.png')).toHaveValue('start')
  await user.click(screen.getByRole('button', { name: 'Remove reference r1.png' }))
  expect(onPatchMotion).toHaveBeenCalledWith({ references: [] })
})

it('shows the desktop default steps without exposing quality tiers', async () => {
  vi.stubEnv('VITE_MAGIC_DESKTOP_RECIPE', 'balanced')
  try {
    const user = userEvent.setup()
    render(<GenerationSettings value={{ ...DEFAULT_MOTION_GENERATION_SETTINGS, steps: 8, provider_parameters: { execution_profile: 'h3-w4a8-16gb-lightx8' } }} onChange={() => {}} />)
    expect(screen.queryByLabelText('Generation recipe')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Advanced generation settings' }))
    expect(screen.getByLabelText('Steps')).toHaveValue(8)
    expect(screen.getByLabelText('Steps')).toBeDisabled()
    expect(screen.queryByLabelText('Preset')).not.toBeInTheDocument()
  } finally {
    vi.unstubAllEnvs()
  }
})

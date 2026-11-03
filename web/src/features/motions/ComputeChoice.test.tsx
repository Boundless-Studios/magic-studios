import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { DEFAULT_MOTION_GENERATION_SETTINGS, type GenerationConfig, type MotionGenerationSettings } from '../../api/types'
import { ComputeChoice } from './ComputeChoice'
import { choiceOfSettings, choiceUsable, defaultComputeChoice, settingsForChoice } from './computeChoiceModel'

const hostedSettings: MotionGenerationSettings = {
  ...DEFAULT_MOTION_GENERATION_SETTINGS,
  provider_parameters: { execution_provider: 'higgsfield', higgsfield_model: 'kling-3-std' },
}
const models = [
  { model: 'kling-3-std', label: 'Kling 3.0 Standard', path: 'kling-video/v3.0/std/image-to-video', closed_loop: true, duration_seconds: { min: 3, max: 15 }, evidence: 'untested' },
  { model: 'seedance-2', label: 'Seedance 2.0', path: 'bytedance/seedance-2.0/image-to-video', closed_loop: true, duration_seconds: { min: 5, max: 15 }, evidence: 'untested' },
]
const machineSettings: MotionGenerationSettings = { ...DEFAULT_MOTION_GENERATION_SETTINGS, steps: 8, provider_parameters: { worker_id: 'w-1' } }

function config(overrides: Partial<GenerationConfig> = {}): GenerationConfig {
  return {
    selected: null, settings: null,
    providers: { higgsfield: { available: true, configured: true, key_hint: '…9876', models, settings: hostedSettings } },
    ...overrides,
  }
}

describe('compute choice', () => {
  it('prefers an available machine and falls back to a saved Higgsfield key', () => {
    expect(defaultComputeChoice(config({ settings: machineSettings }))).toBe('machine')
    expect(defaultComputeChoice(config())).toBe('kling-3-std')
    expect(defaultComputeChoice(config({ providers: { higgsfield: { available: true, configured: false } } }))).toBe('machine')
    expect(defaultComputeChoice(undefined)).toBe('machine')
  })

  it('builds Higgsfield settings from discovery and keeps duration inside the model range', () => {
    const settings = settingsForChoice(config(), 'seedance-2', machineSettings)

    expect(settings.provider_parameters).toEqual({ execution_provider: 'higgsfield', higgsfield_model: 'seedance-2' })
    expect(settings.duration_seconds).toBe(5)
    expect(settingsForChoice(config(), 'machine', machineSettings)).toBe(machineSettings)
    expect(settingsForChoice(config(), 'unknown-model', machineSettings)).toBe(machineSettings)
  })

  it('shows the route the settings will actually run on', () => {
    expect(choiceOfSettings(machineSettings)).toBe('machine')
    expect(choiceOfSettings({ ...hostedSettings, provider_parameters: { execution_provider: 'higgsfield', higgsfield_model: 'kling-3-pro' } })).toBe('kling-3-pro')
    expect(choiceOfSettings({ ...hostedSettings, provider_parameters: { execution_provider: 'higgsfield' } })).toBe('kling-3-std')
  })

  it('says why the attached machine cannot run motions and keeps Higgsfield offered', () => {
    const blocked = config({ workers: [{ worker_id: 'w-1', available: false, reason: 'Motions need a 16 GB card and this one has 12 GB' }] })
    render(<MemoryRouter><ComputeChoice config={blocked} value="machine" onChange={vi.fn()} /></MemoryRouter>)

    expect(screen.getByRole('option', { name: "Your GPU machine (can't run motions)" })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Higgsfield · Kling 3.0 Standard' })).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('Motions need a 16 GB card and this one has 12 GB.')
  })

  it('points a blocked machine without a key at the Higgsfield key', () => {
    const blocked = config({
      workers: [{ worker_id: 'w-1', available: false, reason: 'Motions need a 16 GB card and this one has 12 GB' }],
      providers: { higgsfield: { available: true, configured: false } },
    })
    render(<MemoryRouter><ComputeChoice config={blocked} value="machine" onChange={vi.fn()} /></MemoryRouter>)

    expect(screen.getByText(/Your machine can't run motions: Motions need a 16 GB card and this one has 12 GB\./)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add your Higgsfield API key' })).toBeInTheDocument()
  })

  it('says who is billed once Higgsfield is chosen', async () => {
    const onChange = vi.fn()
    const { rerender } = render(<MemoryRouter><ComputeChoice config={config()} value="machine" onChange={onChange} /></MemoryRouter>)

    await userEvent.selectOptions(screen.getByLabelText('Run on'), 'kling-3-std')
    expect(onChange).toHaveBeenCalledWith('kling-3-std')
    expect(screen.queryByText(/Charged to your Higgsfield account/)).not.toBeInTheDocument()

    rerender(<MemoryRouter><ComputeChoice config={config()} value="kling-3-std" onChange={onChange} /></MemoryRouter>)
    expect(screen.getByText(/Charged to your Higgsfield account \(key …9876\)/)).toBeInTheDocument()
  })

  it('never defaults a public account to a machine it cannot use', () => {
    const publicAccount = { tier: 'public' as const, house_compute: false, h3: false, own_machine: true }
    expect(defaultComputeChoice(config({ account: publicAccount, settings: machineSettings }))).toBe('kling-3-std')
    expect(defaultComputeChoice(config({ account: { ...publicAccount, h3: true }, settings: machineSettings }))).toBe('machine')
    expect(defaultComputeChoice(config({ account: { ...publicAccount, h3: true, own_machine: false } }))).toBe('kling-3-std')
    expect(defaultComputeChoice(config({ account: { ...publicAccount, tier: 'invited', house_compute: true, h3: true, own_machine: false }, settings: machineSettings }))).toBe('machine')
  })

  it('marks a route unusable when the account cannot run it, so Generate stays disabled', () => {
    const blocked = { tier: 'public' as const, house_compute: false, h3: false, own_machine: false }
    const noKey = config({ account: blocked, providers: { higgsfield: { available: true, configured: false } } })
    expect(choiceUsable(noKey, defaultComputeChoice(noKey))).toBe(false)
    expect(choiceUsable(config({ account: blocked, providers: { higgsfield: { available: true, configured: true } } }), 'kling-3-std')).toBe(true)
    expect(choiceUsable(config({}), 'machine')).toBe(true)
    expect(choiceUsable(undefined, 'machine')).toBe(true)
  })

  it('offers a public account only what it can run, and says how to unblock it', () => {
    const account = { tier: 'public' as const, house_compute: false, h3: true, own_machine: false }
    const { rerender } = render(<MemoryRouter><ComputeChoice config={config({ account })} value="kling-3-std" onChange={() => {}} /></MemoryRouter>)
    expect(screen.queryByRole('option', { name: /GPU machine/ })).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Higgsfield · Kling 3.0 Standard' })).toBeInTheDocument()

    rerender(<MemoryRouter><ComputeChoice config={config({ account, providers: { higgsfield: { available: true, configured: false } } })} value="machine" onChange={() => {}} /></MemoryRouter>)
    expect(screen.getByRole('link', { name: 'Add a Higgsfield key or connect a GPU' })).toHaveAttribute('href', '/account/machines')

    rerender(<MemoryRouter><ComputeChoice config={config({ account: { ...account, h3: false, own_machine: true }, providers: { higgsfield: { available: true, configured: false } } })} value="machine" onChange={() => {}} /></MemoryRouter>)
    expect(screen.getByText(/MiniMax H3 is not available for public accounts/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add a Higgsfield key' })).toHaveAttribute('href', '/account/machines')
  })

  it('points to the key page when no key is saved, and hides itself when keys are off', () => {
    const { rerender } = render(<MemoryRouter><ComputeChoice config={config({ providers: { higgsfield: { available: true, configured: false } } })} value="machine" onChange={() => {}} /></MemoryRouter>)
    expect(screen.getByRole('link', { name: 'Add your Higgsfield API key' })).toHaveAttribute('href', '/account/machines')

    rerender(<MemoryRouter><ComputeChoice config={config({ providers: { higgsfield: { available: false, configured: false } } })} value="machine" onChange={() => {}} /></MemoryRouter>)
    expect(screen.queryByLabelText('Run on')).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
})

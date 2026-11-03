import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ArtFirstOnboarding } from './ArtFirstOnboarding'
import { artworkNeeded, recommendedSetup } from './setupModel'
import { studioApi } from '../../api/client'
import { projectFixture } from '../../test/studioTestUtils'

beforeEach(() => { URL.createObjectURL = vi.fn(() => 'blob:art'); URL.revokeObjectURL = vi.fn() })
afterEach(() => vi.restoreAllMocks())
async function start() {
  const user = userEvent.setup(); render(<MemoryRouter><ArtFirstOnboarding onMovie={() => {}} /></MemoryRouter>)
  await user.upload(screen.getByLabelText('Choose character image'), new File(['image'], 'Moon-Ranger.png', { type: 'image/png' }))
  expect(screen.getByLabelText('Character / project name')).toHaveValue('Moon Ranger')
  await user.click(screen.getByRole('button', { name: 'Continue' })); return user
}
describe('art-first setup', () => {
  it('shows the account menu it is given, so a new account can reach it before its first project', () => {
    render(<MemoryRouter><ArtFirstOnboarding account={<button type="button">admin@example.com</button>} onMovie={() => {}} /></MemoryRouter>)
    expect(screen.getByRole('banner')).toContainElement(screen.getByRole('button', { name: 'admin@example.com' }))
  })

  it('keeps neutral facing, permits Idle + Death and uploads only once without generation', async () => {
    vi.spyOn(studioApi, 'setupCapabilities').mockResolvedValue({ schema_versions: [1] })
    const create = vi.spyOn(studioApi, 'createProject').mockResolvedValue(projectFixture)
    const stage = vi.spyOn(studioApi, 'stageSetupReference').mockResolvedValue({ revision_id: 'art-1', key: 'art', sha256: 'hash', width: 512, height: 512 })
    const preview = vi.spyOn(studioApi, 'previewSetup').mockImplementation(async (_id, revision, desired) => ({ plan_token: 'p', expected_revision: revision, desired_setup: desired, impacts: [], blocked_operations: [] }))
    vi.spyOn(studioApi, 'applySetup').mockResolvedValue(projectFixture)
    const generate = vi.spyOn(studioApi, 'generateMotion')
    const user = await start(); await user.click(screen.getByRole('button', { name: 'Continue' })); expect(screen.getByRole('heading', { name: 'Original view' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(screen.getByRole('checkbox', { name: /Walk/ })); await user.click(screen.getByRole('checkbox', { name: /Death/ }))
    await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: 'Create workspace' }))
    expect(create).toHaveBeenCalledOnce(); expect(stage).toHaveBeenCalledOnce(); expect(generate).not.toHaveBeenCalled()
    expect(preview.mock.calls[0][2]).toMatchObject({ sprite_layout: 'single_view', identity_reference: { revision_id: 'art-1' }, views: [{ view_id: 'single', heading: null, reference: { revision_id: 'art-1' }, motions: [{ preset: 'idle' }, { preset: 'death' }] }] })
  })
  it('requires explicit compass facing and lets people edit each recommendation', async () => {
    const user = await start(); await user.click(screen.getByRole('button', { name: /Eight-direction/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled(); await user.click(screen.getByRole('button', { name: /South · toward you/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getAllByRole('checkbox', { name: /Walk/ }).filter((element) => (element as HTMLInputElement).checked)).toHaveLength(8)
    expect(screen.getAllByRole('checkbox', { name: /Attack/ }).filter((element) => (element as HTMLInputElement).checked)).toHaveLength(4)
    expect(screen.getAllByRole('checkbox', { name: /Death/ }).filter((element) => (element as HTMLInputElement).checked)).toHaveLength(0)
    const south = screen.getByRole('group', { name: /South · toward you/ }); expect(within(south).getByRole('checkbox', { name: /Idle/ })).toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Clear all suggestions' })); expect(screen.getAllByRole('checkbox').every((element) => !(element as HTMLInputElement).checked)).toBe(true)
  })
  it('starts with only the supplied heading when just this image is chosen', async () => {
    const user = await start(); await user.click(screen.getByRole('button', { name: /Eight-direction/ })); await user.click(screen.getByRole('checkbox', { name: /Start with just this image/ })); await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: /Northeast/ })); await user.click(screen.getByRole('button', { name: 'Continue' })); expect(screen.getAllByRole('group')).toHaveLength(1); expect(screen.getByRole('group', { name: /Northeast/ })).toBeVisible(); expect(screen.getAllByRole('checkbox').every((item) => !(item as HTMLInputElement).checked)).toBe(true)
    await user.click(screen.getByRole('button', { name: 'Remove direction · Northeast' })); expect(screen.queryByRole('group')).not.toBeInTheDocument(); await user.click(screen.getByRole('button', { name: 'Continue' })); expect(screen.getByText(/supplied direction was removed/)).toBeVisible()
    const desired = recommendedSetup('eight', 'NE', 'art', true); expect(desired.views).toHaveLength(1); expect(desired.views[0]).toMatchObject({ heading: 'NE', reference: { revision_id: 'art' }, motions: [] })
  })
  it('allows replacing rejected artwork and reuses the empty project on retry', async () => {
    vi.spyOn(studioApi, 'setupCapabilities').mockResolvedValue({ schema_versions: [1] }); const create = vi.spyOn(studioApi, 'createProject').mockResolvedValue(projectFixture); const stage = vi.spyOn(studioApi, 'stageSetupReference').mockRejectedValueOnce(new Error('Image is too large')).mockResolvedValue({ revision_id: 'valid-art', key: 'art', sha256: 'hash', width: 512, height: 512 }); vi.spyOn(studioApi, 'previewSetup').mockImplementation(async (_id, revision, desired) => ({ plan_token: 'p', expected_revision: revision, desired_setup: desired, impacts: [], blocked_operations: [] })); vi.spyOn(studioApi, 'applySetup').mockResolvedValue(projectFixture)
    const user = await start(); for (let index = 0; index < 3; index++) await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: 'Create workspace' })); expect(await screen.findByRole('alert')).toHaveTextContent('Image is too large'); expect(screen.getByLabelText('Replace artwork')).toBeEnabled(); await user.upload(screen.getByLabelText('Replace artwork'), new File(['valid'], 'valid.png', { type: 'image/png' })); await user.click(screen.getByRole('button', { name: 'Create workspace' })); expect(create).toHaveBeenCalledOnce(); expect(stage).toHaveBeenCalledTimes(2); expect(stage.mock.calls[1][1].name).toBe('valid.png')
  })
  it('says up front how much artwork each template needs, and how much is still missing at review', async () => {
    const user = await start()
    expect(screen.getByRole('button', { name: /Single direction.*Uses only this image/ })).toBeVisible()
    expect(screen.getByRole('button', { name: /Eight-direction.*Needs art for 8 directions/ })).toBeVisible()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Eight-direction/ }))
    expect(screen.getByRole('note')).toHaveTextContent('This image covers one of the 8')
    await user.click(screen.getByRole('checkbox', { name: /Start with just this image/ })); expect(screen.queryByRole('note')).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: /Start with just this image/ }))
    await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: /South · toward you/ }))
    await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByRole('note')).toHaveTextContent('Upload artwork for 7 more directions before their animations can generate.')
  })
  it('does not promise the uploaded direction can generate after it was removed', async () => {
    const user = await start(); await user.click(screen.getByRole('button', { name: /Side-scrolling/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(screen.getByRole('button', { name: /^Left/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
    await user.click(screen.getByRole('button', { name: 'Remove direction · Left' })); await user.click(screen.getByRole('button', { name: 'Continue' }))
    expect(screen.getByRole('note')).toHaveTextContent('Upload artwork for 1 more direction before their animations can generate.')
    expect(screen.getByRole('note')).not.toHaveTextContent('Only your uploaded direction')
    expect(screen.getByText(/supplied direction was removed/)).toBeVisible()
  })
  it('counts only directions that request animations as needing artwork', () => {
    const setup = recommendedSetup('side', 'left', 'art')
    expect(artworkNeeded(setup, 'left')).toBe(1)
    expect(artworkNeeded({ ...setup, views: setup.views.map((view) => ({ ...view, motions: [] })) }, 'left')).toBe(0)
    expect(artworkNeeded(recommendedSetup('single', null, 'art'), null)).toBe(0)
  })
  it('stores exactly thirteen eight-direction suggestions', () => { const setup = recommendedSetup('eight', 'S', 'art'); expect(setup.views.reduce((n, view) => n + view.motions.length, 0)).toBe(13); expect(setup.views.filter((view) => view.reference)).toHaveLength(1) })
  describe('generate the other directions', () => {
    const standard = { worker_id: 'desk', available: true, reason: null, profile: { profile: 'h3-24gb-standard' }, features: { last_frame: true } }
    function Location() { const location = useLocation(); return <p>at {location.pathname}{location.search}</p> }
    async function review(facing: RegExp) {
      const user = userEvent.setup()
      render(<MemoryRouter initialEntries={['/projects/new']}><Routes><Route path="/projects/new" element={<ArtFirstOnboarding onMovie={() => {}} />} /><Route path="*" element={<Location />} /></Routes></MemoryRouter>)
      await user.upload(screen.getByLabelText('Choose character image'), new File(['image'], 'Moon-Ranger.png', { type: 'image/png' }))
      await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: /Four-direction/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
      await user.click(screen.getByRole('button', { name: facing })); await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: 'Continue' }))
      return user
    }
    it('creates the workspace and opens the turnaround panel from front-facing art', async () => {
      vi.spyOn(studioApi, 'getGenerationConfig').mockResolvedValue({ workers: [standard] } as never)
      vi.spyOn(studioApi, 'setupCapabilities').mockResolvedValue({ schema_versions: [1] })
      vi.spyOn(studioApi, 'createProject').mockResolvedValue(projectFixture)
      vi.spyOn(studioApi, 'stageSetupReference').mockResolvedValue({ revision_id: 'art-1', key: 'art', sha256: 'hash', width: 512, height: 512 })
      vi.spyOn(studioApi, 'previewSetup').mockImplementation(async (_id, revision, desired) => ({ plan_token: 'p', expected_revision: revision, desired_setup: desired, impacts: [], blocked_operations: [] }))
      const apply = vi.spyOn(studioApi, 'applySetup').mockResolvedValue(projectFixture)
      const user = await review(/South · toward you/)
      const generate = await screen.findByRole('button', { name: 'Generate the other directions' })
      await expect.poll(() => (generate as HTMLButtonElement).disabled).toBe(false)
      await user.click(generate)
      expect(await screen.findByText(`at /projects/${projectFixture.project_id}/motions?directions=generate`)).toBeVisible()
      expect(apply).toHaveBeenCalledOnce()
    })
    it('is not offered once the artwork\'s own direction was removed', async () => {
      vi.spyOn(studioApi, 'getGenerationConfig').mockResolvedValue({ workers: [standard] } as never)
      const user = userEvent.setup()
      render(<MemoryRouter initialEntries={['/projects/new']}><Routes><Route path="/projects/new" element={<ArtFirstOnboarding onMovie={() => {}} />} /><Route path="*" element={<Location />} /></Routes></MemoryRouter>)
      await user.upload(screen.getByLabelText('Choose character image'), new File(['image'], 'Moon-Ranger.png', { type: 'image/png' }))
      await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: /Four-direction/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
      await user.click(screen.getByRole('button', { name: /South · toward you/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
      await user.click(screen.getByRole('button', { name: 'Remove direction · South · toward you' })); await user.click(screen.getByRole('button', { name: 'Continue' }))
      expect(await screen.findByText((text) => text.includes("Your artwork's direction was removed"))).toBeVisible()
      expect(screen.getByRole('button', { name: 'Generate the other directions' })).toBeDisabled()
    })
    it('offers it for three-quarter (SE) artwork on an eight-direction workspace', async () => {
      vi.spyOn(studioApi, 'getGenerationConfig').mockResolvedValue({ workers: [standard] } as never)
      const user = userEvent.setup()
      render(<MemoryRouter initialEntries={['/projects/new']}><Routes><Route path="/projects/new" element={<ArtFirstOnboarding onMovie={() => {}} />} /><Route path="*" element={<Location />} /></Routes></MemoryRouter>)
      await user.upload(screen.getByLabelText('Choose character image'), new File(['image'], 'Moon-Ranger.png', { type: 'image/png' }))
      await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: /Eight-direction/ })); await user.click(screen.getByRole('button', { name: 'Continue' }))
      await user.click(screen.getByRole('button', { name: /Southeast/ })); await user.click(screen.getByRole('button', { name: 'Continue' })); await user.click(screen.getByRole('button', { name: 'Continue' }))
      const generate = await screen.findByRole('button', { name: 'Generate the other directions' })
      await expect.poll(() => (generate as HTMLButtonElement).disabled).toBe(false)
    })
    it.each([
      ['artwork that is not front or three-quarter', /East · right/, [standard], 'Needs front or three-quarter artwork'],
      ['no machine that can turn it', /South · toward you/, [{ ...standard, features: { last_frame: false } }], 'No machine supports turnarounds yet'],
    ])('explains why it cannot start with %s', async (_, facing, workers, reason) => {
      vi.spyOn(studioApi, 'getGenerationConfig').mockResolvedValue({ workers } as never)
      await review(facing)
      expect(await screen.findByText((text) => text.includes(reason))).toBeVisible()
      expect(screen.getByRole('button', { name: 'Generate the other directions' })).toBeDisabled()
    })
  })
})

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { MovieRoute } from './MovieRoute'
import { movieApi, movieActive } from './movieApi'

vi.mock('./movieApi', async (importOriginal) => ({ ...await importOriginal<typeof import('./movieApi')>(), movieApi: {
  get: vi.fn(), capabilities: vi.fn(), story: vi.fn(), generate: vi.fn(), build: vi.fn(),
  reconcile: vi.fn(), select: vi.fn(), cut: vi.fn(), assemble: vi.fn(),
  release: vi.fn(), history: vi.fn(), selectedCut: vi.fn(), downloadReleaseUrl: (id: string, releaseId: string) => `/studio/movies/${id}/releases/${releaseId}/download`, assetUrl: (_id: string, key: string) => `/media/${key}`,
  soundtracks: vi.fn(), upload: vi.fn(), importClip: vi.fn(), acceptContinuity: vi.fn(),
  approve: vi.fn(), compose: vi.fn(), opening: vi.fn(), cancel: vi.fn(), characters: vi.fn(), portrait: vi.fn(), storyboard: vi.fn(), audition: vi.fn(),
} }))

const take = { take_id: 'take-1', request_id: 'r1', status: 'succeeded' as const, job_id: 'j1', asset_key: 'clip.mp4', final_frame_key: 'end.png', predecessor_take_id: null, error: null, stale: false }
const shot = { shot_id: 'arrival', name: 'Arrival', prompt: 'A traveller approaches the city.', opening_prompt: 'Wide shot of the city before the traveller enters.', duration_seconds: 4, continuation_of: null, opening_asset_key: 'opening.png', opening_approved: true, takes: [take], selected_take_id: 'take-1' }
const fixture = { movie_id: 'movie-1', owner_id: 'owner', name: 'Ash and light', revision: 3, brief: 'A traveller discovers a hidden city.', style: 'Warm cinematic light', batch_size: 2, shots: [shot, { ...shot, shot_id: 'reveal', name: 'The reveal', continuation_of: 'arrival', takes: [], selected_take_id: null }], cut: [], assembly: null, releases: [] }

function renderStep(step: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return { ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[`/movies/movie-1/${step}`]}><Routes><Route path="/movies/:movieId/:step" element={<MovieRoute />} /></Routes></MemoryRouter></QueryClientProvider>), client }
}

beforeEach(() => {
  vi.clearAllMocks()
  HTMLElement.prototype.setPointerCapture = vi.fn()
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }
  vi.mocked(movieApi.build).mockResolvedValue(structuredClone(fixture))
  vi.mocked(movieApi.get).mockResolvedValue(structuredClone(fixture))
  vi.mocked(movieApi.capabilities).mockResolvedValue({ generation_available: true, composition_available: true, max_duration_seconds: 6, max_batch_size: 4 })
  vi.mocked(movieApi.story).mockResolvedValue(structuredClone(fixture))
  vi.mocked(movieApi.generate).mockResolvedValue(structuredClone(fixture))
  vi.mocked(movieApi.cut).mockResolvedValue(structuredClone(fixture))
  vi.mocked(movieApi.selectedCut).mockResolvedValue([selectedClip('arrival', 'take-1')])
})

// The server's selected-takes cut (GET /selected-cut), as a motion build saves it.
function selectedClip(shot_id: string, take_id: string) { return { shot_id, take_id, in_seconds: 0, out_seconds: 4, transition_seconds: 0.5, mute: true } }

it('combines legacy shot links into one Movie workspace without approval controls', async () => {
  renderStep('shots')
  await screen.findByRole('heading', { name: 'Movie' })
  expect(screen.getByRole('link', { name: 'Movie' })).toBeVisible()
  expect(screen.queryByRole('link', { name: 'Shots' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Approve opening frame' })).not.toBeInTheDocument()
})

it('saves the draft then builds from attached images regardless of legacy approval', async () => {
  const current = { ...structuredClone(fixture), shots: [{ ...shot, opening_approved: false }] }
  vi.mocked(movieApi.get).mockResolvedValue(current)
  vi.mocked(movieApi.story).mockResolvedValue(current)
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByRole('button', { name: 'Generate movie' }))
  expect(movieApi.story).toHaveBeenCalled()
  expect(movieApi.build).toHaveBeenCalledWith('movie-1', 3, expect.any(String))
  expect(movieApi.approve).not.toHaveBeenCalled()
  expect(screen.getByRole('heading', { name: 'Movie' })).toBeVisible()
})

it('shows an attached still until a generated take replaces it with a video', async () => {
  const current = { ...structuredClone(fixture), shots: [{ ...shot, takes: [], selected_take_id: null, opening_approved: false }] }
  vi.mocked(movieApi.get).mockResolvedValue(current)
  const { client } = renderStep('story')
  expect(await screen.findByAltText('Arrival reference')).toHaveAttribute('src', '/media/opening.png')
  expect(screen.queryByLabelText('Arrival preview')).not.toBeInTheDocument()
  await act(async () => { client.setQueryData(['movie', 'movie-1'], { ...current, shots: [{ ...shot, opening_approved: false }] }) })
  expect(await screen.findByLabelText('Arrival preview')).toHaveAttribute('src', '/media/clip.mp4')
  expect(screen.queryByAltText('Arrival reference')).not.toBeInTheDocument()
})

it('saves revised motion before requesting another take of just this shot', async () => {
  const user = userEvent.setup(); renderStep('story')
  await user.clear(await screen.findByLabelText('Action & camera movement'))
  await user.type(screen.getByLabelText('Action & camera movement'), 'Camera moves closer.')
  await user.click(screen.getByRole('button', { name: 'Generate another take' }))
  expect(movieApi.story).toHaveBeenCalledWith('movie-1', expect.objectContaining({ shots: expect.arrayContaining([expect.objectContaining({ shot_id: 'arrival', prompt: 'Camera moves closer.' })]) }))
  expect(movieApi.generate).toHaveBeenCalledWith('movie-1', expect.objectContaining({ shot_ids: ['arrival'] }))
  expect(movieApi.build).not.toHaveBeenCalled()
})

it('reuses an uncertain shot-generation request without spending twice', async () => {
  vi.mocked(movieApi.generate).mockRejectedValueOnce(new Error('Connection interrupted')).mockResolvedValue(structuredClone(fixture))
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByRole('button', { name: 'Generate another take' }))
  await screen.findByRole('alert')
  await user.click(screen.getByRole('button', { name: 'Generate another take' }))
  expect(vi.mocked(movieApi.generate).mock.calls[1][1].request_id).toBe(vi.mocked(movieApi.generate).mock.calls[0][1].request_id)
})

it('asks for a missing image in the same workspace without generating', async () => {
  const current = { ...structuredClone(fixture), shots: [{ ...shot, opening_asset_key: null, takes: [], selected_take_id: null }] }
  vi.mocked(movieApi.get).mockResolvedValue(current); vi.mocked(movieApi.story).mockResolvedValue(current)
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByRole('button', { name: 'Generate movie' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Add a starting image for Arrival')
  expect(movieApi.build).not.toHaveBeenCalled()
})

it('opens and reopens the selected video in a player', async () => {
  const user = userEvent.setup(); renderStep('story')
  const preview = await screen.findByRole('button', { name: 'Preview take 1' })
  await user.click(preview)
  expect(screen.getByRole('dialog', { name: 'Arrival take preview' })).toBeVisible()
  expect(screen.getByLabelText('Arrival take playback')).toHaveAttribute('src', '/media/clip.mp4')
  await user.click(screen.getByRole('button', { name: 'Close preview' }))
  await user.click(preview)
  expect(screen.getByRole('dialog')).toBeVisible()
})

it('preserves prompt edits while switching the sequence selection', async () => {
  const user = userEvent.setup(); renderStep('story')
  await user.type(await screen.findByLabelText('Action & camera movement'), ' Close up.')
  await user.click(screen.getByRole('button', { name: 'Arrange The reveal, shot 2' }))
  expect(screen.getAllByLabelText('Shot name')).toHaveLength(1)
  await user.click(screen.getByRole('button', { name: 'Arrange Arrival, shot 1' }))
  expect(screen.getByLabelText('Action & camera movement')).toHaveValue(shot.prompt + ' Close up.')
})

it('preserves a draft revision when another tab changes the story', async () => {
  const user = userEvent.setup(); const { client } = renderStep('story')
  await user.type(await screen.findByLabelText('Movie brief'), ' My edit.')
  await act(async () => { client.setQueryData(['movie', 'movie-1'], { ...structuredClone(fixture), revision: 4, brief: 'Other tab story' }) })
  await user.click(screen.getByRole('button', { name: 'Save draft' }))
  expect(movieApi.story).toHaveBeenCalledWith('movie-1', expect.objectContaining({ expected_revision: 3 }))
})

it('saves explicit GPU routing with the story', async () => {
  const user = userEvent.setup(); renderStep('story')
  await user.selectOptions(await screen.findByLabelText('GPU network'), 'my_machines')
  await user.click(screen.getByRole('button', { name: 'Save draft' }))
  expect(movieApi.story).toHaveBeenCalledWith('movie-1', expect.objectContaining({ compute_target: 'my_machines' }))
})

it('keeps continuation shots after their source when reordering', async () => {
  const user = userEvent.setup(); renderStep('story')
  const card = await screen.findByRole('button', { name: 'Arrange The reveal, shot 2' })
  card.focus(); await user.keyboard('{ArrowUp}')
  expect(card).toHaveAccessibleName('Arrange The reveal, shot 2')
  expect(screen.getByRole('alert')).toHaveTextContent('Keep continuation shots after the shots they continue.')
})

it('retains old takes and supports explicit continuity review', async () => {
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [{ ...shot, takes: [{ ...take, stale: true }] }] })
  vi.mocked(movieApi.acceptContinuity).mockResolvedValue(structuredClone(fixture))
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByText('Takes & generation details (1)'))
  await user.click(screen.getByRole('button', { name: 'Accept reviewed continuity' }))
  expect(movieApi.acceptContinuity).toHaveBeenCalledWith('movie-1', 'arrival', 'take-1', 3)
})

it('keeps rendering worker details in the take history', async () => {
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [{ ...shot, takes: [{ ...take, completed_by_worker_id: 'gpu-house-2' }] }] })
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByText('Takes & generation details (1)'))
  await user.click(screen.getByText('Generation details'))
  expect(screen.getByText('Rendered by gpu-house-2')).toBeVisible()
})

it('starts Review from the selected takes, then saves the trimmed cut and assembles it', async () => {
  vi.mocked(movieApi.assemble).mockResolvedValue(structuredClone(fixture))
  vi.mocked(movieApi.cut).mockResolvedValue({ ...structuredClone(fixture), revision: 4 })
  const user = userEvent.setup(); renderStep('review')
  expect(await screen.findByRole('button', { name: 'Arrange Arrival, clip 1' })).toBeVisible()
  expect(screen.getByText('These are your selected takes in story order. Save and assemble to keep this cut.')).toBeVisible()
  const clip = screen.getByRole('group', { name: 'Cut clip 1' })
  await user.clear(within(clip).getByLabelText('Trim start (seconds)'))
  await user.type(within(clip).getByLabelText('Trim start (seconds)'), '0.5')
  await user.click(screen.getByRole('button', { name: 'Save and assemble' }))
  expect(movieApi.cut).toHaveBeenCalledWith('movie-1', expect.objectContaining({ expected_revision: 3, clips: [{ shot_id: 'arrival', take_id: 'take-1', in_seconds: 0.5, out_seconds: 4, transition_seconds: 0.5, mute: true }] }))
  expect(movieApi.assemble).toHaveBeenCalledWith('movie-1', 4, expect.any(String))
  expect(movieApi.selectedCut).toHaveBeenCalledWith('movie-1')
  expect(movieApi.generate).not.toHaveBeenCalled()
})

it('says so when the selected takes cannot be read', async () => {
  vi.mocked(movieApi.selectedCut).mockRejectedValue(new Error('offline'))
  renderStep('review')
  expect(await screen.findByRole('alert')).toHaveTextContent('Your clips could not be loaded. Reload the page to try again.')
})

const coda = { ...shot, shot_id: 'coda', name: 'Coda', takes: [{ ...take, take_id: 'take-3', asset_key: 'coda.mp4' }], selected_take_id: 'take-3' }
const threeShots = { ...structuredClone(fixture), shots: [shot, { ...fixture.shots[1], takes: [{ ...take, take_id: 'take-2', asset_key: 'reveal.mp4' }], selected_take_id: 'take-2' }, coda] }

it('reorders review clips and keeps a continuation after the clip it continues', async () => {
  vi.mocked(movieApi.get).mockResolvedValue(structuredClone(threeShots))
  vi.mocked(movieApi.selectedCut).mockResolvedValue([selectedClip('arrival', 'take-1'), selectedClip('reveal', 'take-2'), selectedClip('coda', 'take-3')])
  vi.mocked(movieApi.assemble).mockResolvedValue(structuredClone(threeShots))
  const user = userEvent.setup(); renderStep('review')
  const reveal = await screen.findByRole('button', { name: 'Arrange The reveal, clip 2' })
  reveal.focus(); await user.keyboard('{ArrowUp}')
  expect(screen.getByRole('alert')).toHaveTextContent('Keep continuation clips after the clips they continue.')
  expect(screen.getByRole('button', { name: 'Arrange The reveal, clip 2' })).toBeVisible()
  screen.getByRole('button', { name: 'Arrange Coda, clip 3' }).focus(); await user.keyboard('{ArrowUp}{ArrowUp}')
  expect(screen.getByRole('button', { name: 'Arrange Coda, clip 1' })).toHaveFocus()
  await user.click(screen.getByRole('button', { name: 'Save and assemble' }))
  expect(vi.mocked(movieApi.cut).mock.calls[0][1].clips.map((clip) => clip.shot_id)).toEqual(['coda', 'arrival', 'reveal'])
})

it('plays the clips back to back in sequence order before assembly', async () => {
  vi.mocked(movieApi.get).mockResolvedValue(structuredClone(threeShots))
  // A spoken shot keeps its sound in the assembly; the others are silenced.
  vi.mocked(movieApi.selectedCut).mockResolvedValue([selectedClip('arrival', 'take-1'), { ...selectedClip('reveal', 'take-2'), mute: false }, selectedClip('coda', 'take-3')])
  const user = userEvent.setup(); renderStep('review')
  const player = await screen.findByLabelText<HTMLVideoElement>('Movie preview, clips in order')
  expect(player).toHaveAttribute('src', '/media/clip.mp4')
  expect(screen.getByText('Clip 1 of 3 · Arrival · silent')).toBeVisible()
  expect(player.muted).toBe(true)
  // Unmuting a silenced clip does not let its source audio through.
  player.muted = false; fireEvent.volumeChange(player)
  expect(player.muted).toBe(true)
  Object.defineProperty(player, 'currentTime', { configurable: true, writable: true, value: 4 })
  fireEvent.timeUpdate(player)
  expect(screen.getByText('Clip 2 of 3 · The reveal')).toBeVisible()
  expect(player).toHaveAttribute('src', '/media/reveal.mp4')
  expect(player.muted).toBe(false)
  expect(screen.getByRole('button', { name: 'Arrange The reveal, clip 2' })).toHaveAttribute('aria-current', 'true')
  await user.click(screen.getByRole('button', { name: 'Arrange Coda, clip 3' }))
  expect(screen.getByText('Clip 3 of 3 · Coda · silent')).toBeVisible()
  expect(player).toHaveAttribute('src', '/media/coda.mp4')
  expect(player.muted).toBe(true)
})

it('keeps scrubbing in the preview inside the clip trim', async () => {
  vi.mocked(movieApi.selectedCut).mockResolvedValue([{ ...selectedClip('arrival', 'take-1'), in_seconds: 1, out_seconds: 3 }])
  renderStep('review')
  const player = await screen.findByLabelText<HTMLVideoElement>('Movie preview, clips in order')
  Object.defineProperty(player, 'currentTime', { configurable: true, writable: true, value: 0.2 })
  fireEvent.seeking(player)
  expect(player.currentTime).toBe(1)
  player.currentTime = 3.8; fireEvent.seeking(player)
  expect(player.currentTime).toBeCloseTo(2.9)
  player.currentTime = 0.4; fireEvent.timeUpdate(player)
  expect(player.currentTime).toBe(1)
  expect(screen.getByText('Clip 1 of 1 · Arrival · silent')).toBeVisible()
})

it('shows the assembled movie when the saved cut is assembled', async () => {
  const cut = [{ shot_id: 'arrival', take_id: 'take-1', in_seconds: 0, out_seconds: 4, transition_seconds: 0.5, mute: true }]
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), cut, assembly: { request_id: 'a', cut_digest: 'd', status: 'succeeded', asset_key: 'movie.mp4', error: null } })
  renderStep('review')
  expect(await screen.findByLabelText('Assembled movie preview')).toHaveAttribute('src', '/media/movie.mp4')
  expect(screen.getByText(/Saved and assembled/)).toBeVisible()
  expect(screen.queryByRole('button', { name: /assemble/i })).not.toBeInTheDocument()
})

it('sends a movie without finished takes back to the Movie page', async () => {
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [{ ...shot, takes: [], selected_take_id: null }] })
  vi.mocked(movieApi.selectedCut).mockResolvedValue([])
  renderStep('review')
  expect(await screen.findByRole('heading', { name: 'No clips yet' })).toBeVisible()
  expect(screen.getByRole('link', { name: 'Go to Movie' })).toHaveAttribute('href', '/movies/movie-1/story')
})

it('keeps reconciling builds and superseded pooled openings', () => {
  const current = { ...structuredClone(fixture), build: { request_id: 'movie', status: 'generating' as const, error: null } }
  expect(movieActive(current)).toBe(true)
  const movie = { ...structuredClone(fixture), shots: [], compositions: { old: { shot_id: 'arrival', status: 'superseded', provider: 'pool', error: null, settled: false } } }
  expect(movieActive(movie)).toBe(true)
  movie.compositions.old.settled = true
  expect(movieActive(movie)).toBe(false)
})


it('plays the explicitly selected take when earlier takes are chosen', async () => {
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [{ ...shot, takes: [take, { ...take, take_id: 'take-2', asset_key: 'second.mp4' }], selected_take_id: 'take-1' }] })
  renderStep('story')
  expect(await screen.findByLabelText('Arrival preview')).toHaveAttribute('src', '/media/clip.mp4')
})

it('shows the replacement source instead of video made from a different image', async () => {
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [{ ...shot, opening_asset_key: 'replacement.png', takes: [{ ...take, stale: true, opening_asset_key: 'opening.png' }] }] })
  renderStep('story')
  expect(await screen.findByAltText('Arrival reference')).toHaveAttribute('src', '/media/replacement.png')
  expect(screen.queryByLabelText('Arrival preview')).not.toBeInTheDocument()
})


it('shows a failed generation without requiring the take history to be opened', async () => {
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [{ ...shot, takes: [{ ...take, status: 'failed', error: 'Worker ran out of memory' }], selected_take_id: null }] })
  renderStep('story')
  const alerts = await screen.findAllByRole('alert')
  expect(alerts[0]).toHaveTextContent('Worker ran out of memory')
  expect(alerts[0]).toBeVisible()
})


it('changes a historical hard cut into a dissolve without regenerating takes', async () => {
  const clips = [1, 2].map(() => ({ shot_id: 'arrival', take_id: 'take-1', in_seconds: 0, out_seconds: 4 }))
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), cut: clips })
  vi.mocked(movieApi.assemble).mockResolvedValue(structuredClone(fixture))
  const user = userEvent.setup(); renderStep('review')
  expect(await screen.findByRole('button', { name: 'Assemble movie' })).toBeEnabled()
  const transition = screen.getByRole('combobox', { name: /Transition to next clip/ })
  expect(transition).toHaveValue('0')
  await user.selectOptions(transition, '0.5')
  expect(screen.getByText('2 clips · 7.5 seconds')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Save and assemble' }))
  expect(movieApi.cut).toHaveBeenCalledWith('movie-1', expect.objectContaining({ clips: [{ ...clips[0], transition_seconds: 0.5 }, clips[1]] }))
  expect(movieApi.generate).not.toHaveBeenCalled()
})


it('puts generation before review and reserves Save movie for Review', async () => {
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), assembly: { request_id: 'assembly-1', cut_digest: 'digest-1', status: 'succeeded', asset_key: 'movie.mp4', error: null } })
  renderStep('story')
  const generate = await screen.findByRole('button', { name: 'Generate movie' })
  const review = screen.getByRole('link', { name: 'Review movie →' })
  expect(generate.compareDocumentPosition(review) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(review).toHaveAttribute('href', '/movies/movie-1/review')
  expect(screen.queryByRole('button', { name: 'Save movie' })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Cut' })).not.toBeInTheDocument()
})

it('writes a spoken line for a voiced character and saves it with the shot', async () => {
  const mara = { character_id: 'mara', name: 'Mara', appearance: 'weathered pilot', voice: 'low alto', possessive: 'her' as const, voice_asset_key: 'voices/mara.wav', voice_seconds: 4.2 }
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), characters: [mara] })
  const user = userEvent.setup(); renderStep('story')
  expect(await screen.findByRole('heading', { name: 'Cast & voices' })).toBeVisible()
  expect(screen.getByText('Voice reference · 4.2 s')).toBeVisible()
  await user.click(screen.getByRole('button', { name: '+ Add line' }))
  await user.type(screen.getByLabelText('Line 1 words'), 'Then we go down.')
  expect(screen.getByLabelText('Line 1 character')).toHaveValue('mara')
  expect(screen.getByLabelText('Duration (seconds)')).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Save draft' }))
  expect(movieApi.story).toHaveBeenCalledWith('movie-1', expect.objectContaining({ shots: expect.arrayContaining([expect.objectContaining({ shot_id: 'arrival', dialogue: [{ character_id: 'mara', text: 'Then we go down.', delivery: '' }] })]) }))
})

it('directs a motion shot and keeps its timed beats when saving', async () => {
  const directed = { ...shot, still: 'the city walls', beats: [{ at_seconds: 1.5, action: 'the gate swings open' }] }
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [directed, fixture.shots[1]] })
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByText('Motion direction'))
  await user.type(screen.getByLabelText('Camera'), 'The camera pushes in with small amplitude at slow speed.')
  expect(screen.getByLabelText('Stays still')).toHaveValue('the city walls')
  expect(screen.getByText('1.5s · the gate swings open')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Save draft' }))
  expect(movieApi.story).toHaveBeenCalledWith('movie-1', expect.objectContaining({ shots: expect.arrayContaining([expect.objectContaining({
    shot_id: 'arrival', camera: 'The camera pushes in with small amplitude at slow speed.', still: 'the city walls',
    beats: [{ at_seconds: 1.5, action: 'the gate swings open' }],
  })]) }))
})

it('flags and drops a timed beat that a shorter shot no longer reaches', async () => {
  const directed = { ...shot, duration_seconds: 6, beats: [{ at_seconds: 1, action: 'dust falls' }, { at_seconds: 5, action: 'the gate opens' }] }
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), shots: [directed, fixture.shots[1]] })
  const user = userEvent.setup(); renderStep('story')
  const duration = await screen.findByLabelText('Duration (seconds)')
  await user.clear(duration); await user.type(duration, '4')
  await user.click(screen.getByText('Motion direction'))
  expect(screen.getByText(/past the shot's end/)).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Save draft' }))
  expect(movieApi.story).toHaveBeenCalledWith('movie-1', expect.objectContaining({ shots: expect.arrayContaining([expect.objectContaining({
    shot_id: 'arrival', duration_seconds: 4, beats: [{ at_seconds: 1, action: 'dust falls' }],
  })]) }))
})

it('storyboards every shot from the saved story in one request', async () => {
  vi.mocked(movieApi.storyboard).mockResolvedValue(structuredClone(fixture))
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByRole('button', { name: 'Storyboard' }))
  expect(movieApi.story).toHaveBeenCalled()
  expect(movieApi.storyboard).toHaveBeenCalledWith('movie-1', 3, expect.any(String))
  expect(movieApi.build).not.toHaveBeenCalled()
})

it('composes a portrait for a saved character with an appearance', async () => {
  const mara = { character_id: 'mara', name: 'Mara', appearance: 'weathered pilot', voice: '', possessive: 'her' as const, voice_asset_key: null }
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), characters: [mara] })
  vi.mocked(movieApi.portrait).mockResolvedValue(structuredClone(fixture))
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByRole('button', { name: 'Compose portrait' }))
  expect(movieApi.portrait).toHaveBeenCalledWith('movie-1', 'mara', 3, expect.any(String))
})

it('auditions a saved character from the voice description', async () => {
  const cap = { character_id: 'cap', name: 'Captain', appearance: 'grizzled', voice: 'gravelly baritone', possessive: 'his' as const, voice_asset_key: null }
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), characters: [cap] })
  vi.mocked(movieApi.audition).mockResolvedValue(structuredClone(fixture))
  const user = userEvent.setup(); renderStep('story')
  await user.click(await screen.findByRole('button', { name: 'Audition voice' }))
  expect(movieApi.audition).toHaveBeenCalledWith('movie-1', 'cap', 3, expect.any(String))
})

it('will not save a line for a character without a voice', async () => {
  const silent = { character_id: 'ivo', name: 'Ivo', appearance: '', voice: '', possessive: 'his' as const, voice_asset_key: null }
  vi.mocked(movieApi.get).mockResolvedValue({ ...structuredClone(fixture), characters: [silent] })
  renderStep('story')
  await screen.findByRole('heading', { name: 'Cast & voices' })
  expect(screen.getByText('No voice yet — this character cannot speak.')).toBeVisible()
  expect(screen.getByRole('button', { name: '+ Add line' })).toBeDisabled()
})

it('redirects historical Cut links to Review with the save action', async () => {
  renderStep('cut')
  expect(await screen.findByRole('heading', { name: 'Review your movie' })).toBeVisible()
  expect(screen.getByRole('link', { name: 'Review' })).toHaveAttribute('aria-current', 'page')
  expect(await screen.findByRole('button', { name: 'Save and assemble' })).toBeVisible()
})

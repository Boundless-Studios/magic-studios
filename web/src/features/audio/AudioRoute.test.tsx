import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { AudioRoute } from './AudioRoute'
import { audioApi, audioContentType, draftFrom, type AudioCapabilities, type AudioSet, type Candidate } from './audioApi'

vi.mock('./audioApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./audioApi')>()
  return { ...actual, audioApi: {
    ...actual.audioApi,
    get: vi.fn(), capabilities: vi.fn(), update: vi.fn(), search: vi.fn(), importSound: vi.fn(), addUpload: vi.fn(),
    review: vi.fn(), select: vi.fn(), readiness: vi.fn(), release: vi.fn(), replaceOriginal: vi.fn(),
  } }
})

const caps: AudioCapabilities = {
  account_tier: 'invited', free_sounds_available: true, upload_available: true, processing_available: true,
  replace_original_available: true, send_to_movie_available: true, generation_available: false,
  max_cues: 64, max_candidates_per_cue: 16, max_file_bytes: 52428800, upload_types: ['audio/mpeg', 'audio/wav'],
}
const cue = { cue_id: 'crack', name: 'Glass crack', kind: 'sfx' as const, prompt: 'glass crack', text: '', target_seconds: null, loop: false, keep: 1, selected: [] as string[] }
const base: AudioSet = { set_id: 's1', owner_id: 'o', name: 'Glass', brief: '', revision: 3, licence_policy: 'cc0', cues: [cue], candidates: [], releases: [], upload_keys: [] }
const candidate = (overrides: Partial<Candidate> = {}): Candidate => ({
  candidate_id: 'c1', cue_id: 'crack', origin: 'freesound', asset_key: 'outputs/audio/s1/candidates/abc.mp3', sha256: 'abc',
  byte_length: 1000, media_type: 'audio/mpeg', duration_seconds: 1.5,
  measured: { integrated_lufs: -21.3, true_peak_dbtp: -18.1, centroid_hz: 906 },
  rendition_key: 'outputs/audio/s1/renditions/abc.wav',
  licence: { spdx: 'CC0-1.0', licence_url: 'http://creativecommons.org/publicdomain/zero/1.0/', author: 'qubodup', source: 'freesound', source_id: '101', source_url: 'https://freesound.org/s/101/', retrieved_at: '2026-11-01T00:00:00Z', variant: 'preview', attribution_required: false },
  provenance: { name: 'glass 101' }, review: null, created_at: '2026-11-01T00:00:00Z', ...overrides,
})
const ready = { ready: false, problems: [{ cue_id: 'crack', message: 'Select 1 variant(s); 0 selected.' }], files: 0, attribution_required: false, previews: 0 }

function renderSet(audioSet: AudioSet, capabilities = caps) {
  vi.mocked(audioApi.get).mockResolvedValue(structuredClone(audioSet))
  vi.mocked(audioApi.capabilities).mockResolvedValue(capabilities)
  vi.mocked(audioApi.readiness).mockResolvedValue(ready)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/audio/s1']}><Routes><Route path="/audio/:setId" element={<AudioRoute />} /></Routes></MemoryRouter></QueryClientProvider>)
}

beforeEach(() => { vi.clearAllMocks() })

it('adds a cue and saves it with the licence policy', async () => {
  vi.mocked(audioApi.update).mockResolvedValue({ ...base, revision: 4 })
  const user = userEvent.setup(); renderSet({ ...base, cues: [] })
  await user.click(await screen.findByRole('button', { name: 'Add a cue' }))
  await user.type(screen.getByLabelText('Cue 1 name'), 'Door slam')
  await user.selectOptions(screen.getByLabelText('Cue 1 kind'), 'ambience')
  await user.click(screen.getByLabelText(/CC0 and CC-BY/))
  await user.click(screen.getByRole('button', { name: 'Save cues' }))
  expect(audioApi.update).toHaveBeenCalledWith('s1', 3, expect.objectContaining({
    licence_policy: 'cc0_cc_by', cues: [expect.objectContaining({ name: 'Door slam', kind: 'ambience', keep: 1 })],
  }))
})

it('searches free sounds for a cue and adds one as a candidate', async () => {
  vi.mocked(audioApi.search).mockResolvedValue([{ source_id: '101', name: 'glass 101', author: 'qubodup', spdx: 'CC0-1.0', licence_url: '', attribution_required: false, duration_seconds: 1.5, preview_url: 'https://cdn.freesound.org/previews/0/101-hq.mp3', source_url: 'https://freesound.org/s/101/', tags: [] }])
  vi.mocked(audioApi.importSound).mockResolvedValue({ ...base, revision: 4, candidates: [candidate()] })
  const user = userEvent.setup(); renderSet(base)
  const board = await screen.findByRole('region', { name: 'Glass crack candidates' })
  expect(within(board).getByLabelText('Search free sounds for Glass crack')).toHaveValue('glass crack')
  await user.click(within(board).getByRole('button', { name: 'Search' }))
  expect(audioApi.search).toHaveBeenCalledWith('s1', 'glass crack', 30)
  await user.click(await within(board).findByRole('button', { name: 'Add as candidate' }))
  expect(audioApi.importSound).toHaveBeenCalledWith('s1', 'crack', 3, '101')
  expect(await within(board).findByRole('listitem', { name: 'Glass crack candidate 1' })).toHaveTextContent('-21.3 LUFS')
})

it('keeps a candidate and ships it; a rejected one cannot be shipped', async () => {
  vi.mocked(audioApi.review).mockResolvedValue({ ...base, revision: 4, candidates: [candidate({ review: { outcome: 'keep', note: 'clean', reason: '', sha256: 'abc', actor_id: 'o', actor_kind: 'session', at: '' } })] })
  vi.mocked(audioApi.select).mockResolvedValue({ ...base, revision: 5 })
  const user = userEvent.setup(); renderSet({ ...base, candidates: [candidate()] })
  const tile = await screen.findByRole('listitem', { name: 'Glass crack candidate 1' })
  expect(within(tile).getByRole('checkbox', { name: /Ship/ })).toBeDisabled()
  expect(within(tile).getByText(/Freesound preview/)).toBeInTheDocument()
  await user.type(within(tile).getByLabelText('Note for Glass crack candidate 1'), 'clean')
  await user.click(within(tile).getByRole('button', { name: 'Keep' }))
  expect(audioApi.review).toHaveBeenCalledWith('s1', 'c1', 3, 'keep', 'clean', 'abc')
  const shipped = await screen.findByRole('checkbox', { name: /Ship/ })
  expect(shipped).toBeEnabled()
  await user.click(shipped)
  expect(audioApi.select).toHaveBeenCalledWith('s1', 'crack', 4, ['c1'])
})

it('marks an agent review and a CC-BY sound the policy no longer allows', async () => {
  const agent = candidate({ licence: { ...candidate().licence, spdx: 'CC-BY-4.0', attribution_required: true }, review: { outcome: 'keep', note: '', reason: '', sha256: 'abc', actor_id: 'o', actor_kind: 'api_key', at: '' } })
  renderSet({ ...base, candidates: [agent] })
  const tile = await screen.findByRole('listitem', { name: 'Glass crack candidate 1' })
  expect(within(tile).getByText('Kept by an API key')).toBeInTheDocument()
  expect(within(tile).getByRole('note')).toHaveTextContent('no longer allows')
  expect(within(tile).getByRole('checkbox', { name: /Ship/ })).toBeDisabled()
})

it('releases once ready and lists the download', async () => {
  vi.mocked(audioApi.release).mockResolvedValue({ ...base, revision: 6, releases: [{ release_id: 'r1', name: 'v1', asset_key: 'k', manifest_sha256: 'm', source_revision: 5, created_at: '2026-11-01T00:00:00Z', file_count: 1, attribution_required: false }] })
  const user = userEvent.setup()
  renderSet({ ...base, revision: 5, cues: [{ ...cue, selected: ['c1'] }], candidates: [candidate({ review: { outcome: 'keep', note: '', reason: '', sha256: 'abc', actor_id: 'o', actor_kind: 'session', at: '' } })] })
  vi.mocked(audioApi.readiness).mockResolvedValue({ ready: true, problems: [], files: 1, attribution_required: false, previews: 1 })
  await user.type(await screen.findByLabelText('Release name'), 'v1')
  await user.click(await screen.findByRole('button', { name: 'Release audio' }))
  expect(audioApi.release).toHaveBeenCalledWith('s1', 5, 'v1')
  expect(await screen.findByRole('link', { name: 'Download v1' })).toHaveAttribute('href', '/studio/audio/s1/releases/r1/download')
})

it('tells a public account without search or processing what it can still do', async () => {
  renderSet(base, { ...caps, account_tier: 'public', free_sounds_available: false, processing_available: false, replace_original_available: false })
  expect(await screen.findByText(/Free-sound search is not set up/)).toBeInTheDocument()
  expect(screen.getByText(/kept exactly as uploaded/)).toBeInTheDocument()
  expect(screen.queryByLabelText('Search free sounds for Glass crack')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Upload a file for Glass crack')).toBeEnabled()
})

it('draftFrom sends only the fields the API accepts', () => {
  expect(draftFrom({ ...base, cues: [{ ...cue, selected: ['c1'] }] }).cues[0]).not.toHaveProperty('selected')
})

it('normalizes the names browsers give WAV and MP3 files', () => {
  expect(audioContentType(new File(['x'], 'a.wav', { type: 'audio/x-wav' }))).toBe('audio/wav')
  expect(audioContentType(new File(['x'], 'a.wav', { type: 'audio/wave' }))).toBe('audio/wav')
  expect(audioContentType(new File(['x'], 'a.mp3', { type: 'audio/mp3' }))).toBe('audio/mpeg')
  expect(audioContentType(new File(['x'], 'a.WAV', { type: '' }))).toBe('audio/wav')
})

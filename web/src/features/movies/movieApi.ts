import { redirectToLogin } from '../../api/auth'
import { apiQueue } from '../../api/requestQueue'
import { ApiError } from '../../api/client'

export interface MovieTake {
  take_id: string; request_id: string; status: 'waiting' | 'submitting' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  opening_asset_key?: string | null
  job_id: string | null; asset_key: string | null; final_frame_key: string | null
  predecessor_take_id: string | null; error: string | null; stale: boolean
  completed_by_worker_id?: string | null
  duration_seconds?: number
}
export interface DialogueLine { character_id: string; text: string; delivery?: string; pause_before?: number; reaction?: string }
export interface CharacterDefinition { character_id: string; name: string; appearance: string; voice: string; possessive: 'their' | 'her' | 'his'; voice_asset_key: string | null; audition_line?: string }
export interface MovieCharacter extends CharacterDefinition { voice_seconds?: number | null; portrait_asset_key?: string | null }
export interface MotionBeat { at_seconds: number; action: string }
export interface ShotDefinition {
  shot_id: string; name: string; prompt: string; opening_prompt: string
  duration_seconds: number; continuation_of: string | null
  dialogue?: DialogueLine[]; soundscape?: string
  camera?: string; still?: string; beats?: MotionBeat[]
}
export interface MovieShot extends ShotDefinition {
  opening_asset_key: string | null; opening_approved: boolean; takes: MovieTake[]; selected_take_id: string | null
}
export interface CutClip { shot_id: string; take_id: string; in_seconds: number; out_seconds: number; transition_seconds?: number; mute?: boolean }
export interface AudioTrack { asset_key: string; offset_seconds: number; trim_start_seconds: number; gain_db: number; end_seconds?: number | null; fade_in_seconds?: number; fade_out_seconds?: number; swell_db?: number; swell_seconds?: number }
export interface MovieRelease { release_id: string; name: string; asset_key: string; cut_digest: string; source_revision: number }
export interface Movie {
  movie_id: string; owner_id: string; name: string; revision: number; brief: string; style: string; batch_size: number
  compute_target?: 'shared' | 'my_machines'
  /** Set in the list during the warning window: deleted for inactivity unless opened. */
  expires_at?: string | null
  characters?: MovieCharacter[]
  shots: MovieShot[]; cut: CutClip[]
  build?: { request_id: string; status: 'generating' | 'assembling' | 'succeeded' | 'failed'; error: string | null } | null
  assembly: { request_id: string; status: string; asset_key: string | null; error: string | null; cut_digest: string; clips?: CutClip[]; joins?: { index: number; at_seconds: number; diff: number; limit: number }[] } | null
  releases: MovieRelease[]
  soundtracks?: AudioTrack[]
  compositions?: Record<string, { shot_id: string | null; status: string; error: string | null; provider?: string; settled?: boolean; created_revision?: number; output_key?: string; kind?: 'opening' | 'portrait' | 'storyboard' | 'audition'; character_id?: string }>
}
/** H3's native ceiling (362 frames at 24 fps) in half-second steps; the server's capabilities win. */
export const MAX_SHOT_SECONDS = 15

export interface MovieCapabilities { generation_available: boolean; composition_available: boolean; planner_available?: boolean; min_duration_seconds?: number; duration_step_seconds?: number; max_duration_seconds: number; max_batch_size: number; dialogue_available?: boolean; director_available?: boolean; max_dialogue_seconds?: number; voice_min_seconds?: number; voice_max_seconds?: number; max_speakers_per_shot?: number }
export interface StoryUpdate { compute_target: 'shared' | 'my_machines'; expected_revision: number; brief: string; style: string; batch_size: number; shots: ShotDefinition[] }

const root = '/studio/movies'
async function request<T>(path: string, method = 'GET', body?: object): Promise<T> {
  return apiQueue.run(() => fetch(`${root}${path}`, { method, headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) }), async (response) => {
  if (!response.ok) {
    if (response.status === 401) redirectToLogin()
    const data = await response.json().catch(() => null) as { detail?: unknown } | null
    // Coded refusals (a screened prompt, an account limit) carry {code, message}.
    const coded = data?.detail && typeof data.detail === 'object' ? data.detail as { code?: unknown; message?: unknown } : null
    const message = response.status === 409 ? 'This movie changed elsewhere. Your draft is preserved; reload the latest version before saving again.'
      : typeof data?.detail === 'string' ? data.detail
        : typeof coded?.message === 'string' ? coded.message
          : response.status === 422 ? 'Check the shot settings and try again.' : 'The movie request could not be completed. Try again.'
    throw new ApiError(response.status, message, typeof coded?.code === 'string' ? coded.code : undefined)
  }
  return response.json() as Promise<T>
  })
}
const path = (id: string) => `/${encodeURIComponent(id)}`
export const movieApi = {
  create: (name: string) => request<Movie>('', 'POST', { name }),
  list: () => request<Movie[]>(''),
  get: (id: string) => request<Movie>(path(id)),
  capabilities: () => request<MovieCapabilities>('/capabilities'),
  story: (id: string, body: StoryUpdate) => request<Movie>(`${path(id)}/story`, 'PUT', body),
  plan: (id: string, body: { expected_revision: number; brief: string; style: string; shot_count: number; target_seconds?: number | null }) => request<{ shots: ShotDefinition[]; characters?: CharacterDefinition[]; style?: string }>(`${path(id)}/plan`, 'POST', body),
  build: (id: string, expected_revision: number, request_id: string) => request<Movie>(`${path(id)}/build`, 'POST', { expected_revision, request_id }),
  generate: (id: string, body: { expected_revision: number; shot_ids: string[]; request_id: string; select_when_ready?: boolean }) => request<Movie>(`${path(id)}/generate`, 'POST', body),
  reconcile: (id: string) => request<Movie>(`${path(id)}/reconcile`, 'POST', {}),
  select: (id: string, shot: string, expected_revision: number, take_id: string) => request<Movie>(`${path(id)}/shots/${encodeURIComponent(shot)}/select`, 'POST', { expected_revision, take_id }),
  compose: (id: string, shot: string, expected_revision: number, request_id: string) => request<Movie>(`${path(id)}/shots/${encodeURIComponent(shot)}/compose`, 'POST', { expected_revision, request_id }),
  approve: (id: string, shot: string, expected_revision: number) => request<Movie>(`${path(id)}/shots/${encodeURIComponent(shot)}/approve`, 'POST', { expected_revision }),
  opening: (id: string, shot: string, expected_revision: number, asset_key: string) => request<Movie>(`${path(id)}/shots/${encodeURIComponent(shot)}/opening`, 'POST', { expected_revision, asset_key }),
  cancel: (id: string, shot: string, take: string, expected_revision: number) => request<Movie>(`${path(id)}/shots/${encodeURIComponent(shot)}/takes/${encodeURIComponent(take)}/cancel`, 'POST', { expected_revision }),
  acceptContinuity: (id: string, shot: string, take: string, expected_revision: number) => request<Movie>(`${path(id)}/shots/${encodeURIComponent(shot)}/takes/${encodeURIComponent(take)}/accept-continuity`, 'POST', { expected_revision }),
  importClip: (id: string, shot: string, expected_revision: number, asset_key: string) => request<Movie>(`${path(id)}/shots/${encodeURIComponent(shot)}/import`, 'POST', { expected_revision, asset_key }),
  upload: async (id: string, file: File) => {
    const target = await request<{ asset_key: string; upload_url: string; upload_headers?: Record<string, string>; revision: number }>(`${path(id)}/uploads`, 'POST', { content_type: file.type })
    const response = await fetch(target.upload_url, { method: 'PUT', headers: { ...target.upload_headers, 'Content-Type': file.type }, body: file })
    if (!response.ok) throw new Error('The image could not be uploaded. Try again.')
    return target
  },
  cut: (id: string, body: { expected_revision: number; clips: CutClip[] }) => request<Movie>(`${path(id)}/cut`, 'PUT', body),
  soundtracks: (id: string, expected_revision: number, tracks: AudioTrack[]) => request<Movie>(`${path(id)}/soundtracks`, 'PUT', { expected_revision, tracks }),
  characters: (id: string, expected_revision: number, characters: CharacterDefinition[]) => request<Movie>(`${path(id)}/characters`, 'PUT', { expected_revision, characters }),
  portrait: (id: string, character: string, expected_revision: number, request_id: string) => request<Movie>(`${path(id)}/characters/${encodeURIComponent(character)}/portrait`, 'POST', { expected_revision, request_id }),
  audition: (id: string, character: string, expected_revision: number, request_id: string) => request<Movie>(`${path(id)}/characters/${encodeURIComponent(character)}/audition`, 'POST', { expected_revision, request_id }),
  storyboard: (id: string, expected_revision: number, request_id: string) => request<Movie>(`${path(id)}/storyboard`, 'POST', { expected_revision, request_id }),
  assemble: (id: string, expected_revision: number, request_id: string) => request<Movie>(`${path(id)}/assemble`, 'POST', { expected_revision, request_id }),
  release: (id: string, expected_revision: number, name: string) => request<Movie>(`${path(id)}/release`, 'POST', { expected_revision, name }),
  history: (id: string) => request<Movie[]>(`${path(id)}/history`),
  /** The cut Generate movie saves, built by the server from the selected takes. */
  selectedCut: (id: string) => request<CutClip[]>(`${path(id)}/selected-cut`),
  downloadReleaseUrl: (id: string, releaseId: string) => `${root}${path(id)}/releases/${encodeURIComponent(releaseId)}/download?proxy=true`,
  assetUrl: (id: string, key: string) => `${root}${path(id)}/assets/${key.split('/').map(encodeURIComponent).join('/')}${key.toLowerCase().endsWith('.mp4') ? '?proxy=true' : ''}`,
}

export const selectedTake = (shot: MovieShot) => shot.takes.find((take) => take.take_id === shot.selected_take_id)
export const takeActive = (take: MovieTake) => ['waiting', 'submitting', 'running'].includes(take.status)
export const movieActive = (movie: Movie) => ['generating', 'assembling'].includes(movie.build?.status ?? '') || movie.shots.some((shot) => shot.takes.some(takeActive)) || Object.values(movie.compositions ?? {}).some((composition) => composition.status === 'pending' || (composition.status === 'superseded' && composition.provider === 'pool' && !composition.settled)) || ['waiting', 'pending', 'running', 'submitting'].includes(movie.assembly?.status ?? '')

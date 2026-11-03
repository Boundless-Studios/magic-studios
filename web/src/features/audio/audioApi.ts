import { redirectToLogin } from '../../api/auth'
import { ApiError } from '../../api/client'
import { apiQueue } from '../../api/requestQueue'

export type CueKind = 'line' | 'sfx' | 'ambience' | 'music'
export type LicencePolicy = 'cc0' | 'cc0_cc_by'
export interface CueInput {
  cue_id: string; name: string; kind: CueKind; prompt: string; text: string
  target_seconds: number | null; loop: boolean; keep: number
}
export interface Cue extends CueInput { selected: string[] }
export interface Licence {
  spdx: string; licence_url: string; author: string; source: 'freesound' | 'upload'; source_id: string
  source_url: string; retrieved_at: string; variant: 'preview' | 'original'; attribution_required: boolean
}
export interface Review {
  outcome: 'keep' | 'reject'; note: string; reason: string; sha256: string
  actor_id: string; actor_kind: 'session' | 'api_key'; at: string
}
export interface Candidate {
  candidate_id: string; cue_id: string; origin: 'freesound' | 'upload'; asset_key: string; sha256: string
  byte_length: number; media_type: 'audio/mpeg' | 'audio/wav'; duration_seconds: number | null
  measured: { integrated_lufs: number | null; true_peak_dbtp: number | null; centroid_hz: number | null } | null
  rendition_key: string | null; licence: Licence; provenance: Record<string, unknown>; review: Review | null; created_at: string
}
export interface AudioRelease {
  release_id: string; name: string; asset_key: string; manifest_sha256: string; source_revision: number
  created_at: string; file_count: number; attribution_required: boolean
}
export interface AudioSet {
  set_id: string; owner_id: string; name: string; brief: string; revision: number; licence_policy: LicencePolicy
  cues: Cue[]; candidates: Candidate[]; releases: AudioRelease[]; upload_keys: string[]
}
export interface AudioCapabilities {
  account_tier: 'invited' | 'public'; free_sounds_available: boolean; upload_available: boolean
  processing_available: boolean; replace_original_available: boolean; send_to_movie_available: boolean
  generation_available: boolean; max_cues: number; max_candidates_per_cue: number; max_file_bytes: number; upload_types: string[]
}
export interface FreeSound {
  source_id: string; name: string; author: string; spdx: string; licence_url: string; attribution_required: boolean
  duration_seconds: number; preview_url: string; source_url: string; tags: string[]
}
export interface Readiness {
  ready: boolean; problems: { cue_id: string | null; message: string }[]; files: number
  attribution_required: boolean; previews: number
}
export interface SetDraft { name: string; brief: string; licence_policy: LicencePolicy; cues: CueInput[] }

const root = '/studio/audio'
async function request<T>(path: string, method = 'GET', body?: object): Promise<T> {
  return apiQueue.run(() => fetch(`${root}${path}`, { method, headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) }), async (response) => {
    if (!response.ok) {
      if (response.status === 401) redirectToLogin()
      const data = await response.json().catch(() => null) as { detail?: unknown } | null
      const detail = data?.detail
      const message = response.status === 409 && typeof detail === 'object' && detail && 'current_revision' in detail ? 'This audio set changed elsewhere. Reload it before saving again.'
        : typeof detail === 'string' ? detail
          : typeof detail === 'object' && detail && 'message' in detail ? String((detail as { message: unknown }).message)
            : response.status === 422 ? 'Check the cue settings and try again.' : 'The audio request could not be completed. Try again.'
      throw new ApiError(response.status, message)
    }
    return (response.status === 204 ? undefined : response.json()) as Promise<T>
  })
}

const path = (id: string) => `/${encodeURIComponent(id)}`
const segment = (value: string) => encodeURIComponent(value)

// Browsers name WAV and MP3 several ways; the API accepts only these two.
const WAV_TYPES = new Set(['audio/wav', 'audio/x-wav', 'audio/wave', 'audio/vnd.wave', 'audio/x-pn-wav'])
const MP3_TYPES = new Set(['audio/mpeg', 'audio/mp3', 'audio/x-mp3', 'audio/mpeg3', 'audio/x-mpeg'])
export function audioContentType(file: File): string {
  const type = file.type.toLowerCase()
  if (WAV_TYPES.has(type)) return 'audio/wav'
  if (MP3_TYPES.has(type)) return 'audio/mpeg'
  const name = file.name.toLowerCase()
  return name.endsWith('.mp3') ? 'audio/mpeg' : name.endsWith('.wav') ? 'audio/wav' : type
}

async function uploadFile(id: string, file: File): Promise<{ asset_key: string; revision: number }> {
  const content_type = audioContentType(file)
  const target = await request<{ asset_key: string; upload_url: string; upload_headers?: Record<string, string>; revision: number }>(`${path(id)}/uploads`, 'POST', { content_type })
  const response = await fetch(target.upload_url, { method: 'PUT', headers: { ...target.upload_headers, 'Content-Type': content_type }, body: file })
  if (!response.ok) throw new Error('The audio file could not be uploaded. Try again.')
  return target
}

export const audioApi = {
  capabilities: () => request<AudioCapabilities>('/capabilities'),
  create: (name: string) => request<AudioSet>('', 'POST', { name }),
  list: () => request<AudioSet[]>(''),
  get: (id: string) => request<AudioSet>(path(id)),
  update: (id: string, expected_revision: number, draft: SetDraft) => request<AudioSet>(path(id), 'PUT', { expected_revision, ...draft }),
  remove: (id: string, confirm_name: string) => request<void>(path(id), 'DELETE', { confirm_name }),
  search: (id: string, q: string, max_seconds: number) => request<FreeSound[]>(`${path(id)}/free-sounds?${new URLSearchParams({ q, max_seconds: String(max_seconds) })}`),
  importSound: (id: string, cue: string, expected_revision: number, source_id: string) => request<AudioSet>(`${path(id)}/cues/${segment(cue)}/import`, 'POST', { expected_revision, source: 'freesound', source_id }),
  addUpload: async (id: string, cue: string, file: File) => {
    const target = await uploadFile(id, file)
    return request<AudioSet>(`${path(id)}/cues/${segment(cue)}/upload`, 'POST', { expected_revision: target.revision, asset_key: target.asset_key, filename: file.name.slice(0, 200) })
  },
  replaceOriginal: async (id: string, candidate: string, file: File) => {
    const target = await uploadFile(id, file)
    return request<AudioSet>(`${path(id)}/candidates/${segment(candidate)}/original`, 'POST', { expected_revision: target.revision, asset_key: target.asset_key })
  },
  // `sha256` is the file on screen: a verdict on a file replaced since is refused.
  review: (id: string, candidate: string, expected_revision: number, outcome: 'keep' | 'reject', note: string, sha256: string) => request<AudioSet>(`${path(id)}/candidates/${segment(candidate)}/review`, 'POST', { expected_revision, outcome, note, sha256 }),
  select: (id: string, cue: string, expected_revision: number, candidate_ids: string[]) => request<AudioSet>(`${path(id)}/cues/${segment(cue)}/selection`, 'PUT', { expected_revision, candidate_ids }),
  readiness: (id: string) => request<Readiness>(`${path(id)}/readiness`),
  release: (id: string, expected_revision: number, name: string) => request<AudioSet>(`${path(id)}/release`, 'POST', { expected_revision, name }),
  assetUrl: (id: string, key: string) => `${root}${path(id)}/assets/${key.split('/').map(segment).join('/')}`,
  releaseUrl: (id: string, release: string) => `${root}${path(id)}/releases/${segment(release)}/download`,
}

export const draftFrom = (audioSet: AudioSet): SetDraft => ({
  name: audioSet.name, brief: audioSet.brief, licence_policy: audioSet.licence_policy,
  cues: audioSet.cues.map((cue) => ({
    cue_id: cue.cue_id, name: cue.name, kind: cue.kind, prompt: cue.prompt, text: cue.text,
    target_seconds: cue.target_seconds, loop: cue.loop, keep: cue.keep,
  })),
})

export const kept = (candidate: Candidate) => candidate.review?.outcome === 'keep' && candidate.review.sha256 === candidate.sha256

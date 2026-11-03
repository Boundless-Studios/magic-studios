import { redirectToLogin } from '../../api/auth'
import { ApiError } from '../../api/client'
import { apiQueue } from '../../api/requestQueue'

export type FlightTakeStatus = 'waiting' | 'submitting' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export interface FlightTake {
  take_id: string; request_id: string; status: FlightTakeStatus; stale: boolean; error: string | null
  video_key: string | null; seed: number | null; width: number; height: number; fps: number
  length_frames: number | null; steps: number | null; duration_seconds: number
  first_frame_diff: number | null; last_frame_diff: number | null
  executor_preset: string | null; completed_by_worker_id: string | null; job_id: string
}
export interface FlightLeg {
  leg_id: string; index: number; name: string; origin_image_key: string; destination_image_key: string
  prompt: string; negative_prompt: string; duration_seconds: number; selected_take_id: string | null; takes: FlightTake[]
}
export interface FlightRecipe { width: number; height: number; fps: number; steps: number; default_duration_seconds: number; min_duration_seconds: number; max_duration_seconds: number; duration_step_seconds: number; max_legs: number }
export interface SceneFlight {
  flight_id: string; name: string; revision: number; generation_available: boolean; recipe: FlightRecipe
  images: string[]; legs: FlightLeg[]
  chain: { request_id: string; status: 'pending' | 'succeeded' | 'failed'; video_key: string | null; error: string | null; joins: { index: number; at_seconds: number; diff: number; limit: number }[] } | null
}
export interface FlightSummary { flight_id: string; name: string; revision: number; leg_count: number; active_takes: number; expires_at?: string | null }
export interface LegDraft {
  leg_id: string; origin_image_key: string; destination_image_key: string; prompt: string
  origin_name: string; destination_name: string; negative_prompt: string; duration_seconds: number; name: string
}

const root = '/studio/scene-flights'
async function request<T>(path: string, method = 'GET', body?: object): Promise<T> {
  return apiQueue.run(() => fetch(`${root}${path}`, { method, headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) }), async (response) => {
    if (!response.ok) {
      if (response.status === 401) redirectToLogin()
      const data = await response.json().catch(() => null) as { detail?: unknown } | null
      const detail = data?.detail
      const message = response.status === 409 && typeof detail === 'object' ? 'This flight changed elsewhere. Reload it before saving again.'
        : typeof detail === 'string' ? detail
          : typeof detail === 'object' && detail && 'message' in detail ? String((detail as { message: unknown }).message)
            : response.status === 422 ? 'Check the leg settings and try again.' : 'The scene flight request could not be completed. Try again.'
      throw new ApiError(response.status, message)
    }
    return response.json() as Promise<T>
  })
}

const path = (id: string) => `/${encodeURIComponent(id)}`
const segment = (value: string) => encodeURIComponent(value)

async function base64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer())
  let binary = ''
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  return btoa(binary)
}

export const flightApi = {
  create: (name: string) => request<SceneFlight>('', 'POST', { name }),
  list: () => request<FlightSummary[]>(''),
  get: (id: string) => request<SceneFlight>(path(id)),
  addImage: async (id: string, file: File) => request<{ image_key: string; revision: number; width: number; height: number }>(`${path(id)}/images`, 'POST', { content_type: file.type, content_base64: await base64(file) }),
  setLegs: (id: string, expected_revision: number, legs: LegDraft[]) => request<SceneFlight>(`${path(id)}/legs`, 'PUT', { expected_revision, legs }),
  generate: (id: string, leg: string, expected_revision: number, request_id: string) => request<SceneFlight>(`${path(id)}/legs/${segment(leg)}/takes`, 'POST', { expected_revision, request_id }),
  select: (id: string, leg: string, expected_revision: number, take_id: string) => request<SceneFlight>(`${path(id)}/legs/${segment(leg)}/select`, 'POST', { expected_revision, take_id }),
  cancel: (id: string, leg: string, take: string, expected_revision: number) => request<SceneFlight>(`${path(id)}/legs/${segment(leg)}/takes/${segment(take)}/cancel`, 'POST', { expected_revision }),
  chain: (id: string, expected_revision: number, request_id: string) => request<SceneFlight>(`${path(id)}/chain`, 'POST', { expected_revision, request_id }),
  imageUrl: (id: string, key: string) => `${root}${path(id)}/assets/${key.split('/').map(segment).join('/')}`,
  takeVideoUrl: (id: string, leg: string, take: string, download = false) => `${root}${path(id)}/legs/${segment(leg)}/takes/${segment(take)}/video${download ? '?download=true' : ''}`,
  chainVideoUrl: (id: string, download = false) => `${root}${path(id)}/chain/video${download ? '?download=true' : ''}`,
}

export const flightTakeActive = (take: FlightTake) => ['waiting', 'submitting', 'running'].includes(take.status)
export const flightActive = (flight: SceneFlight) => flight.legs.some((leg) => leg.takes.some(flightTakeActive)) || flight.chain?.status === 'pending'
export const draftsFrom = (flight: SceneFlight): LegDraft[] => flight.legs.map((leg) => ({
  leg_id: leg.leg_id, origin_image_key: leg.origin_image_key, destination_image_key: leg.destination_image_key,
  prompt: leg.prompt, origin_name: '', destination_name: '', negative_prompt: leg.negative_prompt, duration_seconds: leg.duration_seconds, name: leg.name,
}))

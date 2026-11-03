import type { AccessRequestPage, GenerationConfig, ProviderKey, ActivityPage, ApiKey, EnrolledWorker, PairingToken, CancelAllResult, CandidateFramesResponse, CompositionModel, CreatedApiKey, ExportRecord, ExternalSubject, GenerateMotionsResult, HistoryEntry, LoopPolicy, LoopSuggestionResponse, MotionGenerationSettings, MotionPreset, MotionReference, NormalizationSpec, Operation, PoolQueueResponse, PoolWorkersResponse, PresetInfo, ProjectRepairReport, ProjectResponse, ProjectSummary, Release, ReleaseDelivery, SignOutResult, StylePreset, SubjectRevisionEdit, TargetInfo, UploadTarget, SpriteLayout, ViewId } from './types'
import { DEFAULT_MOTION_GENERATION_SETTINGS } from './types'
import { redirectToLogin } from './auth'
import { apiQueue } from './requestQueue'

export interface MotionDefinition { view_id?: ViewId | null; motion_id: string; name: string; kind: 'preset' | 'custom'; preset?: MotionPreset; action?: string | null; prompt?: string; loop_policy?: LoopPolicy; required?: boolean; references?: MotionReference[]; style?: StylePreset | null; script?: string | null }

export const CONFLICT_MESSAGE = 'This project changed elsewhere. Your view has been refreshed.'
const jsonHeaders = { 'Content-Type': 'application/json', 'X-Studio-Setup-Version': '1' }

export class ApiError extends Error { code?: string; detail?: unknown; constructor(public status: number, message: string, code?: string, detail?: unknown) { super(message); this.code = code; this.detail = detail } }
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return apiQueue.run(() => fetch(path, { ...init, headers: { ...jsonHeaders, ...init?.headers } }), async (response) => {
    if (!response.ok) {
      // The session expired or was never there: send the user to sign in, keeping this path.
      if (response.status === 401) redirectToLogin()
      let detail: unknown; try { detail = await response.json() } catch { detail = null }
      const { message, code } = errorMessageAndCode(response.status, detail)
      throw new ApiError(response.status, message, code, detail)
    }
    return response.json() as Promise<T>
  })
}
/** Discovers a human-readable message (and optional machine `code`) from a rejected request's
 * JSON body. The backend uses three shapes for a mutation rejection: `{"detail": "..."}`,
 * `{"detail": {"code": ..., "message": ...}}`, and (rarely) a top-level `{"code", "message"}`.
 * A 409 means the shared "changed elsewhere" notice only when the body is the server's
 * canonical revision-conflict shape (`{"detail": {"message": "expected revision is stale",
 * "current_revision": N}}`, from `_project_mutation_exception`) -- every other 409 (and every
 * other status) surfaces the server's own message so a blocked action never reads as a
 * phantom conflict. */
function errorMessageAndCode(status: number, payload: unknown): { message: string; code?: string } {
  const record = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : null
  const detail = record?.detail
  const detailRecord = detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : null
  if (status === 409 && detailRecord && typeof detailRecord.current_revision === 'number') {
    return { message: CONFLICT_MESSAGE }
  }
  let message: string | undefined
  let code: string | undefined
  if (typeof detail === 'string') message = detail
  else if (detailRecord) {
    if (typeof detailRecord.message === 'string') message = detailRecord.message
    if (typeof detailRecord.code === 'string') code = detailRecord.code
  } else if (record && typeof record.message === 'string') {
    message = record.message
    if (typeof record.code === 'string') code = record.code
  }
  if (message) return { message, code }
  if (status === 409) return { message: CONFLICT_MESSAGE }
  if (status === 422) return { message: 'Check the file or settings and try again.' }
  if (status === 503) return { message: 'The generation or storage service is temporarily unavailable. Try again shortly.' }
  return { message: 'The request could not be completed.' }
}
/** Server messages come back lowercase-first ("motion generation requires..."); this makes them
 * read as a sentence in the UI without touching the rest of the casing. */
export function sentenceCase(message: string): string {
  return message.length ? message[0].toUpperCase() + message.slice(1) : message
}
const post = <T>(path: string, body: object) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })
const patch = <T>(path: string, body: object) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body) })
function requestId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

/** Permanent deletes take the item's name as confirmation and answer 204 with no body. */
async function remove(path: string, body: object): Promise<void> {
  return apiQueue.run(() => fetch(path, { method: 'DELETE', headers: jsonHeaders, body: JSON.stringify(body) }), async (response) => {
    if (response.ok) return
    if (response.status === 401) redirectToLogin()
    let detail: unknown; try { detail = await response.json() } catch { detail = null }
    const { message, code } = errorMessageAndCode(response.status, detail)
    throw new ApiError(response.status, message, code)
  })
}

/** Sign-out bypasses `request`, whose 401 bounce to /login would be wrong on the sign-out path. */
async function authRequest(path: string, init: RequestInit): Promise<Response> {
  return fetch(path, { credentials: 'same-origin', ...init, headers: { ...jsonHeaders, ...init.headers } })
}

export const studioApi = {
  startTurnaround: (id: string, expectedRevision: number, sourceViewId: string, sourceRevisionId: string, equipment: import('./types').TurnaroundEquipment[] = []) => post<{ turnaround_id: string; operation: Operation }>(`/studio/projects/${id}/turnarounds`, { expected_revision: expectedRevision, source_view_id: sourceViewId, source_revision_id: sourceRevisionId, equipment, request_id: requestId() }),
  turnarounds: (id: string) => request<{ turnarounds: import('./types').Turnaround[] }>(`/studio/projects/${id}/turnarounds`),
  turnaround: (id: string, turnaroundId: string) => request<import('./types').Turnaround>(`/studio/projects/${id}/turnarounds/${encodeURIComponent(turnaroundId)}`),
  turnaroundFrames: (id: string, turnaroundId: string, start = 0, limit = 32, stride = 1) => request<CandidateFramesResponse>(`/studio/projects/${id}/turnarounds/${encodeURIComponent(turnaroundId)}/frames?start=${start}&limit=${limit}&stride=${stride}`),
  saveTurnaroundPicks: (id: string, turnaroundId: string, expectedPickRevision: number, picks: Partial<Record<import('./types').CompassHeading, import('./types').TurnaroundPickEntry>>) => request<import('./types').TurnaroundPickSet>(`/studio/projects/${id}/turnarounds/${encodeURIComponent(turnaroundId)}/picks`, { method: 'PUT', body: JSON.stringify({ expected_pick_revision: expectedPickRevision, picks }) }),
  createViewDraft: (id: string, expectedRevision: number, turnaroundId: string, pickRevision: number, heading: import('./types').CompassHeading) => post<{ draft_id: string; draft: import('./types').ViewDraft }>(`/studio/projects/${id}/view-drafts`, { expected_revision: expectedRevision, turnaround_id: turnaroundId, pick_revision: pickRevision, heading, standing: false, request_id: requestId() }),
  viewDrafts: (id: string) => request<{ drafts: import('./types').ViewDraft[] }>(`/studio/projects/${id}/view-drafts`),
  checkViewDraft: (id: string, draftId: string, expectedRevision: number, check: { facing: boolean; equipment: boolean; identity: boolean }) => post<import('./types').ViewDraft>(`/studio/projects/${id}/view-drafts/${encodeURIComponent(draftId)}/check`, { expected_revision: expectedRevision, ...check }),
  discardViewDraft: (id: string, draftId: string, expectedRevision: number) => post<import('./types').ViewDraft>(`/studio/projects/${id}/view-drafts/${encodeURIComponent(draftId)}/discard`, { expected_revision: expectedRevision }),
  stageSetupReference: async (id: string, file: File) => {
    const content_base64 = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('Could not read artwork.')); reader.readAsDataURL(file) })
    return post<import('./types').ViewReference>(`/studio/projects/${id}/setup/references`, { content_base64, content_type: file.type, filename: file.name })
  },
  abandonSetupDispatch: (id: string, operationId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/setup/dispatches/${encodeURIComponent(operationId)}/abandon`, { expected_revision: expectedRevision, confirm_dispatch_stopped: true }),
  setupCapabilities: () => request<{ schema_versions: number[] }>('/studio/setup-capabilities'),
  setup: (id: string) => request<{ revision: number; setup: import('./types').ProjectSetup }>(`/studio/projects/${id}/setup`),
  previewSetup: (id: string, expectedRevision: number, desiredSetup: import('./types').ProjectSetup) => post<import('./types').SetupPlan>(`/studio/projects/${id}/setup/preview`, { expected_revision: expectedRevision, desired_setup: desiredSetup }),
  applySetup: (id: string, plan: import('./types').SetupPlan) => post<ProjectResponse>(`/studio/projects/${id}/setup/apply`, { expected_revision: plan.expected_revision, plan_token: plan.plan_token, confirmed_impacts: plan.impacts.map((impact) => impact.impact_id) }),
  getGenerationConfig: () => request<GenerationConfig>('/studio/generation-config'),
  signOut: async (): Promise<SignOutResult> => {
    const response = await authRequest('/studio/auth/sign-out', { method: 'POST' })
    if (!response.ok) throw new ApiError(response.status, 'Sign-out failed. Try again.')
    const payload = await response.json().catch(() => null) as { logout_url?: unknown } | null
    return { logout_url: typeof payload?.logout_url === 'string' && payload.logout_url ? payload.logout_url : null }
  },
  deleteProject: (id: string, confirmName: string) => remove(`/studio/projects/${encodeURIComponent(id)}`, { confirm_name: confirmName }),
  deleteMovie: (id: string, confirmName: string) => remove(`/studio/movies/${encodeURIComponent(id)}`, { confirm_name: confirmName }),
  deleteFlight: (id: string, confirmName: string) => remove(`/studio/scene-flights/${encodeURIComponent(id)}`, { confirm_name: confirmName }),
  /** Deletes the signed-in account and everything in it, then hands back the Auth0 logout URL. */
  deleteAccount: async (confirmEmail: string): Promise<SignOutResult> => {
    const response = await authRequest('/studio/account', { method: 'DELETE', body: JSON.stringify({ confirm_email: confirmEmail }) })
    const payload = await response.json().catch(() => null) as { logout_url?: unknown; detail?: unknown } | null
    if (!response.ok) {
      const { message, code } = errorMessageAndCode(response.status, payload)
      throw new ApiError(response.status, message, code)
    }
    return { logout_url: typeof payload?.logout_url === 'string' && payload.logout_url ? payload.logout_url : null }
  },
  listAccessRequests: (status: 'waiting' | 'invited', limit: number, offset = 0) =>
    request<AccessRequestPage>(`/studio/access-requests?status=${status}&limit=${limit}&offset=${offset}`),
  dismissAccessRequest: async (email: string): Promise<void> => {
    const response = await fetch(`/studio/access-requests?email=${encodeURIComponent(email)}`, { method: 'DELETE', headers: jsonHeaders })
    if (response.status === 401) redirectToLogin()
    if (!response.ok) throw new ApiError(response.status, 'The request could not be removed. Try again.')
  },
  listApiKeys: () => request<ApiKey[]>('/studio/api-keys'),
  createApiKey: (name: string) => post<CreatedApiKey>('/studio/api-keys', { name }),
  // The route answers 204 in the contract and a JSON body in the current backend; neither is read.
  revokeApiKey: async (keyId: string): Promise<void> => {
    const response = await fetch(`/studio/api-keys/${encodeURIComponent(keyId)}`, { method: 'DELETE', headers: jsonHeaders })
    if (response.status === 401) redirectToLogin()
    if (!response.ok) throw new ApiError(response.status, 'The key could not be revoked. Try again.')
  },
  listProviderKeys: () => request<ProviderKey[]>('/studio/providers'),
  saveProviderKey: (provider: string, apiKey: string) => request<ProviderKey>(`/studio/providers/${encodeURIComponent(provider)}`, { method: 'PUT', body: JSON.stringify({ api_key: apiKey }) }),
  deleteProviderKey: (provider: string) => request<ProviderKey>(`/studio/providers/${encodeURIComponent(provider)}`, { method: 'DELETE' }),
  listWorkers: () => request<EnrolledWorker[]>('/studio/workers'),
  // Minted on demand, never listed: a live token exists only in the panel that
  // reveals it, so asking again mints a new one rather than re-showing the old.
  mintPairingToken: () => post<PairingToken>('/studio/workers/pairing-tokens', {}),
  renameWorker: (workerId: string, label: string) =>
    patch<EnrolledWorker>(`/studio/workers/${encodeURIComponent(workerId)}`, { label }),
  revokeWorker: async (workerId: string): Promise<void> => {
    const response = await fetch(`/studio/workers/${encodeURIComponent(workerId)}`, { method: 'DELETE', headers: jsonHeaders })
    if (response.status === 401) redirectToLogin()
    if (!response.ok) throw new ApiError(response.status, 'The machine could not be removed. Try again.')
  },
  listProjects: () => request<ProjectSummary[]>('/studio/projects'),
  createProject: (name: string, externalSubject: ExternalSubject | null = null, presetMotions: MotionPreset[] = []) => post<ProjectResponse>('/studio/projects', { name, external_subject: externalSubject, preset_motions: presetMotions }),
  targets: () => request<TargetInfo[]>('/studio/targets'),
  projectForExternalSubject: (system: string, entityKind: string, entityId: string) => request<ProjectResponse>(`/studio/projects/for-external-subject?${new URLSearchParams({ system, entity_kind: entityKind, entity_id: entityId }).toString()}`),
  project: (id: string) => request<ProjectResponse>(`/studio/projects/${id}`),
  patchProject: (id: string, expectedRevision: number, externalSubject: ExternalSubject | null) => request<ProjectResponse>(`/studio/projects/${id}`, { method: 'PATCH', body: JSON.stringify({ expected_revision: expectedRevision, external_subject: externalSubject }) }),
  operations: (id: string) => request<Operation[]>(`/studio/projects/${id}/operations`),
  // The live poller (ProjectActivity/operationPolling) only ever wants the freshest page,
  // so these unwrap the paginated envelope back to a flat list for it; History's
  // Operations tab (T14) pages through `activityPage` directly to reach older entries.
  activity: (id: string) => request<ActivityPage>(`/studio/projects/${id}/activity`).then((page) => page.operations),
  reconcileActivity: (id: string) => post<ActivityPage>(`/studio/projects/${id}/activity/reconcile`, {}).then((page) => page.operations),
  activityPage: (id: string, params: { cursor?: string; limit?: number; motionId?: string } = {}) => {
    const query = new URLSearchParams()
    if (params.cursor) query.set('cursor', params.cursor)
    if (params.limit) query.set('limit', String(params.limit))
    if (params.motionId) query.set('motion_id', params.motionId)
    const qs = query.toString()
    return request<ActivityPage>(`/studio/projects/${id}/activity${qs ? `?${qs}` : ''}`)
  },
  poolWorkers: () => request<PoolWorkersResponse>('/pool/workers'),
  poolQueue: () => request<PoolQueueResponse>('/pool/queue'),
  createUpload: (id: string, expectedRevision: number) => post<UploadTarget>(`/studio/projects/${id}/subject/uploads`, { content_type: 'image/png', expected_revision: expectedRevision }),
  createReferenceUpload: (id: string, expectedRevision: number, contentType: string) => post<UploadTarget>(`/studio/projects/${id}/subject/references/uploads`, { content_type: contentType, expected_revision: expectedRevision }),
  upload: async (target: UploadTarget, file: File) => { const response = await fetch(target.upload_url, { method: 'PUT', headers: { ...target.upload_headers, 'Content-Type': file.type }, body: file }); if (!response.ok) throw new ApiError(response.status, 'The image could not be uploaded.') },
  generateSubject: (id: string, expectedRevision: number, referenceKeys: string[], direction: string) => post<Operation>(`/studio/projects/${id}/subject/generate`, { expected_revision: expectedRevision, reference_keys: referenceKeys, direction: direction.trim() || null, request_id: requestId() }),
  settleGeneratedSubject: (id: string, jobId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/subject/operations/${jobId}/settle`, { expected_revision: expectedRevision }),
  preview: (id: string, assetKey: string) => post<UploadTarget>(`/studio/projects/${id}/assets/preview`, { asset_key: assetKey }),
  // Same asset, attachment disposition -- `preview` stays inline for <img>/<video>.
  assetDownload: (id: string, assetKey: string) => post<UploadTarget>(`/studio/projects/${id}/assets/download`, { asset_key: assetKey }),
  settleSubject: (id: string, expectedRevision: number, assetKey: string) => post<ProjectResponse>(`/studio/projects/${id}/subject/settle`, { expected_revision: expectedRevision, asset_key: assetKey }),
  // The server records every present finding as acknowledged with reason "acknowledged" on its
  // own -- the approval card has no note field to send (operator, 2026-09-06).
  approveSubject: (id: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/subject/approve`, { expected_revision: expectedRevision }),
  reframeSubjectRevision: (id: string, revisionId: string, expectedRevision: number, edit: SubjectRevisionEdit) => post<ProjectResponse>(`/studio/projects/${id}/subject/revisions/${revisionId}/reframe`, { expected_revision: expectedRevision, edit }),
  selectSubjectRevision: (id: string, revisionId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/subject/revisions/${revisionId}/select`, { expected_revision: expectedRevision }),
  rejectSubjectRevision: (id: string, revisionId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/subject/revisions/${revisionId}/reject`, { expected_revision: expectedRevision }),
  restoreSubjectRevision: (id: string, revisionId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/subject/revisions/${revisionId}/restore`, { expected_revision: expectedRevision }),
  presets: () => request<PresetInfo[]>('/studio/presets'),
  setSpriteLayout: (id: string, expectedRevision: number, spriteLayout: SpriteLayout) => request<ProjectResponse>(`/studio/projects/${id}/sprite-layout`, { method: 'PUT', body: JSON.stringify({ expected_revision: expectedRevision, sprite_layout: spriteLayout }) }),
  updateView: (id: string, viewId: ViewId, expectedRevision: number, patch: { name?: string; reference?: { key: string; sha256: string } | null }) => request<ProjectResponse>(`/studio/projects/${id}/views/${viewId}`, { method: 'PUT', body: JSON.stringify({ expected_revision: expectedRevision, ...patch }) }),
  createViewReferenceUpload: (id: string, viewId: ViewId, expectedRevision: number, contentType: string) => post<UploadTarget>(`/studio/projects/${id}/views/${viewId}/reference/uploads`, { expected_revision: expectedRevision, content_type: contentType }),
  addMotion: (id: string, expectedRevision: number, motion: MotionDefinition) => post<ProjectResponse>(`/studio/projects/${id}/motions`, { expected_revision: expectedRevision, required: true, loop_policy: 'loop', ...motion }),
  addMotions: (id: string, expectedRevision: number, motions: MotionDefinition[]) => post<ProjectResponse>(`/studio/projects/${id}/motions/batch`, { expected_revision: expectedRevision, motions: motions.map((motion) => ({ required: true, loop_policy: 'loop', ...motion })) }),
  updateMotion: (id: string, motionId: string, expectedRevision: number, motion: MotionDefinition) => request<ProjectResponse>(`/studio/projects/${id}/motions/${motionId}`, { method: 'PATCH', body: JSON.stringify({ expected_revision: expectedRevision, required: true, loop_policy: 'loop', ...motion }) }),
  createMotionReferenceUpload: (id: string, motionId: string, expectedRevision: number, contentType: string) => post<UploadTarget>(`/studio/projects/${id}/motions/${motionId}/references/uploads`, { content_type: contentType, expected_revision: expectedRevision }),
  // `action`/`actions` carry the operator's "what happens" lines: the server persists them as
  // one revision and dispatches from the recompiled prompt.
  generateMotion: (id: string, motionId: string, expectedRevision: number, settings: MotionGenerationSettings = DEFAULT_MOTION_GENERATION_SETTINGS, action: string | null = null) => post<Operation>(`/studio/projects/${id}/motions/${motionId}/generate`, { expected_revision: expectedRevision, request_id: requestId(), settings, action }),
  generateMotions: (id: string, expectedRevision: number, motionIds: string[], settings: MotionGenerationSettings = DEFAULT_MOTION_GENERATION_SETTINGS, sharedStartingPose = false, actions: Record<string, string> = {}) => post<GenerateMotionsResult>(`/studio/projects/${id}/motions/generate-batch`, { expected_revision: expectedRevision, request_id: requestId(), motion_ids: motionIds, settings, shared_starting_pose: sharedStartingPose, actions }),
  composeFrame: (id: string, expectedRevision: number, prompt: string, model: CompositionModel) => post<Operation>(`/studio/projects/${id}/composition/compose`, { expected_revision: expectedRevision, prompt, composition_model: model, request_id: requestId() }),
  approveComposition: (id: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/composition/approve`, { expected_revision: expectedRevision }),
  regenerateComposition: (id: string, expectedRevision: number, prompt: string, model: CompositionModel) => post<Operation>(`/studio/projects/${id}/composition/regenerate`, { expected_revision: expectedRevision, prompt, composition_model: model, request_id: requestId() }),
  settleMotion: (id: string, motionId: string, jobId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/motions/${motionId}/operations/${jobId}/settle`, { expected_revision: expectedRevision }),
  cancelMotionOperation: (id: string, motionId: string, jobId: string) => post<Operation>(`/studio/projects/${id}/motions/${motionId}/operations/${jobId}/cancel`, {}),
  cancelAllOperations: (id: string) => post<CancelAllResult>(`/studio/projects/${id}/operations/cancel-all`, {}),
  selectCandidate: (id: string, motionId: string, candidateId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/motions/${motionId}/candidates/${candidateId}/select`, { expected_revision: expectedRevision }),
  createMotionRevision: (id: string, motionId: string, expectedRevision: number, sourceCandidateId: string, orderedFrames: { index: number; hold: number }[], frameDurationMs: number, loopPolicy: LoopPolicy, flipHorizontal = false, targetFrameCount: number | null = null, cellSize: number | null = null, normalization: NormalizationSpec | null = null) => post<ProjectResponse>(`/studio/projects/${id}/motions/${motionId}/revisions`, { expected_revision: expectedRevision, source_candidate_id: sourceCandidateId, ordered_frames: orderedFrames, frame_duration_ms: frameDurationMs, loop_policy: loopPolicy, flip_horizontal: flipHorizontal, target_frame_count: targetFrameCount, cell_size: cellSize, normalization }),
  candidateFrames: (id: string, motionId: string, candidateId: string, start = 0, limit = 32) => request<CandidateFramesResponse>(`/studio/projects/${id}/motions/${motionId}/candidates/${candidateId}/frames?start=${start}&limit=${limit}`),
  suggestCandidateLoop: (id: string, motionId: string, candidateId: string, mode: 'walk' | 'attack' | 'idle', targetFrameCount: 8 | 12 | 16) => post<LoopSuggestionResponse>(`/studio/projects/${id}/motions/${motionId}/candidates/${candidateId}/loop-suggestion`, { mode, target_frame_count: targetFrameCount }),
  deriveAlpha: (id: string, motionId: string, candidateId: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/motions/${motionId}/candidates/${candidateId}/derive-alpha`, { expected_revision: expectedRevision }),
  candidateSheetDownload: (id: string, motionId: string, candidateId: string) => request<UploadTarget>(`/studio/projects/${id}/motions/${motionId}/candidates/${candidateId}/sheet.png`),
  candidatePreviewDownload: (id: string, motionId: string, candidateId: string, format: 'webm' | 'gif') => request<UploadTarget>(`/studio/projects/${id}/motions/${motionId}/candidates/${candidateId}/preview.${format}`),
  createCandidateUpload: (id: string, motionId: string, expectedRevision: number, contentType: string) => post<UploadTarget>(`/studio/projects/${id}/motions/${motionId}/candidates/uploads`, { content_type: contentType, expected_revision: expectedRevision }),
  settleCandidateUpload: (id: string, motionId: string, expectedRevision: number, assetKey: string, frameCount: number | null = null) => post<ProjectResponse>(`/studio/projects/${id}/motions/${motionId}/candidates/settle-upload`, { expected_revision: expectedRevision, asset_key: assetKey, frame_count: frameCount }),
  assessSet: (id: string, expectedRevision: number) => post<ProjectResponse>(`/studio/projects/${id}/quality/set`, { expected_revision: expectedRevision }),
  approveSet: (id: string, expectedRevision: number, waiverReason?: string) => post<ProjectResponse>(`/studio/projects/${id}/set/approve`, { expected_revision: expectedRevision, ...(waiverReason?.trim() ? { waiver_reason: waiverReason.trim() } : {}) }),
  releases: (id: string) => request<Release[]>(`/studio/projects/${id}/releases`),
  createRelease: (id: string, expectedRevision: number, name: string) => post<Release>(`/studio/projects/${id}/releases`, { expected_revision: expectedRevision, name }),
  retryDelivery: (releaseId: string, target: string) => post<ReleaseDelivery>(`/studio/releases/${releaseId}/deliveries/${target}`, {}),
  createExport: (releaseId: string, expectedRevision: number, profile: string) => post<ExportRecord>(`/studio/releases/${releaseId}/exports`, { profile, expected_revision: expectedRevision }),
  listReleaseExports: (releaseId: string) => request<ExportRecord[]>(`/studio/releases/${releaseId}/exports`),
  exportDownload: (exportId: string) => request<UploadTarget>(`/studio/exports/${exportId}/download`),
  history: (id: string) => request<HistoryEntry[]>(`/studio/projects/${id}/history`),
  repairProject: (id: string) => post<ProjectRepairReport>(`/studio/projects/${id}/repair`, {}),
}

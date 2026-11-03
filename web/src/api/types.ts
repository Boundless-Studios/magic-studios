export type ApprovalState = 'not_ready' | 'measuring' | 'needs_correction' | 'ready_for_signoff' | 'approved' | 'waived'
export type FindingSeverity = 'blocking' | 'warning' | 'info'
export type LoopPolicy = 'once' | 'loop' | 'ping_pong'
export interface NormalizationSpec { scale: number; source_anchor_x: number; source_anchor_y: number; target_anchor_x: number; target_anchor_y: number; padding?: number; cleanup_green_edges?: boolean; reference_candidate_id?: string; reference_sha256?: string }
export interface CandidateFrameRecord { index: number; timestamp_ms: number; image: string; pose: number[]; edge_contact: boolean | null; foreground_bbox?: [number, number, number, number] | null }
export interface CandidateFramesResponse { analysis_version: string; candidate_id: string; source_sha256: string; frame_count: number; width: number; height: number; duration_ms: number; fps: number; index_space: 'stored_source_frames'; frames: CandidateFrameRecord[] }
export interface LoopSuggestionResponse { analysis_version: string; mode: 'walk' | 'attack' | 'idle'; candidate_id: string; source_sha256: string; status: 'complete' | 'incomplete'; start: number | null; end: number | null; ordered_frames: number[]; frame_duration_ms: number | null; loop_policy: LoopPolicy; edge_clipped_frames: number[]; warnings: string[]; visual_review_required: boolean; foreground_bottom_delta_px?: number }
export type MotionKind = 'preset' | 'custom'
export type JobStatus = 'queued' | 'processing' | 'completed' | 'failed' | 'cancelled'
export type OperationStage = 'queued' | 'generating' | 'awaiting_settlement' | 'settled' | 'failed' | 'cancelled'
export type JobFailureCode = 'invalid_input' | 'unsupported_operation' | 'provider_unavailable' | 'temporary_failure'
export type RevisionLifecycle = 'draft' | 'measuring' | 'ready' | 'failed' | 'archived' | 'rejected'
export type SubjectRevisionSource = 'generated' | 'uploaded' | 'edited'
export type SubjectRevisionEdit = 'reframe' | 'cleanup'

export interface Approval { state: ApprovalState; assessment_id: string | null; approver_id: string | null; policy_version: string | null; artifact_sha256: string | null; manifest_digest: string | null; waived_finding_ids: string[]; waiver_reason: string | null }
export interface QualityFinding { code: string; severity: FindingSeverity; message: string; action_id: string | null }
export interface QualityAssessment { policy_version: string; findings: QualityFinding[] }
export interface SubjectFinding { code: 'subject_dimension_too_small' | 'subject_missing_alpha' | 'subject_touches_canvas_edge' | 'subject_occupancy_too_low' | 'subject_occupancy_too_high'; severity: FindingSeverity; title: string; explanation: string; artifact_sha256: string; action_id: string | null }
export interface SubjectAssessment { assessment_id: string; policy_version: string; artifact_sha256: string; width: number; height: number; occupancy_ratio: number; findings: SubjectFinding[] }
export interface SubjectRevision { revision_id: string; artifact_key: string; sha256: string; width: number; height: number; lifecycle: RevisionLifecycle; parent_revision_ids: string[]; source: SubjectRevisionSource; direction: string | null; created_at: string | null; assessment?: SubjectAssessment | null }
export interface SubjectState { current_revision: SubjectRevision | null; revisions?: SubjectRevision[]; assessment: SubjectAssessment | null; approval: Approval }
export interface ExternalSubject { system: string; entity_kind: string; entity_id: string; display_name: string }
export interface TargetSlot { name: string; accepts: string[]; winner: string }
export interface TargetInfo { system: string; display_name: string; entity_kinds: string[]; slots: TargetSlot[]; delivery_configured: boolean }
export type CompositionModel = 'nanobanana_2_lite' | 'nanobanana_2_flash' | 'nanobanana_2' | 'seedream_5_0_lite'
export type CompositionApproval = 'pending' | 'approved' | 'superseded'
export interface CompositionRevisionInfo { revision_id: string; artifact_key: string; sha256: string; prompt: string; model: CompositionModel; parent_subject_revision_id: string; approval: CompositionApproval }
export interface CompositionState { current: CompositionRevisionInfo | null }
export type MotionReferenceRole = 'start' | 'context' | 'end'
export interface MotionReference { key: string; sha256: string; role: MotionReferenceRole }
export type StylePreset = 'high_fantasy' | 'cyberpunk' | 'steampunk' | 'space_corporate' | 'grimdark' | 'post_apocalyptic' | 'noir' | 'psychedelic' | 'ancient_mythology' | 'western'
export interface ExportPackingEstimate { profile: string; width: number; height: number }
export type MediaKind = 'video' | 'sheet'
export type Facing = 'left' | 'right'
export interface MotionMetadata { frame_rate?: string | null; time_base?: string | null; duration_ms?: number | null; decodable: boolean; media_kind: MediaKind; frame_count: number; width: number; height: number; constant_geometry: boolean; has_alpha: boolean; duplicate_terminal_frame: boolean; baseline_y: number | null; pivot_x?: number | null; pivot_y?: number | null; packing_estimate: ExportPackingEstimate }
export type Backdrop = 'native_alpha' | 'chroma_green'
export type CandidateSource = 'generated' | 'upload' | 'derived'
export interface MotionGenerationSettings { preset: 'quick' | 'standard' | 'high' | 'cinematic'; steps: number; seed: number; width: number; height: number; duration_seconds: number; backdrop: Backdrop; provider_parameters: Record<string, boolean | number | string> }
export const DEFAULT_MOTION_GENERATION_SETTINGS: MotionGenerationSettings = { preset: 'standard', steps: import.meta.env.VITE_MAGIC_DESKTOP_RECIPE === 'balanced' ? 8 : 20, seed: 42, width: 512, height: 512, duration_seconds: 4, backdrop: 'chroma_green', provider_parameters: import.meta.env.VITE_MAGIC_DESKTOP_RECIPE === 'balanced' ? { execution_profile: 'h3-w4a8-16gb-lightx8' } : {} }
export interface DerivationRecipe { op: string; key: string | null; similarity: number | null; blend: number | null; target_frame_count: number | null; cell_size: number | null }
export interface MotionCandidate { candidate_id: string; artifact_key: string; sha256: string; duration_ms: number | null; metadata: MotionMetadata | null; assessment: QualityAssessment | null; ordered_frames: number[] | null; frame_duration_ms: number | null; loop_policy: LoopPolicy | null; generation_settings: MotionGenerationSettings | null; facing: Facing; frame_holds?: number[] | null; composition_revision_id?: string | null; recipe: DerivationRecipe | null; source: CandidateSource; subject_revision_id?: string | null; motion_spec_digest?: string | null }
export type SpriteLayout = 'unassigned' | 'single_view' | 'side_view' | 'isometric'
export type Heading = 'left' | 'right' | 'N' | 'NE' | 'E' | 'SE' | 'S' | 'SW' | 'W' | 'NW'
export type ViewId = string
export interface ViewReference { revision_id: string; key: string; sha256: string; width: number; height: number }
export interface CharacterView { view_id: ViewId; heading?: Heading | null; name: string; reference: ViewReference | null }
export interface Motion { view_id?: ViewId | null; view_reference?: ViewReference | null; motion_id: string; name: string; kind: MotionKind; preset?: MotionPreset | null; semantic_slot: string | null; loop_policy: LoopPolicy; required: boolean; action?: string | null; spec_digest?: string; prompt?: string | null; references?: MotionReference[]; style?: StylePreset | null; script?: string | null; candidates: MotionCandidate[]; selected_candidate_id: string | null }
export interface ManifestSummary { sprite_layout?: SpriteLayout; views?: CharacterView[]; project_id: string; name: string; owner_id: string | null; external_subject: ExternalSubject | null; subject: SubjectState; composition: CompositionState; motions: Motion[]; set_assessment: QualityAssessment | null; set_assessment_id: string | null; set_assessment_digest: string | null; set_assessment_fresh: boolean; set_approval: Approval; set_approval_fresh: boolean }
export interface ProjectResponse { project_id: string; revision: number; manifest_digest: string; updated_at: string; manifest: ManifestSummary; active_operations: Operation[]; primary_action: PrimaryAction }
export interface Operation { job_id: string; operation?: 'generate_subject' | 'generate_motion' | 'compose_frame' | 'generate_turnaround'; status: JobStatus; motion_id: string | null; turnaround_id?: string | null; output_key: string; progress_percent: number | null; stage: OperationStage; failure_code: JobFailureCode | null; failure_explanation: string | null; retryable: boolean; created_at?: string | null; updated_at?: string | null; generation_settings?: MotionGenerationSettings | null; prompt?: string | null; settlement_blocked?: string | null }
/** Subject work is matched by operation, never by a missing `motion_id`: a turnaround has none either. */
export const isSubjectOperation = (operation: Operation) => operation.operation === 'generate_subject' || operation.operation === 'compose_frame'
export const isTurnaroundOperation = (operation: Operation) => operation.operation === 'generate_turnaround' || Boolean(operation.turnaround_id)
/** Every place that names an operation uses this, so a turnaround is never shown as Subject work. */
export const operationLabel = (operation: Operation, name: (motionId: string) => string = (motionId) => motionId) =>
  isTurnaroundOperation(operation) ? 'Turnaround' : operation.motion_id && !isSubjectOperation(operation) ? name(operation.motion_id) : 'Subject'
export interface ActivityPage { operations: Operation[]; next_cursor: string | null }
/** `GET /pool/workers` (tenant-scoped, Studio-owned) and `GET /pool/queue` (pool-wide, cluster admins only). */
export type PoolWorkerStatus = 'online' | 'busy' | 'draining' | 'offline'
export interface PoolGpu { index: number; name: string; utilization_pct: number | null; memory_used_mb: number | null; memory_total_mb: number | null }
/** One kind of work and whether this machine can run it; `reason` says why not. */
export interface MachineFeature { key: 'subjects' | 'motions' | 'movie_takes' | 'movie_processing' | string; label: string; available: boolean; capability_id?: string | null; reason: string | null }
export interface PoolWorker { features?: MachineFeature[]; runtime_versions?: Record<string, string>; vram_mb?: number | null; worker_id: string; label?: string | null; status: PoolWorkerStatus; last_heartbeat_at: string | null; capabilities: string[]; gpus: PoolGpu[]; busy_job_id: string | null; draining: boolean }
export interface PoolWorkersResponse { workers: PoolWorker[]; cluster_admin?: boolean }
export interface PoolCapabilityQueueDepth { queued: number; processing: number }
export interface PoolQueueResponse { queued: number; processing: number; by_capability: Record<string, PoolCapabilityQueueDepth> }
/** `upload_headers` must be sent with the PUT unchanged: it carries the grant's signed size ceiling. */
export interface UploadTarget { key: string; upload_url: string; upload_headers?: Record<string, string> }
export type DeliveryStatus = 'pending' | 'delivered' | 'failed'
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }
export interface ReleaseDelivery { release_id: string; target: string; status: DeliveryStatus; delivered_at: string | null; summary: string; details: Record<string, JsonValue>; error: string | null; retryable: boolean }
export interface Release { release_id: string; project_id: string; name: string; source_revision: number; manifest_digest: string; release_digest: string; subject_revision_id: string | null; motion_revision_ids: Record<string, string>; quality_policy_versions: string[]; set_approval: Approval; deliveries: ReleaseDelivery[]; created_at?: string }
export interface ExportRecord { export_id: string; release_id: string; profile_name: string; profile_version: string; artifact_key: string; sha256: string; size_bytes: number; created_at?: string; completed_at?: string | null }
export interface HistoryEntry { revision: number | null; timestamp: string | null; actor: string | null; summary: string; failed: boolean }
export type PrimaryAction = 'add_subject' | 'approve_subject' | 'add_motion' | 'generate_motion' | 'select_candidates' | 'assess_set' | 'approve_set' | 'create_release' | 'export'
export type SubjectStateSummary = 'approved' | 'pending' | 'none'
export interface ProjectActivityStats { running: number; failed: number; completed: number; last_activity_at: string | null }
export interface ProjectSummary { project_id: string; name: string; revision: number; created_at: string; updated_at: string; created_by: string | null; subject_state: SubjectStateSummary; motion_count: number; release_count: number; external_subject: ExternalSubject | null; activity: ProjectActivityStats; unreadable: boolean; expires_at?: string | null }
export interface BadRevision { revision: number; created_at: string; error_summary: string; null_paths: string[] }
export interface ProjectRepairReport { project_id: string; current_revision_before: number; restored_from_revision: number | null; new_revision: number | null; repaired: boolean; bad_revisions: BadRevision[] }

export type MotionPreset = 'idle' | 'walk' | 'run' | 'jump' | 'attack' | 'guard' | 'cast' | 'hurt' | 'death' | 'trip'
export type AnimationType = 'movement' | 'attack' | 'spellcast'
export interface PresetInfo { preset: MotionPreset; display_name: string; description: string; semantic_slot: string; loop_policy: LoopPolicy; animation_type: AnimationType }

export interface QueuedMotion { motion_id: string; operation: Operation }
export interface FailedMotion { motion_id: string; code: string; message: string }
export interface GenerateMotionsResult { queued: QueuedMotion[]; failed: FailedMotion[] }

export interface SkippedCancellation { job_id: string; state: JobStatus }
export interface CancelAllResult { cancelled: string[]; skipped: SkippedCancellation[] }

/** Studio API keys (PR 117). `CreatedApiKey.key` is the plaintext, returned once and never again. */
export interface ApiKey { key_id: string; actor_id: string; name: string; created_at: string; last_used_at: string | null; revoked_at: string | null }
export interface CreatedApiKey extends ApiKey { key: string }

/** A machine someone enrolled. Never carries its credential. */
export interface EnrolledWorker { worker_id: string; label: string; created_at: string; last_seen_at: string | null; revoked_at: string | null }
/** Shown once at redemption; `tenant_id` is the owner whose work this machine may lease. */
export interface RedeemedWorker extends EnrolledWorker { credential: string; tenant_id: string }
/** Single use, one hour. There is no route to read a live token back. */
export interface PairingToken { token: string; expires_at: string }

/** `logout_url` ends the Auth0 tenant session too; absent when Auth0 is not configured. */
export interface SignOutResult { logout_url: string | null }

export interface HostedModel { model: string; label: string; path: string; closed_loop: boolean; duration_seconds: { min: number; max: number }; evidence: string }
/** A bring-your-own-key provider as generation discovery reports it. `settings` is present
 * only once a key is saved; the server uses the provider only when a request carries them. */
export interface HostedProviderOption { available: boolean; configured: boolean; key_hint?: string | null; billing?: string; models?: HostedModel[]; settings?: MotionGenerationSettings | null; reason?: string | null }
export interface ProviderKey { provider: string; available: boolean; configured: boolean; key_hint: string | null; updated_at: string | null }

/** What this account may run on. Public (self-registered) accounts get no shared machines,
 * and MiniMax H3 (every machine recipe) only where the operator allows it for them. */
export interface AccountCapabilities { tier: 'invited' | 'public'; house_compute: boolean; h3: boolean; own_machine: boolean }

export interface GenerationConfig {
  account?: AccountCapabilities;
  default_settings?: MotionGenerationSettings;
  scope?: string;
  selected?: { worker_id: string; configuration_version: string; profile: { profile: string; steps: number; recipe_version: string; evidence: string } } | null;
  settings?: MotionGenerationSettings | null;
  workers?: { worker_id: string; available: boolean; reason: string | null; profile?: { profile: string } | null; features?: { last_frame: boolean } }[];
  providers?: { higgsfield?: HostedProviderOption };
}

/** Turnarounds run only on the standard 24GB recipe; the 16GB LightX recipe is never selected for them. */
export const TURNAROUND_PROFILE = 'h3-24gb-standard'
/** A turnaround needs an available standard machine that itself reported last-frame support. */
export const canStartTurnaround = (config: GenerationConfig | undefined) =>
  Boolean(config?.workers?.some((worker) => worker.available && worker.profile?.profile === TURNAROUND_PROFILE
    && worker.features?.last_frame))

export interface SetupMotion { motion_id: string; name: string; kind?: MotionKind; preset?: MotionPreset | null; prompt?: string | null; action?: string | null; loop_policy?: LoopPolicy; required?: boolean; semantic_slot?: string | null; references?: MotionReference[]; style?: StylePreset | null; script?: string | null }
export interface SetupView { view_id: string; heading: Heading | null; name: string; reference: { revision_id: string } | null; motions: SetupMotion[] }
export interface ProjectSetup { schema_version: 1; sprite_layout: SpriteLayout; views: SetupView[]; identity_reference: { revision_id: string } | null }
export interface SetupPlan { plan_token: string; expected_revision: number; desired_setup: ProjectSetup; impacts: { impact_id: string; kind: string; view_id: string | null; motion_id: string | null; name: string; candidate_count: number; revision_count: number; requires_confirmation?: boolean; reference_count?: number; approval_count?: number }[]; blocked_operations: string[] }

/** `GET /studio/access-requests` (admins only): a request or invitation from before both
 * were closed. It grants nothing; the shared machines come only from the allowlist. */
export interface AccessRequest { email: string; first_requested_at: string; last_requested_at: string; request_count: number; invited_at?: string | null; invited_by?: string | null }
/** `GET /studio/access-requests?status=…&limit=…&offset=…`: one page and the total. */
export interface AccessRequestPage { items: AccessRequest[]; total: number }

/** Generate other directions from one image: a turnaround take, its picks and the drafts made from them. */
export type CompassHeading = 'N' | 'NE' | 'E' | 'SE' | 'S' | 'SW' | 'W' | 'NW'
export type EquipmentAttachment = 'hand' | 'shoulder' | 'hip' | 'back' | 'other'
export interface TurnaroundEquipment { item: string; attachment: EquipmentAttachment; source_picture_side: 'left' | 'right' | 'unsided' }
export interface TurnaroundPickEntry { frame_index: number; confirm_out_of_region?: boolean; confirm_out_of_order?: boolean }
export interface TurnaroundPickSet { pick_revision: number; picks: Partial<Record<CompassHeading, TurnaroundPickEntry>>; findings: { heading: CompassHeading; kind: 'out_of_region' | 'out_of_order' | 'unsuggested'; distance?: number | null }[]; saved_by: string; saved_at: string }
export interface Turnaround {
  turnaround_id: string; source_view_id: string; source_heading: CompassHeading; source_superseded: boolean
  source_backdrop: 'transparent' | 'chroma'; equipment: TurnaroundEquipment[]; prompt: string
  job_id: string | null; status: string | null; settled: boolean; frame_count: number | null
  cards_version: string; placement_cards: Partial<Record<CompassHeading, string[]>>; synthesized_parts: Partial<Record<CompassHeading, string[]>>
  pick_set: TurnaroundPickSet | null
  /** Present on a single read once settled. */
  suggested_picks?: Partial<Record<CompassHeading, { frame: number; reason: string }>> | null
  suggestions_error?: string | null
}
export interface PickProblem { heading: CompassHeading | null; kind: 'source' | 'incomplete' | 'range' | 'out_of_region' | 'out_of_order'; distance?: number | null; message: string }
export interface ViewDraft {
  draft_id: string; turnaround_id: string; heading: CompassHeading; pick_revision: number; frame_index: number
  /** Studio's suggestion the pick was checked against; null when there were no suggestions. */
  suggested_frame: number | null; picks_version: string
  image: { key: string; sha256: string; width: number; height: number }; image_path: string
  placement_card: string[]; synthesized_parts: string[]
  check: { number: number; facing: boolean; equipment: boolean; identity: boolean; checked_at: string } | null
  checked: boolean; discarded: boolean; bound_view_ids: string[]; bindable: boolean
  /** Why the draft no longer matches its turnaround, picks or source; absent from servers before it was added. */
  stale_reason?: string | null
}

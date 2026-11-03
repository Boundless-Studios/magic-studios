import { useIsMutating, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useState } from 'react'
import { ApiError, CONFLICT_MESSAGE, studioApi } from './client'
import { pollProjectOperations, operationRefetchInterval } from '../features/motions/operationPolling'
import type { Operation } from './types'

export const projectKey = (id: string) => ['studio-project', id] as const
/** Shared mutation key every project-scoped mutation tags itself with, so the header's save pill
 * can read a real pending count instead of a hardcoded "Saved". */
const projectMutationKey = (id: string) => ['project-mutation', id] as const

/** Any project mutation may have started a job. The operation pollers only tick while their
 * last fetch showed live work, so a fresh job started while they were idle would never be
 * seen (and never reconciled) unless something refetches them once. */
export async function wakeOperationPollers(client: ReturnType<typeof useQueryClient>, id: string) {
  await Promise.all([
    client.invalidateQueries({ queryKey: ['operations', id] }),
    client.invalidateQueries({ queryKey: ['activity', id] }),
  ])
}

export function useProject(id: string) { return useQuery({ queryKey: projectKey(id), queryFn: () => studioApi.project(id) }) }
function isConflict(error: unknown): error is ApiError { return error instanceof ApiError && error.status === 409 }
/**
 * Every mutation function reads `project.data!.revision` (or an equivalent prop) at call time,
 * so once the project query has been refetched a bare re-invocation picks up the new revision on
 * its own -- no revision needs to be threaded through here. `retryOnConflict` defaults to `true`:
 * on a 409 the project query is refetched and the mutation is retried exactly once before falling
 * back to the "changed elsewhere" notice. Non-409 errors, a second 409, and any mutation opting
 * out via `retryOnConflict: false` are never retried.
 */
export function useProjectMutation<TArgs, TResult = unknown>(id: string, fn: (args: TArgs) => Promise<TResult>, options?: { retryOnConflict?: boolean }) {
  const client = useQueryClient(); const [notice, setNotice] = useState<string | null>(null)
  const retryOnConflict = options?.retryOnConflict ?? true
  const mutation = useMutation({
    mutationKey: projectMutationKey(id),
    mutationFn: async (args: TArgs) => {
      try {
        return await fn(args)
      } catch (error) {
        if (!retryOnConflict || !isConflict(error)) throw error
        await client.invalidateQueries({ queryKey: projectKey(id) })
        return await fn(args)
      }
    },
    onSuccess: async (result) => { setNotice(null); if (result && typeof result === 'object' && 'project_id' in result && 'revision' in result) client.setQueryData(projectKey(id), result); else await client.invalidateQueries({ queryKey: projectKey(id) }); await wakeOperationPollers(client, id) },
    onError: async (error) => { if (isConflict(error)) { setNotice(CONFLICT_MESSAGE); await client.invalidateQueries({ queryKey: projectKey(id) }) } },
  })
  return { ...mutation, notice }
}

/** "Saving" while any project-scoped mutation (subject, motions, quality, release) is in flight. */
export function useProjectSaveState(id: string): 'Saving' | 'Saved' {
  const pending = useIsMutating({ mutationKey: projectMutationKey(id) })
  return pending > 0 ? 'Saving' : 'Saved'
}

/** A finished operation: failed, or completed and folded into the manifest (settled). Used only
 * to detect a *transition* into this state -- not to decide whether to keep polling, which is
 * `operationRefetchInterval`'s job. */
function isFinished(operation: Operation): boolean {
  return operation.status === 'failed' || (operation.status === 'completed' && operation.stage === 'settled')
}

/** Per-project snapshot of the last poll's operations, used only to detect a job's transition
 * into "finished" between one poll and the next (BOU-3208). Module-level so every workspace
 * shares one poller for a project -- keying it on a ref per hook instance would let whichever
 * component mounts first "win" the diff and leave the others blind to transitions that happened
 * while they were not yet mounted. */
const lastKnownOperations = new Map<string, Map<string, Operation>>()

/** Fields that only record *when* the server last touched a job, not anything an operator sees
 * change. They move on every poll of an in-flight job, so comparing them would defeat the
 * stabilisation below; every rendered field is still compared. */
const VOLATILE_OPERATION_FIELDS = new Set(['created_at', 'updated_at'])

function sameRenderedOperation(a: Operation, b: Operation): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)].filter((key) => !VOLATILE_OPERATION_FIELDS.has(key)))
  for (const key of keys) {
    const left = a[key as keyof Operation]
    const right = b[key as keyof Operation]
    if (left === right) continue
    // `generation_settings` is the only nested value an operation carries; a JSON compare is
    // cheaper than the re-render it prevents and needs no dependency.
    if (typeof left === 'object' && typeof right === 'object' && left !== null && right !== null && JSON.stringify(left) === JSON.stringify(right)) continue
    return false
  }
  return true
}

/** The last array `selectStableOperations` handed out per project, so an unchanged poll can be
 * answered with the very same reference. */
const stableOperationsByProject = new Map<string, Operation[]>()

/**
 * The operations query's `select`. React-query's structural sharing cannot help here: an in-flight
 * job's `updated_at` (and often `progress_percent`) is different on literally every poll, so the
 * array identity changes ~every 3s and every consumer of `operations.data` re-renders even though
 * nothing on screen moved. Returning the previous array whenever every rendered field is unchanged
 * makes a poll free for consumers, and coalesces progress into "did this motion's operation
 * actually change" (BOU-3208 follow-up: typing lag during a six-motion batch).
 */
export function selectStableOperations(projectId: string, next: Operation[]): Operation[] {
  const previous = stableOperationsByProject.get(projectId)
  if (previous && previous.length === next.length && previous.every((operation, index) => sameRenderedOperation(operation, next[index]!))) return previous
  stableOperationsByProject.set(projectId, next)
  return next
}

/**
 * The one poller every workspace and the Activity badge share for a project's operations
 * (BOU-3208): reconciles while anything is in flight so a pool-executed job's status can advance,
 * and -- on every operation's transition into failed or settled -- invalidates the project and
 * activity queries so the subject panel, motion candidates, and Activity counts refresh without
 * a reload.
 */
export function useProjectOperations(id: string, options?: { enabled?: boolean }) {
  const client = useQueryClient()
  const select = useCallback((operations: Operation[]) => selectStableOperations(id, operations), [id])
  return useQuery({
    select,
    queryKey: ['operations', id],
    queryFn: async () => {
      const next = await pollProjectOperations(id, { operations: studioApi.operations, reconcileActivity: studioApi.reconcileActivity })
      const previous = lastKnownOperations.get(id)
      // A fast job may already be settled the first time this poller sees it. Its
      // result still needs a project refetch, even without a prior running snapshot.
      const transitioned = next.some((operation) => {
        const prior = previous?.get(operation.job_id)
        return isFinished(operation) && (!prior || !isFinished(prior))
      })
      lastKnownOperations.set(id, new Map(next.map((operation) => [operation.job_id, operation])))
      if (transitioned) {
        await Promise.all([
          client.invalidateQueries({ queryKey: projectKey(id) }),
          client.invalidateQueries({ queryKey: ['activity', id] }),
        ])
      }
      return next
    },
    enabled: options?.enabled,
    // No retry: a transient failure must surface (and let the next scheduled poll try again)
    // rather than have react-query's default backoff (up to ~7s across 3 attempts) keep serving
    // the *previous* successful poll's data -- e.g. a job that already finished -- as if it were
    // still fresh, which is exactly the "stale local state" that made the Generate button's
    // spinner outlive the job it was tracking (BOU-3208 follow-up).
    retry: false,
    refetchInterval: (query) => operationRefetchInterval(query.state.data),
    // Keep polling while the tab is unfocused: react-query pauses interval refetches for
    // background tabs by default, so a user who switched away during a batch (or a browser
    // driven without focus) came back to takes stuck on "Queued" until something else
    // refetched. In-flight work is the one thing that must keep moving without attention.
    refetchIntervalInBackground: true,
  })
}

/**
 * A signed, short-lived read URL for an already-stored asset, keyed by its artifact key so every
 * consumer on a page (the subject strip, a take card, the motion rail thumbnail) shares one
 * in-flight request and one cached result per artifact instead of each firing its own. No retry:
 * a failed preview should say so quickly (`isError`) rather than keep the caller's "Loading
 * preview" state alive through react-query's default multi-second backoff.
 */
export function useAssetPreview(projectId: string, artifactKey: string | null | undefined, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: ['asset-preview', projectId, artifactKey],
    queryFn: () => studioApi.preview(projectId, artifactKey!),
    enabled: Boolean(artifactKey) && (options?.enabled ?? true),
    retry: false,
  })
}

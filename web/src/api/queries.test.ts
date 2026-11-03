import { renderHook, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { projectFixture, queryWrapper } from '../test/studioTestUtils'
import { CONFLICT_MESSAGE } from './client'
import { studioApi } from './client'
import { selectStableOperations, useProject, useProjectMutation, useProjectOperations } from './queries'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

/** `useProjectMutation`'s mutation function is exercised directly (via `studioApi.approveSubject`,
 * which is representative of every caller: it sends `expected_revision` and maps a 409 the same
 * way every other studio endpoint does) rather than through a UI, since the retry-on-conflict
 * behaviour lives entirely in the hook and does not depend on any particular feature component. */
function projectHandler(revision: number) {
  return http.get('/studio/projects/project-1', () => HttpResponse.json({ ...projectFixture, revision }))
}

it('refetches the project and resubmits once after a 409, without showing the notice', async () => {
  let approveCalls = 0
  server.use(
    projectHandler(4),
    http.post('/studio/projects/project-1/subject/approve', () => {
      approveCalls += 1
      if (approveCalls === 1) return HttpResponse.json({ detail: 'conflict' }, { status: 409 })
      return HttpResponse.json({ ...projectFixture, revision: 5 })
    }),
  )
  const { result } = renderHook(() => useProjectMutation('project-1', () => studioApi.approveSubject('project-1', 4)), { wrapper: queryWrapper() })

  result.current.mutate()

  await waitFor(() => expect(result.current.isSuccess).toBe(true))
  expect(approveCalls).toBe(2)
  expect(result.current.notice).toBeNull()
})

it('shows the notice after a second 409, having attempted exactly twice', async () => {
  let approveCalls = 0
  server.use(
    projectHandler(4),
    http.post('/studio/projects/project-1/subject/approve', () => {
      approveCalls += 1
      return HttpResponse.json({ detail: 'conflict' }, { status: 409 })
    }),
  )
  const { result } = renderHook(() => useProjectMutation('project-1', () => studioApi.approveSubject('project-1', 4)), { wrapper: queryWrapper() })

  result.current.mutate()

  await waitFor(() => expect(result.current.isError).toBe(true))
  expect(approveCalls).toBe(2)
  expect(result.current.notice).toBe(CONFLICT_MESSAGE)
})

it('does not retry a non-conflict error', async () => {
  let approveCalls = 0
  server.use(
    projectHandler(4),
    http.post('/studio/projects/project-1/subject/approve', () => {
      approveCalls += 1
      return HttpResponse.json({ detail: 'unsupported export profile' }, { status: 422 })
    }),
  )
  const { result } = renderHook(() => useProjectMutation('project-1', () => studioApi.approveSubject('project-1', 4)), { wrapper: queryWrapper() })

  result.current.mutate()

  await waitFor(() => expect(result.current.isError).toBe(true))
  expect(approveCalls).toBe(1)
  expect(result.current.notice).toBeNull()
})

it('does not retry when the caller opts out with retryOnConflict: false', async () => {
  let approveCalls = 0
  server.use(
    projectHandler(4),
    http.post('/studio/projects/project-1/subject/approve', () => {
      approveCalls += 1
      return HttpResponse.json({ detail: 'conflict' }, { status: 409 })
    }),
  )
  const { result } = renderHook(
    () => useProjectMutation('project-1', () => studioApi.approveSubject('project-1', 4), { retryOnConflict: false }),
    { wrapper: queryWrapper() },
  )

  result.current.mutate()

  await waitFor(() => expect(result.current.isError).toBe(true))
  expect(approveCalls).toBe(1)
  expect(result.current.notice).toBe(CONFLICT_MESSAGE)
})

it('reconciles a generate_subject job (motion_id: null) that is completed but awaiting settlement, on the very first poll -- BOU-3208 follow-up: any unsettled operation, on any page, routes through reconcile', async () => {
  let opsCalls = 0
  let reconcileCalls = 0
  const awaitingSettlement = { job_id: 'subject-job', operation: 'generate_subject' as const, status: 'completed' as const, motion_id: null, output_key: 'out', progress_percent: null, stage: 'awaiting_settlement' as const, failure_code: null, failure_explanation: null, retryable: false }
  const settled = { ...awaitingSettlement, stage: 'settled' as const }
  server.use(
    http.get('/studio/projects/project-1/operations', () => { opsCalls += 1; return HttpResponse.json([awaitingSettlement]) }),
    http.post('/studio/projects/project-1/activity/reconcile', () => { reconcileCalls += 1; return HttpResponse.json({ operations: [settled], next_cursor: null }) }),
  )
  const { result } = renderHook(() => useProjectOperations('project-1'), { wrapper: queryWrapper() })
  await waitFor(() => expect(result.current.data).toEqual([settled]))
  expect(opsCalls).toBe(1)
  expect(reconcileCalls).toBe(1)
})

/** Perf: an in-flight job's payload arrives every poll with a fresh `updated_at` even when nothing
 * an operator sees has moved. `select` must hand back the *same* array so every consumer of
 * `operations.data` skips the render (the typing-lag-during-a-batch fix). */
it('the operations select returns a referentially equal value when the rendered payload is equal', () => {
  const base = { job_id: 'job-1', operation: 'generate_motion' as const, status: 'processing' as const, motion_id: 'idle', output_key: 'out', progress_percent: 40, stage: 'generating' as const, failure_code: null, failure_explanation: null, retryable: false, updated_at: '2026-09-01T10:00:00Z' }
  const first = selectStableOperations('stable-project', [{ ...base }])
  // A later poll: identical to render, only the server's timestamp moved.
  const second = selectStableOperations('stable-project', [{ ...base, updated_at: '2026-09-01T10:00:03Z' }])
  expect(second).toBe(first)

  // Progress is a rendered field, so it must still propagate as a new reference.
  const third = selectStableOperations('stable-project', [{ ...base, progress_percent: 55 }])
  expect(third).not.toBe(second)
  expect(third[0]!.progress_percent).toBe(55)

  // So must a status change, and a job disappearing.
  expect(selectStableOperations('stable-project', [{ ...base, progress_percent: 55, status: 'completed' as const, stage: 'settled' as const }])).not.toBe(third)
  expect(selectStableOperations('stable-project', [])).toEqual([])

  // Projects are stabilised independently -- one project's poll must not answer another's.
  expect(selectStableOperations('other-project', [{ ...base }])).not.toBe(first)
})


it('refreshes the project when a job settles before the first operation poll', async () => {
  const id = 'fast-settlement-project'
  let revision = 4
  const job = { job_id: 'fast-subject', operation: 'generate_subject', status: 'completed', motion_id: null, output_key: 'out', progress_percent: null, stage: 'awaiting_settlement', failure_code: null, failure_explanation: null, retryable: false }
  server.use(
    http.get(`/studio/projects/${id}`, () => HttpResponse.json({ ...projectFixture, project_id: id, revision })),
    http.get(`/studio/projects/${id}/operations`, () => HttpResponse.json([job])),
    http.post(`/studio/projects/${id}/activity/reconcile`, () => {
      revision = 5
      return HttpResponse.json({ operations: [{ ...job, stage: 'settled' }], next_cursor: null })
    }),
  )
  const { result, rerender } = renderHook(({ enabled }) => ({
    project: useProject(id), operations: useProjectOperations(id, { enabled }),
  }), { initialProps: { enabled: false }, wrapper: queryWrapper() })
  await waitFor(() => expect(result.current.project.data?.revision).toBe(4))
  rerender({ enabled: true })
  await waitFor(() => expect(result.current.operations.data?.[0]?.stage).toBe('settled'))
  await waitFor(() => expect(result.current.project.data?.revision).toBe(5))
})

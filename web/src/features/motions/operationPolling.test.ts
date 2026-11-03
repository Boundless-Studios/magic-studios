import { expect, it, vi } from 'vitest'
import type { Operation } from '../../api/types'
import { needsSettlement, operationRefetchInterval, pollProjectOperations } from './operationPolling'

const queued: Operation = { job_id: 'a', status: 'queued', motion_id: 'idle', output_key: 'out', progress_percent: null, stage: 'queued', failure_code: null, failure_explanation: null, retryable: false }
const processing: Operation = { ...queued, job_id: 'b', status: 'processing', stage: 'generating' }
const completedUnsettled: Operation = { ...queued, job_id: 'c', status: 'completed', stage: 'awaiting_settlement' }
const failed: Operation = { ...queued, job_id: 'd', status: 'failed', stage: 'failed', retryable: true }
// A `generate_subject` job (and `compose_frame`) always carries `motion_id: null` -- the poller
// must treat it the same as any motion job's operation, not only ones with a motion_id.
const subjectAwaitingSettlement: Operation = { ...completedUnsettled, job_id: 'e', operation: 'generate_subject', motion_id: null }

it('polls only while an operation still needs integration', () => {
  expect(operationRefetchInterval([])).toBe(false)
  expect(operationRefetchInterval([failed])).toBe(false)
  expect(operationRefetchInterval([processing])).toBe(1500)
  expect(operationRefetchInterval([completedUnsettled])).toBe(1500)
  expect(operationRefetchInterval([{ ...completedUnsettled, stage: 'settled' }])).toBe(false)
  // Same for a generate_subject job (motion_id: null) -- the interval must not go stale just
  // because the operation has no motion_id.
  expect(operationRefetchInterval([subjectAwaitingSettlement])).toBe(1500)
  expect(operationRefetchInterval([{ ...subjectAwaitingSettlement, stage: 'settled' }])).toBe(false)
})

it('identifies a completed op still awaiting settlement', () => {
  expect(needsSettlement(queued)).toBe(false)
  expect(needsSettlement(processing)).toBe(false)
  expect(needsSettlement(completedUnsettled)).toBe(true)
  expect(needsSettlement(failed)).toBe(false)
})

it('reconciles instead of only relisting when a completed op is awaiting settlement', async () => {
  const operations = vi.fn().mockResolvedValueOnce([completedUnsettled])
  const reconcileActivity = vi.fn().mockResolvedValue([])
  const onSettled = vi.fn()
  const result = await pollProjectOperations('project-1', { operations, reconcileActivity }, onSettled)
  expect(reconcileActivity).toHaveBeenCalledWith('project-1')
  expect(operations).toHaveBeenCalledTimes(1)
  expect(onSettled).toHaveBeenCalledTimes(1)
  expect(result).toEqual([])
})

it('reconciles while a job is only queued or processing, not just when awaiting settlement -- a pool-executed job never advances via the plain listing alone (BOU-3208)', async () => {
  const operations = vi.fn().mockResolvedValueOnce([queued])
  const reconciledResult = [{ ...queued, status: 'completed' as const, stage: 'settled' as const }]
  const reconcileActivity = vi.fn().mockResolvedValue(reconciledResult)
  const onSettled = vi.fn()
  const result = await pollProjectOperations('project-1', { operations, reconcileActivity }, onSettled)
  expect(reconcileActivity).toHaveBeenCalledWith('project-1')
  expect(onSettled).toHaveBeenCalledTimes(1)
  expect(result).toEqual(reconciledResult)
})

it('skips reconciliation entirely when nothing is in flight', async () => {
  const operations = vi.fn().mockResolvedValue([failed])
  const reconcileActivity = vi.fn()
  const onSettled = vi.fn()
  const result = await pollProjectOperations('project-1', { operations, reconcileActivity }, onSettled)
  expect(reconcileActivity).not.toHaveBeenCalled()
  expect(operations).toHaveBeenCalledTimes(1)
  expect(onSettled).not.toHaveBeenCalled()
  expect(result).toEqual([failed])
})

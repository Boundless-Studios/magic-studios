import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { studioApi } from './client'

const server = setupServer()
beforeAll(() => server.listen()); afterEach(() => server.resetHandlers()); afterAll(() => server.close())
const TURN = `turnaround-${'a'.repeat(32)}`
const DRAFT = `draft-${'b'.repeat(32)}`

it('starts a turnaround from the source view with a fresh request id', async () => {
  let body: Record<string, unknown> = {}
  server.use(http.post('/studio/projects/p-1/turnarounds', async ({ request }) => { body = await request.json() as Record<string, unknown>; return HttpResponse.json({ turnaround_id: TURN, operation: { job_id: 'j' } }) }))
  const started = await studioApi.startTurnaround('p-1', 3, 'S', 'rev-s', [{ item: 'axe', attachment: 'hand', source_picture_side: 'left' }])
  expect(started.turnaround_id).toBe(TURN)
  expect(body).toEqual(expect.objectContaining({ expected_revision: 3, source_view_id: 'S', source_revision_id: 'rev-s', equipment: [{ item: 'axe', attachment: 'hand', source_picture_side: 'left' }] }))
  expect(typeof body.request_id).toBe('string')
})

it('saves the whole pick set with PUT and the pick revision, and surfaces a stale one', async () => {
  let method = ''; let body: Record<string, unknown> = {}
  server.use(http.put(`/studio/projects/p-1/turnarounds/${TURN}/picks`, async ({ request }) => {
    method = request.method; body = await request.json() as Record<string, unknown>
    return body.expected_pick_revision === 1 ? HttpResponse.json({ pick_revision: 2, picks: {}, findings: [], saved_by: 'u', saved_at: 'now' }) : HttpResponse.json({ detail: { message: 'the pick set is at revision 2', pick_revision: 2 } }, { status: 409 })
  }))
  expect((await studioApi.saveTurnaroundPicks('p-1', TURN, 1, { N: { frame_index: 52 } })).pick_revision).toBe(2)
  expect(method).toBe('PUT'); expect(body).toEqual({ expected_pick_revision: 1, picks: { N: { frame_index: 52 } } })
  await expect(studioApi.saveTurnaroundPicks('p-1', TURN, 0, { N: { frame_index: 52 } })).rejects.toEqual(expect.objectContaining({ status: 409 }))
})

it('makes, checks and discards a draft against the project revision', async () => {
  const seen: Record<string, unknown>[] = []
  server.use(
    http.post('/studio/projects/p-1/view-drafts', async ({ request }) => { seen.push(await request.json() as Record<string, unknown>); return HttpResponse.json({ draft_id: DRAFT, draft: { draft_id: DRAFT } }) }),
    http.post(`/studio/projects/p-1/view-drafts/${DRAFT}/check`, async ({ request }) => { seen.push(await request.json() as Record<string, unknown>); return HttpResponse.json({ draft_id: DRAFT, bindable: true }) }),
    http.post(`/studio/projects/p-1/view-drafts/${DRAFT}/discard`, async ({ request }) => { seen.push(await request.json() as Record<string, unknown>); return HttpResponse.json({ draft_id: DRAFT, discarded: true }) }),
  )
  await studioApi.createViewDraft('p-1', 4, TURN, 2, 'N')
  await studioApi.checkViewDraft('p-1', DRAFT, 5, { facing: true, equipment: true, identity: true })
  await studioApi.discardViewDraft('p-1', DRAFT, 6)
  expect(seen[0]).toEqual(expect.objectContaining({ expected_revision: 4, turnaround_id: TURN, pick_revision: 2, heading: 'N', standing: false }))
  expect(seen[1]).toEqual({ expected_revision: 5, facing: true, equipment: true, identity: true })
  expect(seen[2]).toEqual({ expected_revision: 6 })
})

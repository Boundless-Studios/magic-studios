import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it, vi } from 'vitest'
import { ApiError, CONFLICT_MESSAGE, sentenceCase, studioApi } from './client'

const server = setupServer()
beforeAll(() => server.listen()); afterEach(() => { server.resetHandlers(); vi.unstubAllGlobals() }); afterAll(() => server.close())

it('does not claim a trusted browser identity', async () => {
  server.use(http.get('/studio/projects/project-1', ({ request }) => { expect(request.headers.get('X-Studio-Actor-Id')).toBeNull(); return HttpResponse.json({ project_id: 'project-1' }) }))
  await studioApi.project('project-1')
})

it('generates a request ID when randomUUID is unavailable', async () => {
  vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => { bytes.fill(0); return bytes } })
  server.use(http.post('/studio/projects/project-1/subject/generate', async ({ request }) => {
    expect(await request.json()).toEqual(expect.objectContaining({ request_id: '00000000-0000-4000-8000-000000000000' }))
    return HttpResponse.json({ job_id: 'job-1' })
  }))

  await studioApi.generateSubject('project-1', 1, ['reference.png'], '')
})

it('uses the exact professional conflict message for the server\'s canonical revision-conflict shape', async () => {
  server.use(http.post('/studio/projects/project-1/subject/approve', () => HttpResponse.json({ detail: { message: 'expected revision is stale', current_revision: 7 } }, { status: 409 })))
  await expect(studioApi.approveSubject('project-1', 4)).rejects.toEqual(expect.objectContaining({ status: 409, message: CONFLICT_MESSAGE }))
})

it('falls back to the conflict message for an unrecognized empty 409 body', async () => {
  server.use(http.post('/studio/projects/project-1/subject/approve', () => HttpResponse.json({}, { status: 409 })))
  await expect(studioApi.approveSubject('project-1', 4)).rejects.toEqual(expect.objectContaining({ status: 409, message: CONFLICT_MESSAGE }))
})

it('falls back to a generic message when a rejection carries no usable body', async () => {
  server.use(http.post('/studio/releases/release-1/exports', () => HttpResponse.json(null, { status: 500 })))
  await expect(studioApi.createExport('release-1', 4, 'png-json')).rejects.toEqual(expect.objectContaining({ status: 500, message: 'The request could not be completed.' }))
})

it('falls back to a status-appropriate message when the body carries no message', async () => {
  server.use(http.post('/studio/releases/release-1/exports', () => HttpResponse.json({}, { status: 422 })))
  await expect(studioApi.createExport('release-1', 4, 'png-json')).rejects.toEqual(expect.objectContaining({ status: 422, message: 'Check the file or settings and try again.' }))
})

// The backend rejects a mutation with one of three JSON shapes. All three must surface the
// server's own human-readable reason (BOU: "That action could not be completed" gave the
// operator no idea their subject was unapproved) -- and a `code`, when the server sent one, is
// exposed on the error so the UI can react to a specific rejection (e.g. link to Approve).
it.each([
  ['a plain string detail', { detail: 'motion generation requires the approved current subject' }, 'motion generation requires the approved current subject', undefined],
  ['a structured detail with code + message', { detail: { code: 'unapproved_subject', message: 'motion generation requires the approved current subject' } }, 'motion generation requires the approved current subject', 'unapproved_subject'],
  ['a top-level code + message', { code: 'unapproved_subject', message: 'motion generation requires the approved current subject' }, 'motion generation requires the approved current subject', 'unapproved_subject'],
])('surfaces the server reason from %s', async (_label, body, message, code) => {
  server.use(http.post('/studio/releases/release-1/exports', () => HttpResponse.json(body, { status: 422 })))
  await expect(studioApi.createExport('release-1', 4, 'png-json')).rejects.toEqual(expect.objectContaining({ status: 422, message, code }))
})

it('sentence-cases a lowercase server reason without otherwise touching it', () => {
  expect(sentenceCase('motion generation requires the approved current subject')).toBe('Motion generation requires the approved current subject')
  expect(sentenceCase('')).toBe('')
})

it('surfaces the server\'s own message for a 409 that is not the canonical revision-conflict shape', async () => {
  // A blocked action must never read as a phantom "changed elsewhere" conflict -- only the
  // server's canonical revision-conflict shape (a `current_revision` number) maps to that
  // notice; every other 409 body surfaces its own message like any other rejection.
  server.use(http.post('/studio/projects/project-1/subject/approve', () => HttpResponse.json({ detail: { code: 'subject_approval_blocked', message: 'subject approval is blocked by quality findings' } }, { status: 409 })))
  await expect(studioApi.approveSubject('project-1', 4)).rejects.toBeInstanceOf(ApiError)
  await expect(studioApi.approveSubject('project-1', 4)).rejects.toEqual(expect.objectContaining({ status: 409, message: 'subject approval is blocked by quality findings', code: 'subject_approval_blocked' }))
})

it('sends an upload grant\'s signed size header with the file', async () => {
  server.use(http.put('https://storage.invalid/signed', ({ request }) => {
    expect(request.headers.get('x-goog-content-length-range')).toBe('0,20000000')
    expect(request.headers.get('Content-Type')).toBe('image/png')
    return new HttpResponse(null, { status: 200 })
  }))
  const file = new File(['png'], 'reference.png', { type: 'image/png' })
  await studioApi.upload({ key: 'k', upload_url: 'https://storage.invalid/signed', upload_headers: { 'x-goog-content-length-range': '0,20000000' } }, file)
})

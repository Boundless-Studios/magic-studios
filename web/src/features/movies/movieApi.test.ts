import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'

import { ApiError } from '../../api/client'
import { movieApi } from './movieApi'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

it('shows a coded refusal, such as a screened prompt, with its own message and code', async () => {
  server.use(http.post('/studio/movies/m-1/generate', () => HttpResponse.json(
    { detail: { code: 'prompt_refused', category: 'sexual', message: 'This prompt was refused under our acceptable-use rules: sexually explicit content. Rewrite it and try again.' } },
    { status: 422 },
  )))

  const failure = await movieApi.generate('m-1', { expected_revision: 1, shot_ids: ['a'], request_id: 'r1' }).catch((error: unknown) => error)

  expect(failure).toBeInstanceOf(ApiError)
  expect((failure as ApiError).message).toMatch(/refused under our acceptable-use rules/)
  expect((failure as ApiError).code).toBe('prompt_refused')
})


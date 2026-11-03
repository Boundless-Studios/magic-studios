import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { authNavigation, redirectToLogin } from './auth'
import { movieApi } from '../features/movies/movieApi'
import { ApiError, studioApi } from './client'

const server = setupServer()
beforeAll(() => server.listen())
afterEach(() => { server.resetHandlers(); vi.restoreAllMocks(); window.history.replaceState(null, '', '/') })
afterAll(() => server.close())

describe('401 handling', () => {
  it('returns to the movie workspace after an expired session', async () => {
    const redirect = vi.spyOn(authNavigation, 'redirect').mockImplementation(() => {})
    server.use(http.get('/studio/movies/m-1', () => HttpResponse.json({ detail: 'unauthenticated' }, { status: 401 })))
    window.history.replaceState(null, '', '/movies/m-1/shots')
    await expect(movieApi.get('m-1')).rejects.toBeInstanceOf(ApiError)
    expect(redirect).toHaveBeenCalledWith('/login?return_to=%2Fmovies%2Fm-1%2Fshots')
  })

  it('sends any unauthorized studioApi call to /login with the current path', async () => {
    const redirect = vi.spyOn(authNavigation, 'redirect').mockImplementation(() => {})
    server.use(http.get('/studio/projects', () => HttpResponse.json({ detail: 'unauthenticated' }, { status: 401 })))
    window.history.replaceState(null, '', '/projects/p-1/motions?tab=ready')

    await expect(studioApi.listProjects()).rejects.toBeInstanceOf(ApiError)

    expect(redirect).toHaveBeenCalledWith('/login?return_to=%2Fprojects%2Fp-1%2Fmotions%3Ftab%3Dready')
  })

  it('does not bounce a user who is already on the sign-in page', () => {
    const redirect = vi.spyOn(authNavigation, 'redirect').mockImplementation(() => {})
    window.history.replaceState(null, '', '/login?error=expired')
    redirectToLogin()
    expect(redirect).not.toHaveBeenCalled()
  })
})

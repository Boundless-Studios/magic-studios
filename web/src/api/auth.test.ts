import { describe, expect, it } from 'vitest'

import { auth0LoginUrl, auth0SignUpUrl, loginUrl, readStudioSession, safeReturnPath } from './auth'

describe('Studio session', () => {
  it('accepts a JSON authenticated session with its identity fields', async () => {
    const result = await readStudioSession(new Response(
      JSON.stringify({ actor_id: 'iap:123', email: 'someone@example.com', display_name: 'Someone', sign_in_method: 'auth0' }),
      { headers: { 'Content-Type': 'application/json' } },
    ))
    expect(result).toEqual({ actorId: 'iap:123', email: 'someone@example.com', displayName: 'Someone', signInMethod: 'auth0', admin: false })
  })

  it('tolerates a session that carries only an actor id', async () => {
    const result = await readStudioSession(new Response(JSON.stringify({ actor_id: 'gateway:dev', sign_in_method: 'nonsense' }), { headers: { 'Content-Type': 'application/json' } }))
    expect(result).toEqual({ actorId: 'gateway:dev', email: null, displayName: null, signInMethod: null, admin: false })
  })

  it('reads an unauthenticated HTML response as no session', async () => {
    expect(await readStudioSession(new Response('<html>login</html>', { status: 401, headers: { 'Content-Type': 'text/html' } }))).toBeNull()
  })
})

describe('Sign-in URLs', () => {
  it('keeps return paths same-origin', () => {
    expect(safeReturnPath('/projects/p-1/motions?tab=ready#main')).toBe('/projects/p-1/motions?tab=ready#main')
    expect(safeReturnPath('https://evil.invalid/steal')).toBe('/')
    expect(safeReturnPath('//evil.invalid/steal')).toBe('/')
    expect(safeReturnPath('/\\evil.invalid')).toBe('/')
  })

  it('builds the login and Auth0 start URLs', () => {
    expect(loginUrl('/projects/p-1/motions?tab=ready#main')).toBe('/login?return_to=%2Fprojects%2Fp-1%2Fmotions%3Ftab%3Dready%23main')
    expect(loginUrl('/')).toBe('/welcome')
    expect(loginUrl('https://evil.invalid/steal')).toBe('/welcome')
    expect(auth0LoginUrl('/projects/p-1/subject')).toBe('/studio/auth/login?return_to=%2Fprojects%2Fp-1%2Fsubject')
    expect(auth0LoginUrl('//evil.invalid')).toBe('/studio/auth/login?return_to=%2F')
    expect(auth0SignUpUrl('/projects')).toBe('/studio/auth/login?return_to=%2Fprojects&signup=true')
    expect(auth0SignUpUrl('//evil.invalid')).toBe('/studio/auth/login?return_to=%2F&signup=true')
  })
})

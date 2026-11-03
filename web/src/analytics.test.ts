import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CF_BEACON_SRC } from './analytics'

const beacons = () => document.head.querySelectorAll(`script[src="${CF_BEACON_SRC}"]`)

describe('initAnalytics', () => {
  beforeEach(() => {
    vi.resetModules()
    beacons().forEach((script) => script.remove())
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    beacons().forEach((script) => script.remove())
  })

  it('adds no beacon when VITE_CF_BEACON_TOKEN is empty', async () => {
    vi.stubEnv('VITE_CF_BEACON_TOKEN', '')

    const { initAnalytics } = await import('./analytics')
    initAnalytics()

    expect(beacons()).toHaveLength(0)
  })

  it('adds no beacon when the token is only whitespace', async () => {
    vi.stubEnv('VITE_CF_BEACON_TOKEN', '   ')

    const { initAnalytics } = await import('./analytics')
    initAnalytics()

    expect(beacons()).toHaveLength(0)
  })

  it('adds one deferred Cloudflare beacon carrying the token when it is set', async () => {
    vi.stubEnv('VITE_CF_BEACON_TOKEN', ' abc123 ')

    const { initAnalytics } = await import('./analytics')
    initAnalytics()
    initAnalytics()

    const scripts = beacons()
    expect(scripts).toHaveLength(1)
    const script = scripts[0] as HTMLScriptElement
    expect(script.defer).toBe(true)
    expect(JSON.parse(script.getAttribute('data-cf-beacon')!)).toEqual({ token: 'abc123', spa: true })
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'

/** Load the module fresh, as a build with this `VITE_SITE_MEDIA_BASE` would. */
async function load(base: string | undefined) {
  vi.stubEnv('VITE_SITE_MEDIA_BASE', base)
  vi.resetModules()
  return import('./siteMedia')
}

describe('site media', () => {
  afterEach(() => { vi.unstubAllEnvs() })

  it('is enabled by the deploy-time base and builds URLs beneath it', async () => {
    const media = await load('/assets/site-media/1')
    expect(media.siteMediaEnabled).toBe(true)
    expect(media.siteMediaUrl('welcome/flight.mp4')).toBe('/assets/site-media/1/welcome/flight.mp4')
    expect(media.siteMediaUrl('/welcome/flight.mp4')).toBe('/assets/site-media/1/welcome/flight.mp4')
  })

  it('tolerates a trailing slash and surrounding space in the base', async () => {
    const media = await load(' /assets/site-media/2/ ')
    expect(media.siteMediaEnabled).toBe(true)
    expect(media.siteMediaUrl('welcome/orc-walk-n.webp')).toBe('/assets/site-media/2/welcome/orc-walk-n.webp')
  })

  it.each([['unset', undefined], ['empty', ''], ['blank', '   ']])('is disabled when the base is %s, as in public builds', async (_, base) => {
    expect((await load(base)).siteMediaEnabled).toBe(false)
  })
})

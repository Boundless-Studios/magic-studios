import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const initMock = vi.fn()

vi.mock('@sentry/react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@sentry/react')>()
  return { ...actual, init: initMock }
})

describe('initMonitoring', () => {
  beforeEach(() => {
    vi.resetModules()
    initMock.mockClear()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('is a no-op when VITE_SENTRY_DSN is empty', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', '')

    const { initMonitoring } = await import('./monitoring')
    initMonitoring()

    expect(initMock).not.toHaveBeenCalled()
  })

  it('initializes Sentry when VITE_SENTRY_DSN is set', async () => {
    vi.stubEnv('VITE_SENTRY_DSN', 'https://abc@example.ingest.sentry.io/123')
    vi.stubEnv('VITE_SENTRY_ENVIRONMENT', 'production')
    vi.stubEnv('VITE_APP_RELEASE', 'web@1')

    const { initMonitoring } = await import('./monitoring')
    initMonitoring()

    expect(initMock).toHaveBeenCalledTimes(1)
    const options = initMock.mock.calls[0][0]
    expect(options.dsn).toBe('https://abc@example.ingest.sentry.io/123')
    expect(options.environment).toBe('production')
    expect(options.release).toBe('web@1')
    expect(options.replaysSessionSampleRate).toBe(0)
    expect(options.replaysOnErrorSampleRate).toBe(0)
    expect(options.tracesSampleRate).toBe(0.05)
  })

  it("ignores Safari's injected EmptyRanges error and nothing broader", async () => {
    vi.stubEnv('VITE_SENTRY_DSN', 'https://abc@example.ingest.sentry.io/123')

    const { initMonitoring } = await import('./monitoring')
    initMonitoring()

    const ignored: RegExp[] = initMock.mock.calls[0][0].ignoreErrors
    const matches = (message: string) => ignored.some((pattern) => pattern.test(message))
    expect(matches("ReferenceError: Can't find variable: EmptyRanges")).toBe(true)
    expect(matches("ReferenceError: Can't find variable: ModeShowcase")).toBe(false)
  })
})

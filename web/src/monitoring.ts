import * as Sentry from '@sentry/react'

/**
 * Sentry error and performance monitoring for the Studio web app.
 *
 * Configuration is env-only: this repo is public, so no DSN or org may ever
 * live in source. `initMonitoring` is a documented no-op whenever
 * `VITE_SENTRY_DSN` is empty, which is the default for local development,
 * Vitest, and CI.
 */

const TRACES_SAMPLE_RATE = 0.05

// Thrown by a script iOS Safari injects into the page, not by our bundle: the
// name appears nowhere in our source or dependencies.
const IGNORED_ERRORS = [/Can't find variable: EmptyRanges/]

export function initMonitoring(): void {
  const dsn = import.meta.env.VITE_SENTRY_DSN
  if (!dsn) return

  Sentry.init({
    dsn,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT || 'development',
    release: import.meta.env.VITE_APP_RELEASE || undefined,
    integrations: [
      Sentry.browserTracingIntegration({
        // Same-origin `/studio` calls only — never send trace headers to a
        // third-party origin.
        shouldCreateSpanForRequest: (url) => url.includes('/studio'),
      }),
    ],
    tracesSampleRate: TRACES_SAMPLE_RATE,
    tracePropagationTargets: [/^\/studio\//],
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
    ignoreErrors: IGNORED_ERRORS,
  })
}

export const SentryErrorBoundary = Sentry.ErrorBoundary

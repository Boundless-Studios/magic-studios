/**
 * Cloudflare Web Analytics for the public site and the Studio.
 *
 * Configuration is env-only, like `monitoring.ts`: `initAnalytics` is a
 * documented no-op whenever `VITE_CF_BEACON_TOKEN` is empty, which is the
 * default for local development, Vitest, and CI. The beacon is cookieless (it
 * stores nothing in the browser), Cloudflare does not log query strings, and
 * single-page route changes are counted by the beacon itself, so there is no
 * per-route call here and no custom event API.
 */

export const CF_BEACON_SRC = 'https://static.cloudflareinsights.com/beacon.min.js'

export function initAnalytics(): void {
  const token = import.meta.env.VITE_CF_BEACON_TOKEN?.trim()
  if (!token) return
  if (document.querySelector(`script[src="${CF_BEACON_SRC}"]`)) return

  const script = document.createElement('script')
  script.defer = true
  script.src = CF_BEACON_SRC
  script.setAttribute('data-cf-beacon', JSON.stringify({ token, spa: true }))
  document.head.appendChild(script)
}

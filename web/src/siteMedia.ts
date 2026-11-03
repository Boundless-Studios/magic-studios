/**
 * Hosted site media: the welcome page's demo videos, sprite sheets and captions.
 *
 * The media are not in the repository. The canonical deploy builds with
 * `VITE_SITE_MEDIA_BASE` set to the `base` in `scripts/site_media.json` (a
 * versioned, immutable prefix such as `/assets/site-media/1`) and stages the
 * files there from a pinned private release with `scripts/stage_site_media.py`.
 * Public and self-hosted builds leave it unset, so `siteMediaEnabled` is false
 * and pages render without those media instead of showing broken ones.
 */
const base = (import.meta.env.VITE_SITE_MEDIA_BASE ?? '').trim().replace(/\/+$/, '')

export const siteMediaEnabled = base !== ''

/** The URL of a file under the media base, e.g. `siteMediaUrl('welcome/flight.mp4')`. */
export function siteMediaUrl(path: string): string {
  return `${base}/${path.replace(/^\/+/, '')}`
}
